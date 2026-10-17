"""Business rules. Every endpoint goes through `authorize()` and every change
writes its activity event(s) + outbox job in the SAME transaction (the caller,
server.py, opens BEGIN IMMEDIATE ... COMMIT around each handler)."""
import json
import secrets
import time
from datetime import date

from . import db

TYPES = ("incident", "customer_issue", "payment", "compliance", "task")
PRIORITIES = ("low", "medium", "high", "urgent")
STATUSES = ("OPEN", "IN_PROGRESS", "AWAITING_APPROVAL", "RESOLVED", "CLOSED")
ROLE_RANK = {"viewer": 0, "member": 1, "lead": 2, "admin": 3}

# Allowed workflow edges -> minimum team role. Anything not listed is invalid.
TRANSITIONS = {
    ("OPEN", "IN_PROGRESS"): "member",
    ("IN_PROGRESS", "OPEN"): "member",
    ("IN_PROGRESS", "AWAITING_APPROVAL"): "member",   # request approval
    ("AWAITING_APPROVAL", "RESOLVED"): "lead",        # approve  (Lead/Admin of the item's team only)
    ("AWAITING_APPROVAL", "IN_PROGRESS"): "lead",     # reject
    ("RESOLVED", "CLOSED"): "lead",
    ("RESOLVED", "IN_PROGRESS"): "lead",              # reopen
}
# Role x action matrix (minimum role within the item's team).
ACTION_MIN_ROLE = {"read": "viewer", "comment": "member", "create": "member",
                   "edit": "member", "claim": "member", "reassign": "lead"}
EDITABLE = {"title", "description", "type", "priority", "due_date", "assignee_id"}
SESSION_TTL = 12 * 3600


class ApiError(Exception):
    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


class Ctx:
    def __init__(self, user, roles):
        self.user, self.roles = user, roles  # roles: {team_id: role}

    @property
    def uid(self):
        return self.user["id"]


# ---------------------------------------------------------------- auth
def login(conn, body):
    email, password = str(body.get("email", "")).lower().strip(), str(body.get("password", ""))
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    ok = db.verify_password(password, row["password_hash"] if row else "x$1$x$x")
    if not row or not ok:
        raise ApiError(401, "bad_credentials", "Invalid email or password")
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions VALUES (?,?,?)", (token, row["id"], time.time() + SESSION_TTL))
    return 200, {"token": token, "user": {"id": row["id"], "name": row["name"], "email": row["email"]}}


def authenticate(conn, token):
    if not token:
        return None
    row = conn.execute(
        "SELECT u.id, u.name, u.email FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token=? AND s.expires_at>?", (token, time.time())).fetchone()
    if not row:
        return None
    roles = {r["team_id"]: r["role"] for r in conn.execute(
        "SELECT team_id, role FROM memberships WHERE user_id=?", (row["id"],))}
    return Ctx(dict(row), roles)


def authorize(ctx, team_id, min_role, hide_outsider=True):
    """The single permission check. Outsiders get 404 on existing resources (no
    existence leak) and 403 when they try to act on a team they're not in."""
    role = ctx.roles.get(team_id)
    if role is None:
        if hide_outsider:
            raise ApiError(404, "not_found", "Not found")
        raise ApiError(403, "forbidden", "You are not a member of this team")
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        raise ApiError(403, "forbidden", f"This action requires the {min_role} role in this team")
    return role


# ---------------------------------------------------------------- helpers
ITEM_SELECT = (
    "SELECT w.*, t.name AS team_name, a.name AS assignee_name, c.name AS created_by_name "
    "FROM work_items w JOIN teams t ON t.id=w.team_id "
    "LEFT JOIN users a ON a.id=w.assignee_id JOIN users c ON c.id=w.created_by ")


def _can_transition(role, need, item, uid, to):
    if ROLE_RANK[role] < ROLE_RANK[need]:
        return False
    if need == "member" and ROLE_RANK[role] < ROLE_RANK["lead"] and item["assignee_id"] != uid:
        return False  # members may only move items assigned to them
    if to == "AWAITING_APPROVAL" and item["assignee_id"] is None:
        return False
    return True


def item_out(ctx, row):
    d = dict(row)
    role = ctx.roles.get(d["team_id"])
    d["my_role"] = role
    d["allowed_transitions"] = [to for (frm, to), need in TRANSITIONS.items()
                                if frm == d["status"] and role and _can_transition(role, need, d, ctx.uid, to)]
    return d


def _fetch(conn, item_id):
    return conn.execute(ITEM_SELECT + "WHERE w.id=?", (item_id,)).fetchone()


