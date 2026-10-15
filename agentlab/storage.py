import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "tasks.sqlite3"
        self.checkpoints = self.data_dir / "checkpoints.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, question TEXT NOT NULL, request TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    result TEXT, error TEXT, cancel_requested INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    node TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
                    time REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_task ON events(task_id,id);
                CREATE TABLE IF NOT EXISTS call_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    kind TEXT NOT NULL, time REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempts_task ON call_attempts(task_id);
            """)
            if "archived_at" not in {r[1] for r in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN archived_at REAL")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def create(self, task_id, request):
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO tasks(id,question,request,status,created,updated) VALUES(?,?,?,?,?,?)",
                       (task_id, request["question"], json.dumps(request, ensure_ascii=False), "queued", now, now))

    def update(self, task_id, status=None, result=None, error=None, cancel=None):
        fields, values = ["updated=?"], [time.time()]
        for key, value in [("status", status), ("result", json.dumps(result, ensure_ascii=False) if result is not None else None),
                           ("error", error), ("cancel_requested", cancel)]:
            if value is not None:
                fields.append(f"{key}=?")
                values.append(value)
        with self.connect() as db:
            db.execute(f"UPDATE tasks SET {','.join(fields)} WHERE id=?", [*values, task_id])

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["request"] = json.loads(result["request"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def list(self, owner_id=None, archived=False):
        with self.connect() as db:
            condition = "archived_at IS NOT NULL" if archived else "archived_at IS NULL"
            args = []
            if owner_id is not None:
                condition += " AND json_extract(request,'$.owner_id')=?"
                args.append(owner_id)
            return [dict(r) for r in db.execute(f"SELECT id,question,status,created,updated,error,archived_at FROM tasks WHERE {condition} ORDER BY created DESC LIMIT 200", args)]

    def delete(self, task_id):
        with self.connect() as db:
            cur = db.execute("UPDATE tasks SET archived_at=?,updated=? WHERE id=? AND archived_at IS NULL", (time.time(),time.time(),task_id))
        return {"deleted": cur.rowcount > 0, "archived": True, "recoverable": True, "checkpoint_cleaned": False}

    def restore(self, task_id):
        with self.connect() as db:
            return db.execute("UPDATE tasks SET archived_at=NULL,updated=? WHERE id=? AND archived_at IS NOT NULL", (time.time(),task_id)).rowcount > 0

    def _delete_checkpoint(self, task_id):
        """Delete checkpoint data. Returns True on success, False on failure."""
        try:
            with sqlite3.connect(self.checkpoints, timeout=15) as db:
                for table in ("writes", "checkpoints"):
                    try:
                        db.execute(f"DELETE FROM {table} WHERE thread_id=?", (task_id,))
                    except sqlite3.OperationalError as exc:
                        if "no such table" not in str(exc):
                            raise
                db.commit()
            return True
        except sqlite3.Error:
            return False

    def event(self, task_id, node, status, detail):
        with self.connect() as db:
            db.execute("INSERT INTO events(task_id,node,status,detail,time) VALUES(?,?,?,?,?)",
                       (task_id, node, status, str(detail)[:4000], time.time()))

    def events(self, task_id, after=0):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT 1000", (task_id, after))]

    def recover_interrupted(self):
        with self.connect() as db:
            db.execute("UPDATE tasks SET status='interrupted',error='服务中断；已保留最近检查点，可继续运行。' WHERE status IN ('queued','running')")

    def attempt(self, task_id, kind):
        with self.connect() as db:
            db.execute("INSERT INTO call_attempts(task_id,kind,time) VALUES(?,?,?)", (task_id, kind, time.time()))

    def attempt_counts(self, task_id):
        result = {"model": 0, "simulated": 0, "search": 0}
        with self.connect() as db:
            for row in db.execute("SELECT kind,COUNT(*) AS n FROM call_attempts WHERE task_id=? GROUP BY kind", (task_id,)):
                result[row["kind"]] = row["n"]
        return result

    def find_benchmark(self, external_id, fingerprint, owner_id=None):
        with self.connect() as db:
            if owner_id is not None:
                row = db.execute("SELECT id FROM tasks WHERE json_extract(request,'$.owner_id')=? AND json_extract(request,'$.external_task_id')=? AND json_extract(request,'$.request_fingerprint')=? ORDER BY created DESC LIMIT 1", (owner_id, external_id, fingerprint)).fetchone()
                return self.get(row[0]) if row else None
            rows = db.execute("SELECT id FROM tasks WHERE json_extract(request,'$.external_task_id')=? AND json_extract(request,'$.request_fingerprint')=? ORDER BY created DESC LIMIT 1", (external_id, fingerprint)).fetchall()
        return self.get(rows[0]["id"]) if rows else None
