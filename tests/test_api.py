"""API-level tests for the dangerous behaviours. Run: python -m unittest discover -s tests -v"""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app import db, seed, server, worker  # noqa: E402

BASE = None
TOKENS = {}


def call(method, path, body=None, token=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


def as_(name):
    return TOKENS[name]


def setUpModule():
    global BASE
    conn = db.connect()
    db.init_db(conn)
    seed.seed(conn, n_items=250)
    conn.close()
    srv = server.make_server("127.0.0.1", 0)
    BASE = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for name in ("admin", "alice", "bob", "gina", "ivan", "carol"):
        st, body, _ = call("POST", "/api/login", {"email": f"{name}@demo.test", "password": seed.PASSWORD})
        assert st == 200, body
        TOKENS[name] = body["token"]


def team_id(name):
    return db.connect().execute("SELECT id FROM teams WHERE name=?", (name,)).fetchone()[0]


def new_item(who="bob", team="Payments Ops", **extra):
    st, body, _ = call("POST", "/api/items", {"title": "Test item", "team_id": team_id(team), **extra}, as_(who))
    assert st == 201, body
    return body


def events(item_id):
    return [e["type"] for e in call("GET", f"/api/items/{item_id}/activity", token=as_("admin"))[1]["events"]]


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_claim_exactly_one_wins(self):
        item = new_item()
        results, barrier = [], threading.Barrier(3)

        def claim(who):
            barrier.wait()
            results.append((who, call("POST", f"/api/items/{item['id']}/claim", {}, as_(who))[0]))

        threads = [threading.Thread(target=claim, args=(w,)) for w in ("bob", "alice", "admin")]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(sorted(code for _, code in results), [200, 409, 409])
        winner = next(w for w, c in results if c == 200)
        final = call("GET", f"/api/items/{item['id']}", token=as_("admin"))[1]
        winner_id = call("GET", "/api/me", token=as_(winner))[1]["user"]["id"]
        self.assertEqual(final["assignee_id"], winner_id)
        self.assertEqual(events(item["id"]).count("assignee_changed"), 1)

    def test_stale_update_rejected_and_data_unchanged(self):
        item = new_item()
        st, v2, _ = call("PATCH", f"/api/items/{item['id']}", {"version": 1, "priority": "high"}, as_("bob"))
        self.assertEqual((st, v2["version"]), (200, 2))
        before = len(events(item["id"]))
        st, body, _ = call("PATCH", f"/api/items/{item['id']}", {"version": 1, "title": "Overwrite!"}, as_("alice"))
        self.assertEqual(st, 409)
        self.assertEqual(body["error"]["code"], "stale_version")
        self.assertEqual(body["current"]["version"], 2)
        self.assertEqual(body["current"]["title"], "Test item")  # unchanged
        self.assertEqual(len(events(item["id"])), before)  # failed write left no history

    def test_idempotency_key_creates_one_item(self):
        payload = {"title": "Idempotent please", "team_id": team_id("Payments Ops")}
        h = {"Idempotency-Key": "key-123"}
        s1, b1, _ = call("POST", "/api/items", payload, as_("bob"), h)
        s2, b2, h2 = call("POST", "/api/items", payload, as_("bob"), h)
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(b1["id"], b2["id"])
        self.assertEqual(h2.get("Idempotent-Replay"), "true")
        found = call("GET", "/api/items?q=Idempotent%20please", token=as_("bob"))[1]["items"]
        self.assertEqual(len(found), 1)
        # same key, different body -> refused
        s3, b3, _ = call("POST", "/api/items", {**payload, "title": "Other"}, as_("bob"), h)
        self.assertEqual((s3, b3["error"]["code"]), (422, "idempotency_key_reuse"))

    def test_parallel_requests_same_key_create_one_item(self):
        payload = {"title": "Parallel dup", "team_id": team_id("Payments Ops")}
        out, barrier = [], threading.Barrier(4)

        def go():
            barrier.wait()
            out.append(call("POST", "/api/items", payload, as_("bob"), {"Idempotency-Key": "par-1"})[1]["id"])

        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(set(out)), 1)
        self.assertEqual(len(call("GET", "/api/items?q=Parallel%20dup", token=as_("bob"))[1]["items"]), 1)