def _visible(conn, ctx, item_id):
    row = _fetch(conn, item_id)
    if row is None:
        raise ApiError(404, "not_found", "Not found")
    authorize(ctx, row["team_id"], ACTION_MIN_ROLE["read"])  # outsiders -> 404
    return row


def _conflict(conn, ctx, item_id, code, message):
    return ApiError(409, code, message, {"current": item_out(ctx, _fetch(conn, item_id))})


def _event(conn, item_id, actor_id, etype, data):
    return conn.execute("INSERT INTO activity_events(item_id,actor_id,type,data,created_at) VALUES (?,?,?,?,?)",
                        (item_id, actor_id, etype, json.dumps(data), db.now())).lastrowid


def _enqueue(conn, jtype, payload):
    conn.execute("INSERT INTO jobs(type,payload,run_at,created_at) VALUES (?,?,?,?)",
                 (jtype, json.dumps(payload), time.time(), db.now()))


def _int(v, name):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ApiError(400, "validation", f"{name} must be an integer")
    return v


def _check_field(key, v):
    if key == "title":
        if not isinstance(v, str) or not 1 <= len(v.strip()) <= 200:
            raise ApiError(400, "validation", "title must be 1-200 characters")
        return v.strip()
    if key == "description":
        if not isinstance(v, str) or len(v) > 5000:
            raise ApiError(400, "validation", "description must be a string up to 5000 characters")
        return v
    if key == "type":
        if v not in TYPES:
            raise ApiError(400, "validation", f"type must be one of {TYPES}")
        return v
    if key == "priority":
        if v not in PRIORITIES:
            raise ApiError(400, "validation", f"priority must be one of {PRIORITIES}")
        return v
    if key == "due_date":
        if v is None:
            return None
        try:
            return date.fromisoformat(v).isoformat()
        except (TypeError, ValueError):
            raise ApiError(400, "validation", "due_date must be YYYY-MM-DD or null")
    if key == "assignee_id":
        return None if v is None else _int(v, "assignee_id")


def _check_assignee(conn, assignee_id, team_id):
    row = conn.execute("SELECT role FROM memberships WHERE user_id=? AND team_id=?", (assignee_id, team_id)).fetchone()
    if not row or ROLE_RANK[row["role"]] < ROLE_RANK["member"]:
        raise ApiError(400, "validation", "assignee must be a non-viewer member of the item's team")


def _notify_assignment(conn, item_id, assignee_id, actor_id, event_id):
    if assignee_id is not None and assignee_id != actor_id:
        _enqueue(conn, "notify_assignee", {"item_id": item_id, "user_id": assignee_id, "event_id": event_id})


# ---------------------------------------------------------------- items
def create_item(conn, ctx, body):
    team_id = _int(body.get("team_id"), "team_id")
    authorize(ctx, team_id, ACTION_MIN_ROLE["create"], hide_outsider=False)
    title = _check_field("title", body.get("title"))
    description = _check_field("description", body.get("description", ""))
    typ = _check_field("type", body.get("type", "task"))
    priority = _check_field("priority", body.get("priority", "medium"))
    due = _check_field("due_date", body.get("due_date"))
    assignee = _check_field("assignee_id", body.get("assignee_id"))
    if assignee is not None:
        if assignee != ctx.uid:
            authorize(ctx, team_id, ACTION_MIN_ROLE["reassign"], hide_outsider=False)
        _check_assignee(conn, assignee, team_id)
    ts = db.now()
    item_id = conn.execute(
        "INSERT INTO work_items(title,description,type,priority,status,team_id,assignee_id,created_by,due_date,"
        "version,created_at,updated_at) VALUES (?,?,?,?, 'OPEN',?,?,?,?,1,?,?)",
        (title, description, typ, priority, team_id, assignee, ctx.uid, due, ts, ts)).lastrowid
    _event(conn, item_id, ctx.uid, "created", {"title": title, "priority": priority, "type": typ})
    if assignee is not None:
        ev = _event(conn, item_id, ctx.uid, "assignee_changed", {"from": None, "to": assignee})
        _notify_assignment(conn, item_id, assignee, ctx.uid, ev)
    return 201, item_out(ctx, _fetch(conn, item_id))


def get_item(conn, ctx, item_id):
    return 200, item_out(ctx, _visible(conn, ctx, item_id))


