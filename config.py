import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def env_value(key, default=""):
    """Read Railway/local env safely and trim accidental quotes/spaces."""
    value = os.getenv(key, default)
    if value is None:
        return default
    value = str(value).strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1].strip()
    return value


BASE_DIR = Path(__file__).resolve().parent

# Railway volumes usually expose this variable. The app also works locally.
DATA_DIR = Path(env_value("RAILWAY_VOLUME_MOUNT_PATH") or env_value("DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_PATH = DATA_DIR / "web_email_access.sqlite3"

SECRET_KEY = env_value("SECRET_KEY") or env_value("FLASK_SECRET_KEY") or "change-this-secret-key"

# IMAP settings
EMAIL_USER = env_value("EMAIL_USER")
EMAIL_PASS = env_value("EMAIL_PASS")
IMAP_HOST = env_value("IMAP_HOST", "imap.gmail.com")
IMAP_MAILBOX = env_value("IMAP_MAILBOX", "inbox")
IMAP_ENCRYPTION_KEY = env_value("IMAP_ENCRYPTION_KEY")

# First super admin account. Change these in Railway variables.
ADMIN_USERNAME = env_value("WEB_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = env_value("WEB_ADMIN_PASSWORD", "admin123")
ADMIN_FULL_NAME = env_value("WEB_ADMIN_NAME", "Super Admin")
# Optional. If not set, EMAIL_USER also works as an admin login alias.
ADMIN_EMAIL = env_value("WEB_ADMIN_EMAIL") or EMAIL_USER

MAX_WORKERS = int(env_value("MAX_WORKERS", "5"))

APP_NAME = env_value("APP_NAME", "Netflix Email Access")
TIMEZONE = env_value("APP_TIMEZONE", "Asia/Kolkata")