class WorkflowAndAuthTests(unittest.TestCase):
    def test_invalid_jump_and_non_lead_approval_rejected(self):
        item = new_item()
        iid = item["id"]
        st, b, _ = call("POST", f"/api/items/{iid}/transition", {"to": "CLOSED"}, as_("bob"))
        self.assertEqual((st, b["error"]["code"]), (422, "invalid_transition"))
        call("POST", f"/api/items/{iid}/claim", {}, as_("bob"))
        self.assertEqual(call("POST", f"/api/items/{iid}/transition", {"to": "IN_PROGRESS"}, as_("bob"))[0], 200)
        self.assertEqual(call("POST", f"/api/items/{iid}/transition", {"to": "AWAITING_APPROVAL"}, as_("bob"))[0], 200)
        # bob (member) cannot approve; ivan (other team) cannot even see it; alice (lead) can
        self.assertEqual(call("POST", f"/api/items/{iid}/transition", {"to": "RESOLVED"}, as_("bob"))[0], 403)
        self.assertEqual(call("POST", f"/api/items/{iid}/transition", {"to": "RESOLVED"}, as_("ivan"))[0], 404)
        self.assertEqual(call("POST", f"/api/items/{iid}/transition", {"to": "RESOLVED"}, as_("alice"))[0], 200)
        self.assertEqual(events(iid).count("state_changed"), 3)

    def test_cannot_request_approval_without_assignee(self):
        item = new_item()
        call("POST", f"/api/items/{item['id']}/transition", {"to": "IN_PROGRESS"}, as_("alice"))
        st, b, _ = call("POST", f"/api/items/{item['id']}/transition", {"to": "AWAITING_APPROVAL"}, as_("alice"))
        self.assertEqual(st, 422)

    def test_outsider_cannot_read_or_write(self):
        item = new_item()
        iid = item["id"]
        self.assertEqual(call("GET", f"/api/items/{iid}", token=as_("ivan"))[0], 404)
        self.assertEqual(call("PATCH", f"/api/items/{iid}", {"version": 1, "title": "x"}, as_("ivan"))[0], 404)
        self.assertEqual(call("POST", f"/api/items/{iid}/claim", {}, as_("ivan"))[0], 404)
        self.assertEqual(call("GET", f"/api/items/{iid}/activity", token=as_("ivan"))[0], 404)
        st, _, _ = call("POST", "/api/items", {"title": "x", "team_id": team_id("Payments Ops")}, as_("ivan"))
        self.assertEqual(st, 403)
        ids = [i["id"] for i in call("GET", "/api/items?limit=100", token=as_("ivan"))[1]["items"]]
        self.assertNotIn(iid, ids)

    def test_viewer_is_read_only_and_member_cannot_reassign(self):
        item = new_item()
        iid = item["id"]
        self.assertEqual(call("GET", f"/api/items/{iid}", token=as_("gina"))[0], 200)
        self.assertEqual(call("POST", f"/api/items/{iid}/claim", {}, as_("gina"))[0], 403)
        self.assertEqual(call("POST", f"/api/items/{iid}/comments", {"body": "hi"}, as_("gina"))[0], 403)
        bob_id = call("GET", "/api/me", token=as_("bob"))[1]["user"]["id"]
        self.assertEqual(call("PATCH", f"/api/items/{iid}", {"version": 1, "assignee_id": bob_id}, as_("bob"))[0], 403)
        self.assertEqual(call("PATCH", f"/api/items/{iid}", {"version": 1, "assignee_id": bob_id}, as_("alice"))[0], 200)

    def test_history_is_append_only(self):
        item = new_item()
        raw = sqlite3.connect(os.environ["DB_PATH"])
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute("UPDATE activity_events SET type='x' WHERE item_id=?", (item["id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute("DELETE FROM activity_events WHERE item_id=?", (item["id"],))
        raw.close()

    def test_history_records_priority_and_owner_changes(self):
        item = new_item()
        call("PATCH", f"/api/items/{item['id']}", {"version": 1, "priority": "urgent"}, as_("bob"))
        call("POST", f"/api/items/{item['id']}/claim", {}, as_("bob"))
        call("POST", f"/api/items/{item['id']}/comments", {"body": "looking"}, as_("bob"))
        self.assertEqual(events(item["id"]), ["created", "priority_changed", "assignee_changed", "comment"])


class ListingTests(unittest.TestCase):
    def test_keyset_pagination_walks_every_item_once(self):
        total = db.connect().execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
        seen, cursor = [], None
        while True:
            url = "/api/items?limit=40" + (f"&cursor={cursor}" if cursor else "")
            body = call("GET", url, token=as_("admin"))[1]
            seen += [i["id"] for i in body["items"]]
            cursor = body["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertGreaterEqual(len(seen), total - 1000)  # concurrent tests may add items; never lose or repeat
        self.assertEqual(seen, sorted(seen, reverse=True))

    def test_filters_and_dashboard_views(self):
        item = new_item(priority="urgent")
        urgent = call("GET", "/api/items?view=unassigned_urgent&limit=100", token=as_("bob"))[1]["items"]
        self.assertTrue(all(i["priority"] == "urgent" and i["assignee_id"] is None for i in urgent))
        self.assertIn(item["id"], [i["id"] for i in urgent])
        counts = call("GET", "/api/dashboard", token=as_("bob"))[1]["counts"]
        self.assertEqual(set(counts), {"assigned_to_me", "unassigned_urgent", "awaiting_my_approval"})
        self.assertEqual(counts["awaiting_my_approval"], 0)  # bob is not a lead anywhere
        self.assertGreater(call("GET", "/api/dashboard", token=as_("alice"))[1]["counts"]["awaiting_my_approval"] + 1, 0)
        self.assertEqual(call("GET", "/api/items?status=NOPE", token=as_("bob"))[0], 400)
        self.assertEqual(call("GET", "/api/items?q=%25", token=as_("bob"))[1]["items"], [])  # '%' is escaped, not a wildcard


class OutboxTests(unittest.TestCase):
    def test_assignment_notification_is_effectively_once(self):
        bob_id = call("GET", "/api/me", token=as_("bob"))[1]["user"]["id"]
        item = new_item("alice", assignee_id=bob_id)  # lead assigns bob -> outbox row in same txn
        conn = db.connect()
        job = conn.execute("SELECT * FROM jobs WHERE payload LIKE ?", (f'%"item_id": {item["id"]},%',)).fetchone()
        self.assertEqual(job["status"], "pending")
        worker.process_due_jobs(conn)
        conn.execute("UPDATE jobs SET status='pending' WHERE id=?", (job["id"],))  # simulate redelivery / crash before ack
        worker.process_due_jobs(conn)
        n = conn.execute("SELECT COUNT(*) FROM notifications WHERE item_id=? AND user_id=?", (item["id"], bob_id)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(call("GET", "/api/notifications", token=as_("bob"))[1]["notifications"][0]["item_id"], item["id"])

    def test_failed_job_retries_with_backoff_then_marks_failed(self):
        def boom(conn, payload):
            raise RuntimeError("smtp down")
        worker.HANDLERS["always_fail"] = boom
        conn = db.connect()
        jid = conn.execute("INSERT INTO jobs(type,payload,max_attempts,run_at,created_at) VALUES ('always_fail','{}',3,0,'t')").lastrowid
        t = 1000.0
        worker.process_due_jobs(conn, now=t)
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        worker.process_due_jobs(conn, now=t)  # backoff: not due yet
        self.assertEqual(conn.execute("SELECT attempts FROM jobs WHERE id=?", (jid,)).fetchone()[0], 1)
        worker.process_due_jobs(conn, now=t + 10)
        worker.process_due_jobs(conn, now=t + 100)
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("failed", 3))
        self.assertIn("smtp down", row["last_error"])


if __name__ == "__main__":
    unittest.main()
