# Engineering decisions

## 1. SQLite (stdlib) instead of PostgreSQL
**Context:** the reviewer must run the project on a fresh machine; I also needed to run real concurrent
tests. **Options:** Postgres + Docker; SQLite. **Choice:** SQLite in WAL mode with `BEGIN IMMEDIATE` per write.
**Trade-off:** one writer at a time and a single host, but zero setup and genuinely atomic transactions. Every
concurrency rule is written as a *conditional UPDATE* (not "read then write in Python"), so the same SQL is
correct under Postgres row locks; only the connection layer would change.

## 2. Data model & history: events written in the same transaction
**Context:** history must never disagree with the item. **Choice:** `activity_events` is append-only
(DB triggers reject UPDATE/DELETE) and each handler inserts the event in the same transaction as the change;
if the 409/403 happens, the whole transaction rolls back, so failed requests leave no history. Comments are
events too (one timeline). **Trade-off:** one table to query and page, but event `data` is JSON, so it's
not relationally queryable.

## 3. Concurrency control: three different tools for three problems
- *Two people claim one item:* `UPDATE … SET assignee_id=? WHERE id=? AND assignee_id IS NULL`; 0 rows → 409.
- *Stale edits:* optimistic locking with `version`; `PATCH` requires it; `WHERE version=?`; mismatch → 409 with
  the latest item so the UI can recover. Transitions also guard on `status=<from>`.
- *Retries/double-clicks:* `Idempotency-Key` stored (key + user + request fingerprint + response) in the same
  transaction as the work; a replay returns the stored response; same key with a different body → 422.
**Trade-off:** clients must send `version`/keys; in exchange there are no lost updates or duplicates.

## 4. Authorization: one function, per-team roles, 404 vs 403
`authorize(ctx, team_id, min_role)` is the only permission check. Outsiders get 404 (don't leak that an item
exists); insiders lacking the role get 403. Approval requires lead/admin **of the item's team**; members can only
move items assigned to them. List queries are scoped to the caller's teams in SQL, never filtered in the browser.
**Trade-off:** roles are coarse (no per-item ACLs, no separation of duties yet).

## 5. Sync vs async: transactional outbox
Assignment notifications are written to `jobs` in the same transaction as the assignment, so a crash can't
lose or invent them. The worker claims jobs atomically, retries with exponential backoff, stops at `max_attempts`
(status `failed`), re-queues jobs stuck in `processing`, and the handler is idempotent (`UNIQUE(dedupe_key)` +
`INSERT OR IGNORE`). Delivery is at-least-once; idempotent handlers make the effect once. **Trade-off:** polling
latency (≤1s) instead of push.

## 6. Search & pagination
Keyset pagination (`id < cursor ORDER BY id DESC`) stays fast at any depth, unlike OFFSET. Filters run in SQL,
with composite and partial indexes for the dashboard views (unassigned-urgent, awaiting-approval). Search is
`LIKE` with escaping — simple and correct, but would move to full-text search at larger scale.

## What I chose not to build
Sign-up, email, attachments, SSO, websockets, per-item ACLs, soft-delete, rate limiting.

## At 10× scale
Postgres + read replicas, partition `activity_events` by time, FTS/OpenSearch for search, a real queue
(SQS/BullMQ) instead of table polling, and websockets/SSE for live updates.
