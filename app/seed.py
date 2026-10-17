"""Seed demo teams, users (one per role mix) and a few thousand items."""
import json
import random
from datetime import datetime, timedelta, timezone

from . import db

PASSWORD = "password123"
TEAMS = ["Payments Ops", "Customer Support", "Compliance", "Platform"]
# email, name, {team: role}
USERS = [
    ("admin@demo.test", "Ada Admin", {t: "admin" for t in TEAMS}),
    ("alice@demo.test", "Alice Lead", {"Payments Ops": "lead", "Customer Support": "member"}),
    ("bob@demo.test", "Bob Member", {"Payments Ops": "member"}),
    ("carol@demo.test", "Carol Lead", {"Customer Support": "lead"}),
    ("dave@demo.test", "Dave Member", {"Customer Support": "member", "Compliance": "viewer"}),
    ("erin@demo.test", "Erin Lead", {"Compliance": "lead"}),
    ("frank@demo.test", "Frank Member", {"Compliance": "member", "Platform": "member"}),
    ("gina@demo.test", "Gina Viewer", {"Payments Ops": "viewer"}),
    ("heidi@demo.test", "Heidi Lead", {"Platform": "lead"}),
    ("ivan@demo.test", "Ivan Member", {"Platform": "member"}),
]
PHRASES = {
    "incident": ["API latency spike", "Queue backlog growing", "Login failures in EU", "Disk almost full"],
    "customer_issue": ["Customer cannot reset password", "Wrong invoice amount", "Order stuck in review"],
    "payment": ["Settlement mismatch", "Refund not received", "Chargeback dispute", "Payout delayed"],
    "compliance": ["KYC document expired", "Audit evidence missing", "Policy exception request"],
    "task": ["Rotate credentials", "Update runbook", "Clean up stale accounts"],
}


def seed(conn, n_items=3000, rng_seed=42):
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    rng = random.Random(rng_seed)
    pw = db.hash_password(PASSWORD)  # same hash for all demo users: keeps seeding fast
    conn.execute("BEGIN IMMEDIATE")
    team_ids = {}
    for name in TEAMS:
        team_ids[name] = conn.execute("INSERT INTO teams(name) VALUES (?)", (name,)).lastrowid
    members = {t: [] for t in team_ids.values()}  # team_id -> [(user_id, role)]
    for email, name, roles in USERS:
        uid = conn.execute("INSERT INTO users(email,name,password_hash) VALUES (?,?,?)", (email, name, pw)).lastrowid
        for team, role in roles.items():
            conn.execute("INSERT INTO memberships VALUES (?,?,?)", (uid, team_ids[team], role))
            members[team_ids[team]].append((uid, role))
    start = conn.execute("SELECT COALESCE(MAX(id),0) FROM work_items").fetchone()[0]
    base = datetime.now(timezone.utc)
    items, events = [], []
    for n in range(1, n_items + 1):
        item_id = start + n
        team = rng.choice(list(team_ids.values()))
        workers = [u for u, r in members[team] if r != "viewer"]
        typ = rng.choice(list(PHRASES))
        status = rng.choices(["OPEN", "IN_PROGRESS", "AWAITING_APPROVAL", "RESOLVED", "CLOSED"], [30, 25, 10, 15, 20])[0]
        assignee = rng.choice(workers) if (status != "OPEN" or rng.random() < 0.4) else None
        creator = rng.choice(workers)
        ts = (base - timedelta(minutes=rng.randint(1, 60 * 24 * 90))).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        due = (base + timedelta(days=rng.randint(-5, 30))).strftime("%Y-%m-%d") if rng.random() < 0.6 else None
        title = f"{rng.choice(PHRASES[typ])} #{item_id}"
        items.append((item_id, title, "Seeded sample item.", typ,
                      rng.choices(["low", "medium", "high", "urgent"], [25, 40, 25, 10])[0],
                      status, team, assignee, creator, due, 1, ts, ts))
        events.append((item_id, creator, "created", json.dumps({"seeded": True}), ts))
    conn.executemany("INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", items)
    conn.executemany("INSERT INTO activity_events(item_id,actor_id,type,data,created_at) VALUES (?,?,?,?,?)", events)
    conn.execute("COMMIT")
