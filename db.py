import sqlite3
from contextlib import contextmanager
from pathlib import Path

from werkzeug.security import generate_password_hash

from config import ADMIN_EMAIL, ADMIN_FULL_NAME, ADMIN_PASSWORD, ADMIN_USERNAME, DATABASE_PATH, EMAIL_PASS, EMAIL_USER, IMAP_HOST, IMAP_MAILBOX
from services.security import encrypt_secret
from services.utils import ADMIN_DEFAULT_PERMISSIONS, iso_now, normalize_email, normalize_username

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    username TEXT NOT NULL,
    username_norm TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    status TEXT NOT NULL DEFAULT 'pending',
    legacy_telegram_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permissions (
    user_id INTEGER NOT NULL,
    permission_key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, permission_key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS email_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    email_norm TEXT NOT NULL UNIQUE,
    email_display TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_email_assignments_user ON email_assignments(user_id);
CREATE INDEX IF NOT EXISTS idx_email_assignments_expiry ON email_assignments(expiry_date);

CREATE TABLE IF NOT EXISTS search_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    email TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    result_text TEXT,
    fetch_time REAL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_search_history_created ON search_history(created_at);

CREATE TABLE IF NOT EXISTS renewal_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    old_expiry TEXT,
    new_expiry TEXT NOT NULL,
    action TEXT NOT NULL,
    changed_by INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (changed_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    actor_user_id INTEGER,
    meta_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_events_created ON activity_events(created_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imap_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    encrypted_password TEXT NOT NULL,
    imap_host TEXT NOT NULL,
    mailbox TEXT NOT NULL DEFAULT 'INBOX',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imap_accounts_enabled ON imap_accounts(enabled);
"""


def connect():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_cursor(commit=False):
    conn = connect()
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()


def init_db():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()
    seed_super_admin()
    seed_imap_account_from_env()


def seed_imap_account_from_env():
    if not EMAIL_USER or not EMAIL_PASS or not encrypt_secret(EMAIL_PASS):
        return
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM imap_accounts LIMIT 1").fetchone()
        if exists:
            return
        now = iso_now()
        conn.execute(
            "INSERT INTO imap_accounts (email, encrypted_password, imap_host, mailbox, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (EMAIL_USER, encrypt_secret(EMAIL_PASS), IMAP_HOST or "imap.gmail.com", IMAP_MAILBOX or "INBOX", now, now),
        )
        conn.commit()


def seed_super_admin():
    """Create the initial administrator without overwriting panel changes."""
    admin_username = (ADMIN_USERNAME or "admin").strip() or "admin"
    username_norm = normalize_username(admin_username) or "admin"
    now = iso_now()
    password_hash = generate_password_hash(ADMIN_PASSWORD or "admin123")

    with connect() as conn:
        existing = conn.execute("SELECT * FROM users WHERE role='super_admin' ORDER BY id LIMIT 1").fetchone()

        if existing:
            user_id = existing["id"]
            conn.execute("UPDATE users SET status='active', updated_at=? WHERE id=?", (now, user_id))

            for key in ADMIN_DEFAULT_PERMISSIONS:
                conn.execute(
                    "INSERT OR REPLACE INTO permissions (user_id, permission_key, enabled) VALUES (?, ?, 1)",
                    (user_id, key),
                )
            conn.commit()
            return user_id

        cur = conn.execute(
            """
            INSERT INTO users (full_name, username, username_norm, password_hash, role, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'super_admin', 'active', ?, ?)
            """,
            (ADMIN_FULL_NAME, admin_username, username_norm, password_hash, now, now),
        )
        user_id = cur.lastrowid
        for key in ADMIN_DEFAULT_PERMISSIONS:
            conn.execute(
                "INSERT OR REPLACE INTO permissions (user_id, permission_key, enabled) VALUES (?, ?, 1)",
                (user_id, key),
            )
        conn.commit()
        return user_id


def set_permissions(user_id, permission_keys, enabled=True):
    with connect() as conn:
        for key in permission_keys:
            conn.execute(
                "INSERT OR REPLACE INTO permissions (user_id, permission_key, enabled) VALUES (?, ?, ?)",
                (user_id, key, 1 if enabled else 0),
            )
        conn.commit()


def get_user(user_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_user_by_username(username_norm):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE username_norm=?", (username_norm,)).fetchone()


def get_super_admin():
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE role='super_admin' ORDER BY id LIMIT 1").fetchone()


def get_user_by_login(identifier):
    """Find a user by username, and let super admin login by email alias too."""
    raw = (identifier or "").strip()
    if not raw:
        return None

    raw_email = normalize_email(raw)
    username_norm = normalize_username(raw)

    admin_aliases = {
        normalize_email(ADMIN_EMAIL),
        normalize_email(ADMIN_USERNAME),
        normalize_username(ADMIN_EMAIL),
        normalize_username(ADMIN_USERNAME),
    }
    admin_aliases.discard("")

    if raw_email in admin_aliases or username_norm in admin_aliases:
        admin = get_super_admin()
        if admin:
            return admin

    if username_norm:
        user = get_user_by_username(username_norm)
        if user:
            return user

    return None


def user_has_permission(user, permission_key):
    if not user:
        return False
    if user["role"] in ("super_admin", "admin"):
        return True
    if user["status"] != "active":
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM permissions WHERE user_id=? AND permission_key=?",
            (user["id"], permission_key),
        ).fetchone()
    return bool(row and row["enabled"])


def is_admin(user):
    return bool(user and user["role"] in ("super_admin", "admin"))
