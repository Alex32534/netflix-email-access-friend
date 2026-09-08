import re
import json
from datetime import datetime, date, timedelta
import pytz
from config import TIMEZONE

IST = pytz.timezone(TIMEZONE)
DATE_FORMAT = "%Y-%m-%d"

CATEGORIES = {
    "household": {
        "label": "Household Code",
        "icon": "🏠",
        "permission": "search_household",
        "window_minutes": 15,
    },
    "reset": {
        "label": "Reset Password",
        "icon": "🔑",
        "permission": "search_reset",
        "window_hours": 24,
    },
    "login_code": {
        "label": "Sign-in Code",
        "icon": "🔢",
        "permission": "search_login_code",
        "window_minutes": 15,
    },
    "verification_code": {
        "label": "Verification Code",
        "icon": "🔐",
        "permission": "search_verification_code",
        "window_minutes": 10,
        "admin_only_by_default": True,
        "dangerous_for_users": True,
    },
    "verification_code_after_login": {
        "label": "Verification Code",
        "icon": "🛡️",
        "permission": "search_verification_code_after_login",
        "window_minutes": 15,
        "auto_for_users": True,
    },
    "verify_email": {
        "label": "Verify Email",
        "icon": "✅",
        "permission": "search_verify_email",
        "window_hours": 48,
    },
    "tv_login": {
        "label": "TV Login",
        "icon": "📺",
        "permission": "search_tv_login",
        "window_minutes": 15,
    },
}

# Keep permissions exactly aligned with the original bot categories.
# Admin/admin-panel abilities are controlled by role only, not toggles.
PERMISSIONS = {
    "search_household": "Household",
    "search_reset": "Reset",
    "search_login_code": "Login Code",
    "search_verification_code": "Verification Code",
    "search_verification_code_after_login": "Verification Code After Login",
    "search_verify_email": "Verify Email",
    "search_tv_login": "TV Login",
}

# Admins bypass category permissions through user_has_permission(); we do not
# store fake dashboard/log/user-management permissions anymore.
ADMIN_DEFAULT_PERMISSIONS = []
# Normal users get every normal bot category automatically, EXCEPT the risky
# old Verification Code. That one must be enabled manually by admin after a warning.
USER_DEFAULT_PERMISSIONS = [
    key for key in PERMISSIONS.keys()
    if key != "search_verification_code"
]

APPROVED_URL_PREFIXES = [
    "https://www.netflix.com/account/travel/",
    "https://www.netflix.com/account/update-primary-location?",
    "https://www.netflix.com/account/confirmdevice?",
    "https://www.netflix.com/password?",
    "https://www.netflix.com/verifyemail",
    "https://www.netflix.com/ilum?",
]

def now_ist():
    return datetime.now(IST)

def iso_now():
    return now_ist().isoformat(timespec="seconds")

def today_ist():
    return now_ist().date()

def tomorrow_str():
    return (today_ist() + timedelta(days=1)).strftime(DATE_FORMAT)

def normalize_username(username: str) -> str:
    username = (username or "").strip().lower()
    username = re.sub(r"[^a-z0-9_.-]", "", username)
    username = re.sub(r"[_.-]{2,}", "_", username)
    return username[:40]

def normalize_email(value: str) -> str:
    return (value or "").strip().lower()

def parse_emails(input_text: str):
    emails = []
    seen = set()
    for part in re.split(r"[,\n\s;]+", input_text or ""):
        cleaned = normalize_email(part)
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cleaned) and cleaned not in seen:
            emails.append(cleaned)
            seen.add(cleaned)
    return emails

def is_valid_date(value: str) -> bool:
    try:
        datetime.strptime(value, DATE_FORMAT)
        return True
    except Exception:
        return False

def parse_date(value: str):
    return datetime.strptime(value, DATE_FORMAT).date()

def normalize_date(value: str):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in (DATE_FORMAT, "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date().strftime(DATE_FORMAT)
        except Exception:
            pass
    return None

def clean_url(url: str) -> str:
    return re.sub(r"[)\]>\"']+$", "", url or "")

def clean_urls(text: str) -> str:
    url_pattern = r"https?://[^\s)\]'>\"]+"
    for url in re.findall(url_pattern, text or ""):
        if not any(url.startswith(prefix) for prefix in APPROVED_URL_PREFIXES):
            text = text.replace(url, "[LINK REMOVED]")
    return text

def json_dumps(data):
    return json.dumps(data or {}, ensure_ascii=False)


def parse_email_date_lines(input_text: str):
    """
    Parse lines like:
    user@example.com 2026-07-15
    user@example.com 15-07-2026
    user@example.com -> 15/07/2026
    Returns ([(email, normalized_date), ...], invalid_lines)
    """
    updates = []
    invalid = []
    for raw_line in (input_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        emails = parse_emails(line)
        if not emails:
            invalid.append(raw_line)
            continue
        # remove email text and separators, then search for date-ish tokens
        email_value = emails[0]
        remainder = re.sub(re.escape(email_value), " ", line, count=1, flags=re.IGNORECASE)
        candidates = re.findall(r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4})\b", remainder)
        normalized = None
        for candidate in candidates:
            normalized = normalize_date(candidate)
            if normalized:
                break
        if normalized:
            updates.append((email_value, normalized))
        else:
            invalid.append(raw_line)
    return updates, invalid


def parse_replacement_pairs(input_text: str):
    """
    Parse replacement lines like:
    old@example.com new@example.com
    old@example.com -> new@example.com
    old@example.com,new@example.com
    Returns ([(old, new), ...], invalid_lines)
    """
    pairs = []
    invalid = []
    seen = set()
    for raw_line in (input_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        emails = parse_emails(line)
        if len(emails) < 2:
            invalid.append(raw_line)
            continue
        pair = (emails[0], emails[1])
        if pair[0] == pair[1]:
            invalid.append(raw_line)
            continue
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs, invalid
