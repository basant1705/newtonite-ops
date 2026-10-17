"""Outbox worker: at-least-once delivery + idempotent handlers = effectively-once.

Jobs are written in the same transaction as the change that caused them.
Failures retry with exponential backoff, capped at max_attempts, then 'failed'.
"""
import json
import threading
import time

from . import db

STUCK_AFTER = 60  # seconds before a 'processing' job is considered crashed and re-queued


def handle_notify_assignee(conn, payload):
    item = conn.execute("SELECT title FROM work_items WHERE id=?", (payload["item_id"],)).fetchone()
    title = item["title"] if item else f"#{payload['item_id']}"
    # UNIQUE(dedupe_key) + INSERT OR IGNORE => running this twice creates ONE notification.
    conn.execute("INSERT OR IGNORE INTO notifications(user_id,item_id,message,dedupe_key,created_at) VALUES (?,?,?,?,?)",
                 (payload["user_id"], payload["item_id"], f"You were assigned: {title}",
                  f"assign:{payload['event_id']}", db.now()))


HANDLERS = {"notify_assignee": handle_notify_assignee}


def process_due_jobs(conn, now=None, limit=50):
    now = time.time() if now is None else now
    conn.execute("UPDATE jobs SET status='pending' WHERE status='processing' AND locked_at<?", (now - STUCK_AFTER,))
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE status='pending' AND run_at<=? ORDER BY id LIMIT ?", (now, limit))]
    done = 0
    for job_id in ids:
        # Claim atomically so two workers never run the same job.
        cur = conn.execute("UPDATE jobs SET status='processing', locked_at=?, attempts=attempts+1 "
                           "WHERE id=? AND status='pending'", (now, job_id))
        if cur.rowcount == 0:
            continue
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        try:
            HANDLERS[job["type"]](conn, json.loads(job["payload"]))
            conn.execute("UPDATE jobs SET status='done', last_error=NULL WHERE id=?", (job_id,))
            done += 1
        except Exception as exc:  # noqa: BLE001 - any handler failure is retried
            if job["attempts"] >= job["max_attempts"]:
                conn.execute("UPDATE jobs SET status='failed', last_error=? WHERE id=?", (repr(exc), job_id))
            else:
                delay = min(2 ** job["attempts"], 300)
                conn.execute("UPDATE jobs SET status='pending', run_at=?, last_error=? WHERE id=?",
                             (now + delay, repr(exc), job_id))
    return done


def run_forever(interval=1.0, stop: threading.Event = None):
    conn = db.connect()
    while not (stop and stop.is_set()):
        try:
            process_due_jobs(conn)
        except Exception as exc:  # noqa: BLE001
            print("worker error:", exc)
        time.sleep(interval)