def update_item(conn, ctx, item_id, body):
    item = _visible(conn, ctx, item_id)
    authorize(ctx, item["team_id"], ACTION_MIN_ROLE["edit"])
    version = _int(body.get("version"), "version")
    unknown = set(body) - EDITABLE - {"version"}
    if unknown:
        raise ApiError(400, "validation", f"cannot edit: {sorted(unknown)} (use /transition for status)")
    changes = {}
    for key in EDITABLE & set(body):
        new = _check_field(key, body[key])
        if new != item[key]:
            changes[key] = new
    if "assignee_id" in changes:
        authorize(ctx, item["team_id"], ACTION_MIN_ROLE["reassign"])
        if changes["assignee_id"] is not None:
            _check_assignee(conn, changes["assignee_id"], item["team_id"])
    if version != item["version"]:
        raise _conflict(conn, ctx, item_id, "stale_version", "This item changed since you loaded it")
    if not changes:
        return 200, item_out(ctx, item)
    sets = ", ".join(f"{k}=?" for k in changes)  # keys come from the EDITABLE whitelist
    cur = conn.execute(f"UPDATE work_items SET {sets}, version=version+1, updated_at=? WHERE id=? AND version=?",
                       (*changes.values(), db.now(), item_id, version))
    if cur.rowcount == 0:
        raise _conflict(conn, ctx, item_id, "stale_version", "This item changed since you loaded it")
    plain = {}
    for key, new in changes.items():
        if key == "priority":
            _event(conn, item_id, ctx.uid, "priority_changed", {"from": item["priority"], "to": new})
        elif key == "assignee_id":
            ev = _event(conn, item_id, ctx.uid, "assignee_changed", {"from": item["assignee_id"], "to": new})
            _notify_assignment(conn, item_id, new, ctx.uid, ev)
        else:
            plain[key] = {"from": item[key], "to": new}
    if plain:
        _event(conn, item_id, ctx.uid, "edited", {"fields": plain})
    return 200, item_out(ctx, _fetch(conn, item_id))


def claim_item(conn, ctx, item_id, body):
    item = _visible(conn, ctx, item_id)
    authorize(ctx, item["team_id"], ACTION_MIN_ROLE["claim"])
    if item["status"] in ("RESOLVED", "CLOSED"):
        raise ApiError(422, "invalid_state", "Resolved or closed items cannot be claimed")
    cur = conn.execute("UPDATE work_items SET assignee_id=?, version=version+1, updated_at=? "
                       "WHERE id=? AND assignee_id IS NULL", (ctx.uid, db.now(), item_id))
    if cur.rowcount == 0:
        raise _conflict(conn, ctx, item_id, "already_claimed", "Someone else already claimed this item")
    _event(conn, item_id, ctx.uid, "assignee_changed", {"from": None, "to": ctx.uid, "via": "claim"})
    return 200, item_out(ctx, _fetch(conn, item_id))


def transition_item(conn, ctx, item_id, body):
    item = _visible(conn, ctx, item_id)
    to = body.get("to")
    if to not in STATUSES:
        raise ApiError(400, "validation", f"'to' must be one of {STATUSES}")
    need = TRANSITIONS.get((item["status"], to))
    if need is None:
        allowed = [t for (f, t) in TRANSITIONS if f == item["status"]]
        raise ApiError(422, "invalid_transition", f"Cannot move {item['status']} -> {to}. Allowed: {allowed}")
    role = authorize(ctx, item["team_id"], need)
    if need == "member" and ROLE_RANK[role] < ROLE_RANK["lead"] and item["assignee_id"] != ctx.uid:
        raise ApiError(403, "forbidden", "Only the assignee or a lead can move this item")
    if to == "AWAITING_APPROVAL" and item["assignee_id"] is None:
        raise ApiError(422, "invalid_transition", "Assign the item before requesting approval")
    expected = body.get("version")
    if expected is not None and _int(expected, "version") != item["version"]:
        raise _conflict(conn, ctx, item_id, "stale_version", "This item changed since you loaded it")
    cur = conn.execute("UPDATE work_items SET status=?, version=version+1, updated_at=? WHERE id=? AND status=?",
                       (to, db.now(), item_id, item["status"]))
    if cur.rowcount == 0:
        raise _conflict(conn, ctx, item_id, "stale_state", "The item's status changed concurrently")
    _event(conn, item_id, ctx.uid, "state_changed", {"from": item["status"], "to": to})
    return 200, item_out(ctx, _fetch(conn, item_id))


def add_comment(conn, ctx, item_id, body):
    item = _visible(conn, ctx, item_id)
    authorize(ctx, item["team_id"], ACTION_MIN_ROLE["comment"])
    text = body.get("body")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
        raise ApiError(400, "validation", "comment must be 1-2000 characters")
    ev = _event(conn, item_id, ctx.uid, "comment", {"body": text.strip()})
    return 201, {"id": ev, "type": "comment", "item_id": item_id}


