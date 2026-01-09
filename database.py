# database.py
import sqlite3
import threading
import datetime

# 1. 在这里添加新表的建表语句
INIT_SQL = [
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        role TEXT NOT NULL,
        first_seen TEXT,
        display_name TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        user_id INTEGER NOT NULL,
        chat_type TEXT NOT NULL,
        system_prompt TEXT,
        PRIMARY KEY (user_id, chat_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage (
        user_id INTEGER NOT NULL,
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, scope, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invitation_codes (
        code TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by INTEGER NOT NULL,
        used_at TEXT,
        used_by INTEGER,
        status TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        msg_uuid TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        ts TEXT NOT NULL,
        seq INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS system_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        action TEXT NOT NULL,
        target_id INTEGER,
        user_name TEXT,
        source TEXT,
        detail TEXT
    )
    """,
    # === 新增：用户模型偏好表 ===
    """
    CREATE TABLE IF NOT EXISTS user_model_prefs (
        user_id INTEGER PRIMARY KEY,
        model_name TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # === 新增：usage_totals 统计表 ===
    """
    CREATE TABLE IF NOT EXISTS usage_totals (
        user_id TEXT,
        model_name TEXT,
        msg_count INTEGER NOT NULL DEFAULT 0,
        token_count INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (user_id, model_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)",
    "CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_invitation_status ON invitation_codes(status)",
    "CREATE INDEX IF NOT EXISTS idx_chat_history_chat ON chat_history(chat_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_system_logs_ts ON system_logs(ts)",
    "CREATE INDEX IF NOT EXISTS idx_usage_totals_user ON usage_totals(user_id)"
]

class Database:
    def __init__(self, db_path):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self._lock:
            cur = self.conn.cursor()
            for stmt in INIT_SQL:
                cur.execute(stmt)
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()

    def execute(self, sql, params=()):
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(sql, params)
            self.conn.commit()
            return cur

    def query_one(self, sql, params=()):
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(sql, params)
            return cur.fetchone()

    def query_all(self, sql, params=()):
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(sql, params)
            return cur.fetchall()

    # Users
    def get_user(self, user_id):
        return self.query_one("SELECT * FROM users WHERE user_id=?", (user_id,))

    def upsert_user(self, user_id, role, display_name=None):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.execute(
            """
            INSERT INTO users (user_id, role, first_seen, display_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, display_name=excluded.display_name
            """,
            (user_id, role, now, display_name)
        )

    def update_display_name(self, user_id, display_name):
        self.execute("UPDATE users SET display_name=? WHERE user_id=?", (display_name, user_id))

    def delete_user(self, user_id):
        self.execute("DELETE FROM users WHERE user_id=?", (user_id,))

    def list_users(self):
        return self.query_all("SELECT * FROM users ORDER BY role DESC, user_id ASC")
    def list_super_admin_ids(self):
        rows = self.query_all("SELECT user_id FROM users WHERE role='super_admin'")
        return [r["user_id"] for r in rows]


    # Settings
    def set_prompt(self, user_id, chat_type, prompt):
        if prompt is None:
            self.execute("DELETE FROM settings WHERE user_id=? AND chat_type=?", (user_id, chat_type))
        else:
            self.execute(
                """
                INSERT INTO settings (user_id, chat_type, system_prompt)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, chat_type) DO UPDATE SET system_prompt=excluded.system_prompt
                """,
                (user_id, chat_type, prompt)
            )

    def get_prompt(self, user_id, chat_type):
        row = self.query_one(
            "SELECT system_prompt FROM settings WHERE user_id=? AND chat_type=?",
            (user_id, chat_type)
        )
        return row["system_prompt"] if row else None
    
    # === 新增：模型偏好 (Model Preferences) ===
    def get_user_model(self, user_id):
        row = self.query_one("SELECT model_name FROM user_model_prefs WHERE user_id=?", (user_id,))
        if row and row["model_name"]:
            return row["model_name"].strip()
        return None

    def set_user_model(self, user_id, model_name):
        try:
            if model_name is None or not str(model_name).strip():
                self.execute("DELETE FROM user_model_prefs WHERE user_id=?", (user_id,))
                return True
            model_name = str(model_name).strip()
            self.execute(
                """
                INSERT OR REPLACE INTO user_model_prefs (user_id, model_name, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (user_id, model_name)
            )
            return True
        except Exception as e:
            print(f"[DB Error] set_user_model: {e}")
            return False


    # Usage
    def get_usage(self, user_id, scope, key):
        row = self.query_one(
            "SELECT count FROM usage WHERE user_id=? AND scope=? AND key=?",
            (user_id, scope, key)
        )
        return row["count"] if row else 0

    def set_usage(self, user_id, scope, key, count):
        self.execute(
            """
            INSERT INTO usage (user_id, scope, key, count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, scope, key) DO UPDATE SET count=excluded.count
            """,
            (user_id, scope, key, count)
        )

    def cleanup_usage(self, user_id, scope, valid_keys):
        if not valid_keys:
            self.execute("DELETE FROM usage WHERE user_id=? AND scope=?", (user_id, scope))
            return
        placeholders = ",".join("?" for _ in valid_keys)
        params = (user_id, scope, *valid_keys)
        self.execute(
            f"DELETE FROM usage WHERE user_id=? AND scope=? AND key NOT IN ({placeholders})",
            params
        )

    # === 新增：usage_totals 统计 ===
    def incr_usage(self, user_id, model_name, msg_delta, token_delta, ts):
        self.execute(
            """
            INSERT INTO usage_totals (user_id, model_name, msg_count, token_count, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, model_name) DO UPDATE SET
              msg_count = msg_count + excluded.msg_count,
              token_count = token_count + excluded.token_count,
              updated_at = excluded.updated_at
            """,
            (user_id, model_name, msg_delta, token_delta, ts)
        )

    def get_usage_totals(self, user_id):
        return self.query_all(
            "SELECT model_name, msg_count, token_count FROM usage_totals WHERE user_id = ?",
            (user_id,)
        )

    def get_usage_total_all_models(self, user_id):
        row = self.query_one(
            "SELECT SUM(msg_count) AS msg_count, SUM(token_count) AS token_count FROM usage_totals WHERE user_id = ?",
            (user_id,)
        )
        return (row["msg_count"], row["token_count"]) if row else (0, 0)

    # Invitation codes
    def create_invitation_code(self, code, role, created_by):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.execute(
            """
            INSERT INTO invitation_codes (code, role, created_at, created_by, status)
            VALUES (?, ?, ?, ?, 'active')
            """,
            (code, role, now, created_by)
        )

    def consume_invitation_code(self, code, used_by):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                UPDATE invitation_codes
                SET status='used', used_at=?, used_by=?
                WHERE code=? AND status='active'
                RETURNING role
                """,
                (now, used_by, code)
            )
            row = cur.fetchone()
            self.conn.commit()
            return row["role"] if row else None


    def list_invitation_codes(self):
        return self.query_all(
            "SELECT code, role, created_at FROM invitation_codes WHERE status='active' ORDER BY created_at DESC"
        )

    def revoke_invitation_code(self, code):
        cur = self.execute(
            "UPDATE invitation_codes SET status='revoked' WHERE code=? AND status='active'",
            (code,)
        )
        return cur.rowcount > 0

    # Chat history
    def load_chat_history(self, chat_id):
        rows = self.query_all(
            "SELECT msg_uuid, role, content, ts FROM chat_history WHERE chat_id=? ORDER BY seq ASC",
            (chat_id,)
        )
        return [{"uuid": r["msg_uuid"], "role": r["role"], "content": r["content"], "ts": r["ts"]} for r in rows]

    def save_chat_history(self, chat_id, context):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self.conn.cursor()
            try:
                cur.execute("BEGIN")
                cur.execute("DELETE FROM chat_history WHERE chat_id=?", (chat_id,))
                for idx, msg in enumerate(context):
                    cur.execute(
                        """
                        INSERT INTO chat_history (chat_id, msg_uuid, role, content, ts, seq)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (chat_id, msg.get("uuid"), msg["role"], msg["content"], msg.get("ts", now), idx)
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise


    # System logs
    def add_system_log(self, action, target_id=None, user_name=None, source=None, detail=None):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.execute(
            """
            INSERT INTO system_logs (ts, action, target_id, user_name, source, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now, action, target_id, user_name, source, detail)
        )

    def get_recent_logs(self, limit=10):
        rows = self.query_all(
            "SELECT ts, action, target_id, user_name, source FROM system_logs ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        return rows
