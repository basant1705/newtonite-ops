# Operations Under Pressure — Newtonite Ops

Internal web app to create, assign, track and resolve operational work items across teams.
Team-based roles (viewer / member / lead / admin), a server-enforced workflow with a Lead-only
approval step, an append-only activity history, a filtered dashboard, and a transactional-outbox worker.

**Stack:** Python 3.10+ standard library only (HTTP server, SQLite in WAL mode, unittest) + a single-file
vanilla-JS frontend. Zero installs, so it runs the same on any machine.

## Run

Requires Python 3.10 or newer. No third-party packages are required.

On Windows, double-click `run.bat`, or run the commands below.

```bash
python run.py                 # http://localhost:8000  (creates ops.db, seeds 4 teams, 10 users, 3000 items, starts worker)
# or
docker compose up --build
```

## Demo logins (password for all: `password123`)

| User | Roles |
|---|---|
| admin@demo.test | admin in all teams |
| alice@demo.test | lead in Payments Ops, member in Customer Support |
| bob@demo.test | member in Payments Ops |
| carol@demo.test | lead in Customer Support |
| dave@demo.test | member in Customer Support, viewer in Compliance |
| erin@demo.test | lead in Compliance |
| frank@demo.test | member in Compliance and Platform |
| gina@demo.test | viewer in Payments Ops |
| heidi@demo.test | lead in Platform |
| ivan@demo.test | member in Platform only (use him to see 404/403 on Payments items) |

## Tests

```bash
python -m unittest discover -s tests -v     # 14 tests, ~1 second, one command
```

Covers: concurrent claim (3 parallel claimers, exactly one wins), stale-version update rejected with data
unchanged, idempotency key (sequential + 4 parallel requests create one item), invalid workflow jump,
non-Lead approval, cross-team 403/404 on read and write, viewer is read-only, append-only history,
keyset pagination, dashboard views, outbox idempotency, retry/backoff/max-attempts.

## Workflow & permissions

`OPEN → IN_PROGRESS → AWAITING_APPROVAL → RESOLVED → CLOSED` (+ back-edges: IN_PROGRESS→OPEN,
AWAITING_APPROVAL→IN_PROGRESS (reject), RESOLVED→IN_PROGRESS (reopen)). Any other jump is a 422.

| Action | viewer | member | lead | admin |
|---|---|---|---|---|
| read item / activity | ✔ | ✔ | ✔ | ✔ |
| create, edit fields, comment, claim | | ✔ | ✔ | ✔ |
| move OPEN/IN_PROGRESS/request approval | | only if assignee | ✔ | ✔ |
| approve / reject / close / reopen | | | ✔ | ✔ |
| reassign others | | | ✔ | ✔ |

Roles are per team. Users outside an item's team get **404** (existence is not leaked); users inside the
team without the role get **403**. Every endpoint calls the single `authorize()` in `app/service.py`.

## API (all under `/api`, Bearer token)

`POST /login` · `GET /me` · `GET /dashboard` · `GET /items?view=&status=&priority=&type=&team=&assignee=me|none|<id>&q=&cursor=&limit=`
· `POST /items` · `GET|PATCH /items/:id` (PATCH needs `version`) · `POST /items/:id/claim|transition|comments`
· `GET /items/:id/activity` · `GET /teams/:id/members` · `GET /notifications`.
Send `Idempotency-Key: <uuid>` on any POST/PATCH to make retries safe.

## Known limitations

- SQLite instead of PostgreSQL (see ENGINEERING_DECISIONS.md #1): single-writer, one host.
- Search is `LIKE '%q%'` — fine for tens of thousands of rows, not millions (would move to FTS/Postgres `tsvector`).
- Multi-team list queries use `ORDER BY id` with a temp sort when several teams are selected.
- Tokens are random session ids in a table (no refresh/revocation UI); demo passwords share one hash.
- Items can't be deleted; no email delivery (the worker writes in-app notifications); no websockets (refetch only).
- A lead can approve their own item (no separation-of-duties rule yet).
- Frontend has no automated tests; the rollback logic was checked manually.

## Next week I would

Move to Postgres (`SELECT … FOR UPDATE`/`ON CONFLICT`), add FTS search, SLA/due-date escalation jobs,
separation of duties on approval, frontend tests for optimistic rollback, and an audit-log export.