def get_activity(conn, ctx, item_id, q):
    item = _visible(conn, ctx, item_id)
    authorize(ctx, item["team_id"], ACTION_MIN_ROLE["read"])
    after = int(q.get("after", 0) or 0)
    limit = min(max(int(q.get("limit", 50) or 50), 1), 200)
    rows = conn.execute(
        "SELECT e.id, e.type, e.data, e.created_at, e.actor_id, u.name AS actor_name FROM activity_events e "
        "JOIN users u ON u.id=e.actor_id WHERE e.item_id=? AND e.id>? ORDER BY e.id LIMIT ?",
        (item_id, after, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    events = [{**dict(r), "data": json.loads(r["data"])} for r in rows]
    return 200, {"events": events, "next_after": events[-1]["id"] if more else None}


# ---------------------------------------------------------------- listing
def _where(ctx, q):
    """Build the server-side filter. Team scoping is ALWAYS applied first."""
    if not ctx.roles:
        return None, None
    teams = list(ctx.roles)
    if q.get("team"):
        tid = int(q["team"])
        if tid not in ctx.roles:
            raise ApiError(404, "not_found", "Not found")
        teams = [tid]
    where, args = [f"w.team_id IN ({','.join('?' * len(teams))})"], list(teams)
    for key, allowed in (("status", STATUSES), ("priority", PRIORITIES), ("type", TYPES)):
        if q.get(key):
            if q[key] not in allowed:
                raise ApiError(400, "validation", f"{key} must be one of {allowed}")
            where.append(f"w.{key}=?")
            args.append(q[key])
    a = q.get("assignee")
    if a == "me":
        where.append("w.assignee_id=?"); args.append(ctx.uid)
    elif a == "none":
        where.append("w.assignee_id IS NULL")
    elif a:
        where.append("w.assignee_id=?"); args.append(int(a))
    if q.get("q"):
        like = "%" + q["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(w.title LIKE ? ESCAPE '\\' OR w.description LIKE ? ESCAPE '\\')")
        args += [like, like]
    view = q.get("view")
    if view == "assigned_to_me":
        where.append("w.assignee_id=? AND w.status NOT IN ('RESOLVED','CLOSED')"); args.append(ctx.uid)
    elif view == "unassigned_urgent":
        where.append("w.assignee_id IS NULL AND w.priority='urgent' AND w.status IN ('OPEN','IN_PROGRESS')")
    elif view == "awaiting_my_approval":
        leads = [t for t in teams if ROLE_RANK[ctx.roles[t]] >= ROLE_RANK["lead"]]
        if not leads:
            return None, None
        where.append(f"w.status='AWAITING_APPROVAL' AND w.team_id IN ({','.join('?' * len(leads))})")
        args += leads
    elif view:
        raise ApiError(400, "validation", "unknown view")
    return " AND ".join(where), args


def list_items(conn, ctx, q):
    try:
        limit = min(max(int(q.get("limit", 25)), 1), 100)
        cursor = int(q["cursor"]) if q.get("cursor") else None
    except ValueError:
        raise ApiError(400, "validation", "limit/cursor must be integers")
    where, args = _where(ctx, q)
    if where is None:
        return 200, {"items": [], "next_cursor": None}
    if cursor is not None:  # keyset pagination on the primary key: O(log n) at any depth
        where += " AND w.id<?"
        args.append(cursor)
    rows = conn.execute(ITEM_SELECT + f"WHERE {where} ORDER BY w.id DESC LIMIT ?", (*args, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return 200, {"items": [item_out(ctx, r) for r in rows], "next_cursor": rows[-1]["id"] if more else None}


def dashboard(conn, ctx, q):
    counts = {}
    for view in ("assigned_to_me", "unassigned_urgent", "awaiting_my_approval"):
        where, args = _where(ctx, {"view": view})
        counts[view] = 0 if where is None else conn.execute(
            f"SELECT COUNT(*) FROM work_items w WHERE {where}", args).fetchone()[0]
    return 200, {"counts": counts}


def me(conn, ctx, q):
    teams = conn.execute("SELECT t.id, t.name, m.role FROM memberships m JOIN teams t ON t.id=m.team_id "
                         "WHERE m.user_id=? ORDER BY t.id", (ctx.uid,)).fetchall()
    return 200, {"user": ctx.user, "teams": [dict(t) for t in teams]}


def team_members(conn, ctx, team_id):
    authorize(ctx, team_id, "viewer")
    rows = conn.execute("SELECT u.id, u.name, m.role FROM memberships m JOIN users u ON u.id=m.user_id "
                        "WHERE m.team_id=? ORDER BY u.name", (team_id,)).fetchall()
    return 200, {"members": [dict(r) for r in rows]}


def my_notifications(conn, ctx, q):
    rows = conn.execute("SELECT id,item_id,message,created_at FROM notifications WHERE user_id=? "
                        "ORDER BY id DESC LIMIT 20", (ctx.uid,)).fetchall()
    return 200, {"notifications": [dict(r) for r in rows]}
