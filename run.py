"""Start everything: init DB, seed if empty, run the outbox worker + HTTP server."""
import os
import threading

from app import db, seed, server, worker

if __name__ == "__main__":
    conn = db.connect()
    db.init_db(conn)
    seed.seed(conn, int(os.environ.get("SEED_ITEMS", "3000")))
    conn.close()
    threading.Thread(target=worker.run_forever, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print(f"Ops app running on http://localhost:{port}  (demo password: {seed.PASSWORD})")
    server.make_server("0.0.0.0", port).serve_forever()
