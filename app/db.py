"""Database layer: connection, schema, password hashing.

SQLite (stdlib) is used so the project runs with zero installs. Every request
opens its own connection; writes run inside BEGIN IMMEDIATE so they are
serialised, and each handler still uses conditional UPDATEs (WHERE version=?,
WHERE assignee_id IS NULL) so the same code is correct on PostgreSQL too.
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone


def db_path():
    return os.environ.get("DB_PATH", "ops.db")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect():
    conn = sqlite3.connect(db_path(), timeout=10, isolation_level=None)  # autocommit; we BEGIN explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS teams (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS memberships (
  user_id INTEGER NOT NULL REFERENCES users(id),
  team_id INTEGER NOT NULL REFERENCES teams(id),
  role TEXT NOT NULL CHECK (role IN ('viewer','member','lead','admin')),
  PRIMARY KEY (user_id, team_id));
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS work_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL CHECK (type IN ('incident','customer_issue','payment','compliance','task')),
  priority TEXT NOT NULL CHECK (priority IN ('low','medium','high','urgent')),
  status TEXT NOT NULL CHECK (status IN ('OPEN','IN_PROGRESS','AWAITING_APPROVAL','RESOLVED','CLOSED')),
  team_id INTEGER NOT NULL REFERENCES teams(id),
  assignee_id INTEGER REFERENCES users(id),
  created_by INTEGER NOT NULL REFERENCES users(id),
  due_date TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_items_team_status ON work_items(team_id, status, id);
CREATE INDEX IF NOT EXISTS idx_items_assignee ON work_items(assignee_id, status, id);
CREATE INDEX IF NOT EXISTS idx_items_unassigned_urgent ON work_items(team_id, id)
  WHERE assignee_id IS NULL AND priority = 'urgent';
CREATE INDEX IF NOT EXISTS idx_items_awaiting ON work_items(team_id, id) WHERE status = 'AWAITING_APPROVAL';

-- Append-only history. Comments are events of type 'comment'.
CREATE TABLE IF NOT EXISTS activity_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES work_items(id),
  actor_id INTEGER NOT NULL REFERENCES users(id),
  type TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_activity_item ON activity_events(item_id, id);
CREATE TRIGGER IF NOT EXISTS activity_no_update BEFORE UPDATE ON activity_events
  BEGIN SELECT RAISE(ABORT, 'activity_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS activity_no_delete BEFORE DELETE ON activity_events
  BEGIN SELECT RAISE(ABORT, 'activity_events is append-only'); END;

CREATE TABLE IF NOT EXISTS idempotency_keys (
  key TEXT NOT NULL, user_id INTEGER NOT NULL, fingerprint TEXT NOT NULL,
  status_code INTEGER NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (key, user_id));

-- Transactional outbox + worker bookkeeping.
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL, payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','failed')),
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5,
  run_at REAL NOT NULL, locked_at REAL, last_error TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id), item_id INTEGER NOT NULL,
  message TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
"""


def init_db(conn):
    conn.executescript(SCHEMA)


def hash_password(password, salt=None, iterations=100_000):
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2${iterations}${salt}${digest}"


def verify_password(password, stored):
    try:
        _, iters, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters)).hex()
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False
