import concurrent.futures
import email
import html
import imaplib
import logging
import re
import time
from datetime import datetime, timedelta
from email.header import decode_header, make_header

import pytz

from config import MAX_WORKERS
from db import connect
from services.security import decrypt_secret
from services.utils import CATEGORIES, clean_url, clean_urls

logger = logging.getLogger(__name__)
UTC = pytz.utc
INDIA = pytz.timezone("Asia/Kolkata")
INDO = pytz.timezone("Asia/Jakarta")


def _to_header_addresses(message):
    """Return normalized addresses from the RFC To header only."""
    raw_to = message.get("To", "")
    if not raw_to:
        return set()

    try:
        decoded_to = str(make_header(decode_header(raw_to)))
    except (TypeError, ValueError):
        decoded_to = str(raw_to)

    return {
        address.strip().lower()
        for _display_name, address in email.utils.getaddresses([decoded_to])
        if address and "@" in address
    }


def _to_header_matches(message, receiver_email):
    """Validate the requested mailbox against only the message To header."""
    receiver = str(receiver_email).strip().lower()
    return bool(receiver) and receiver in _to_header_addresses(message)


def extract_login_code(text, subject=None):
    """
    Extract 4-digit Netflix login code from email.
    Priority:
      1. Subject line (Netflix often places code there)
      2. Body: code near keywords like code, verification, login, sign in, access
      3. Fallback: any 4-digit number if email mentions 'netflix' (weak)
    Avoids false positives like 8199 from forwarding headers.
    """
    # 1) Check subject
    if subject:
        matches = re.findall(r"(?<!\d)(\d{4})(?!\d)", subject)
        for match in matches:
            if match not in ["2023", "2024", "2025", "2026", "2027"]:
                return match

    if not text:
        return None

    # 2) Context-aware: code near relevant keywords
    pattern = r"(?i)(?:code|verification|login|sign in|access)\s*(?:is|:)?\s*(\d{4})(?!\d)"
    matches = re.findall(pattern, text)
    for match in matches:
        if match not in ["2023", "2024", "2025", "2026", "2027"]:
            return match

    # 3) Weak fallback: only if the email is definitely from Netflix
    if "netflix" in text.lower():
        matches = re.findall(r"(?<!\d)(\d{4})(?!\d)", text)
        for match in matches:
            if match not in ["2023", "2024", "2025", "2026", "2027"]:
                return match

    return None


def extract_verification_code(text):
    match = re.search(r"\b(\d{6})\b", text or "")
    return match.group(1) if match else None


_POST_LOGIN_VERIFICATION_BODIES = (
    "Someone is trying to access your account. If you recognise this request, enter this code to confirm. You'll have 15 minutes before this code expires.",
    "Seseorang mencoba mengakses akunmu. Jika kamu mengenali permintaan ini, masukkan kode ini untuk mengonfirmasi. Kode ini akan kedaluwarsa dalam 15 menit.",
    "มีคนพยายามเข้าใช้บัญชีของคุณ หากจำคำขอนี้ได้ ให้ป้อนรหัสนี้เพื่อยืนยัน รหัสดังกล่าวจะหมดอายุใน 15 นาที",
    "Seseorang cuba mengakses akaun anda. Jika anda mengenali permintaan ini, masukkan kod ini untuk membuat pengesahan. Kod ini akan tamat tempoh dalam masa 15 minit.",
)


def is_verification_code_after_login(subject, body):
    """Return whether an email uses the specific post-login template."""
    text = re.sub(r"\s+", " ", f"{subject} {body}").strip().lower()
    return any(marker.lower() in text for marker in _POST_LOGIN_VERIFICATION_BODIES)


def extract_verification_code_after_login(subject, body):
    """Extract a code only after the post-login template has been identified."""
    if not is_verification_code_after_login(subject, body):
        return None
    matches = re.findall(r"(?<!\d)(\d{6})(?!\d)", body or "")
    return matches[0] if matches else None


def extract_reset_link(text):
    if not text:
        return None
    text = text.replace('\n', '').replace('\r', '')
    # Prefer link with lkid=URL_CTA (the correct reset link)
    match = re.search(r"(https://www\.netflix\.com/password\?[^\s>\"'\)\]]*lkid=URL_CTA[^\s>\"'\)\]]*)", text)
    if not match:
        # Fallback to any password reset link
        match = re.search(r"(https://www\.netflix\.com/password\?[^\s>\"'\)\]]+)", text)
    return clean_url(match.group(1)) if match else None


def extract_household_links(text):
    if not text:
        return []
    text = text.replace('\n', '').replace('\r', '')
    patterns = [
        r"(https://www\.netflix\.com/account/travel/[^\s>\"'\)\]]+)",
        r"(https://www\.netflix\.com/account/update-primary-location\?[^\s>\"'\)\]]+)",
        r"(https://www\.netflix\.com/account/confirmdevice\?[^\s>\"'\)\]]+)",
    ]
    links = []
    for pattern in patterns:
        for url in re.findall(pattern, text):
            cleaned = clean_url(url)
            if cleaned not in links:
                links.append(cleaned)
    return links


def extract_verify_email_link(text):
    if not text:
        return None
    text = text.replace('\n', '').replace('\r', '')
    match = re.search(r"(https://www\.netflix\.com/verifyemail\?[^\s>\"'\)\]]+)", text)
    return clean_url(match.group(1)) if match else None


def extract_tv_login_link(text):
    if not text:
        return None
    text = text.replace('\n', '').replace('\r', '')
    match = re.search(r"(https://www\.netflix\.com/ilum\?code=[^\s>\"'\)\]]+)", text)
    return clean_url(match.group(1)) if match else None


def _category_threshold(category_key):
    category = CATEGORIES.get(category_key, CATEGORIES["household"])
    now = datetime.now(UTC)
    if category.get("window_hours"):
        return now - timedelta(hours=category["window_hours"])
    return now - timedelta(minutes=category.get("window_minutes", 15))


def _extract_body(msg):
    """
    Extract all text/HTML from all parts (including nested messages) using an iterative
    stack to avoid recursion issues. HTML is kept raw so link regex can find href URLs.
    After concatenation, decode HTML entities so &amp; becomes &.
    """
    texts = []
    stack = [(msg, 0)]
    seen = set()
    max_depth = 20

    while stack:
        part, depth = stack.pop()
        if depth > max_depth:
            continue
        part_id = id(part)
        if part_id in seen:
            continue
        seen.add(part_id)

        if part.is_multipart():
            for sub in part.get_payload():
                stack.append((sub, depth + 1))
        else:
            content_type = part.get_content_type()
            disposition = part.get("Content-Disposition") or ""
            if "attachment" in disposition.lower():
                continue

            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="ignore")
            except Exception:
                continue

            if content_type == "text/plain":
                texts.append(text)
            elif content_type == "text/html":
                texts.append(text)
            elif content_type == "message/rfc822":
                try:
                    inner = email.message_from_bytes(payload)
                    stack.append((inner, depth + 1))
                except Exception:
                    pass

    full_body = "\n".join(texts)
    full_body = html.unescape(full_body)
    return clean_urls(full_body)


def _extract_plain_text_body(msg):
    """Match the reference fetcher: use only the first plain-text body part."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "text/plain" or part.get("Content-Disposition"):
                continue
            try:
                payload = part.get_payload(decode=True)
                return clean_urls((payload or b"").decode(errors="ignore"))
            except Exception:
                continue
        return ""

    try:
        payload = msg.get_payload(decode=True)
        return clean_urls((payload or b"").decode(errors="ignore"))
    except Exception:
        return ""


def _format_email_time(msg_date):
    try:
        email_date = email.utils.parsedate_to_datetime(msg_date)
        if email_date.tzinfo is None:
            email_date = email_date.replace(tzinfo=UTC)
        india_time = email_date.astimezone(INDIA).strftime("%d/%m, %I:%M %p")
        indo_time = email_date.astimezone(INDO).strftime("%d/%m, %H:%M")
        return email_date, f"Email Time: {india_time} (India) | {indo_time} (Indonesia)"
    except Exception:
        return None, "Email Time: Unavailable"


def _build_result(receiver_email, category_key, body, time_text, subject=""):
    label = CATEGORIES.get(category_key, {}).get("label", category_key)

    if category_key == "login_code":
        code = extract_login_code(body, subject)
        if code:
            return True, f"{time_text}\nEmail: {receiver_email}\nLogin Code: {code}", [code]
    elif category_key == "verification_code":
        # Keep post-login verification mail in its own category.
        if is_verification_code_after_login(subject, body):
            return False, f"No {label} result found.", []
        code = extract_verification_code(body)
        if code:
            return True, f"{time_text}\nEmail: {receiver_email}\nVerification Code: {code}", [code]
    elif category_key == "verification_code_after_login":
        code = extract_verification_code_after_login(subject, body)
        if code:
            return True, f"{time_text}\nEmail: {receiver_email}\nVerification Code After Login: {code}", [code]
    elif category_key == "reset":
        link = extract_reset_link(body)
        if link:
            return True, f"{time_text}\nEmail: {receiver_email}\nReset Link: {link}", [link]
    elif category_key == "household":
        links = extract_household_links(body)
        if links:
            return True, f"{time_text}\nEmail: {receiver_email}\nHousehold Link(s):\n" + "\n".join(links), links
    elif category_key == "verify_email":
        link = extract_verify_email_link(body)
        if link:
            return True, f"{time_text}\nEmail: {receiver_email}\nVerification Link: {link}", [link]
    elif category_key == "tv_login":
        link = extract_tv_login_link(body)
        if link:
            return True, f"{time_text}\nEmail: {receiver_email}\nTV Login Link: {link}\nNote: Link usually expires fast.", [link]

    return False, f"No {label} result found.", []


def not_found_message(category_key):
    return {
        "login_code": "No new Sign-in mail found. Request the code again and retry.",
        "verification_code": "No new Verification Code mail found in the time window.",
        "verification_code_after_login": "No post-login Verification Code mail found in the last 15 minutes.",
        "household": "No new Household link/code mail found. Request it again and retry.",
        "reset": "No new Reset mail found in the last 24 hours.",
        "verify_email": "No new Verification Email found in the last 48 hours.",
        "tv_login": "No new TV Login link found in the last 15 minutes.",
    }.get(category_key, "No relevant email found in the selected time frame.")


def _active_imap_accounts():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM imap_accounts WHERE enabled=1 ORDER BY id").fetchall()
    accounts = []
    for row in rows:
        password = decrypt_secret(row["encrypted_password"])
        if password:
            accounts.append((row, password))
    return accounts


def fetch_email_for_account(receiver_email, category_key):
    start_time = time.time()
    category_key = category_key if category_key in CATEGORIES else "household"

    accounts = _active_imap_accounts()
    if not accounts:
        return {
            "email": receiver_email,
            "category": category_key,
            "status": "error",
            "result": "No enabled IMAP account is configured.",
            "items": [],
            "fetch_time": round(time.time() - start_time, 2),
        }

    max_retries = 3
    base_delay = 2
    threshold = _category_threshold(category_key)

    for account, password in accounts:
      for attempt in range(max_retries):
        imap = None
        try:
            imap = imaplib.IMAP4_SSL(account["imap_host"], timeout=15)
            imap.login(account["email"], password)

            since_date = threshold.strftime("%d-%b-%Y")
            search_criteria_to = f'(TO "{receiver_email}") (SINCE "{since_date}")'
            search_criteria_text = f'(TEXT "{receiver_email}") (SINCE "{since_date}")'

            mailboxes_to_check = [
                account["mailbox"],
                '"[Gmail]/Spam"',
                '"Spam"',
                '"Junk"',
                '"[Gmail]/All Mail"'
            ]
            found_result = None

            # First attempt: TO search
            for mailbox in mailboxes_to_check:
                try:
                    status, _ = imap.select(mailbox, readonly=True)
                    if status != "OK":
                        continue
                except Exception:
                    continue

                status, messages = imap.search(None, search_criteria_to)
                if status == "OK" and messages[0]:
                    email_ids = messages[0].split()
                    for eid in reversed(email_ids):
                        status, msg_data = imap.fetch(eid, "(RFC822)")
                        if status != "OK" or not isinstance(msg_data, list):
                            continue
                        for response_part in msg_data:
                            if not isinstance(response_part, tuple):
                                continue
                            msg = email.message_from_bytes(response_part[1])
                            # IMAP TO search is only a preliminary filter. Do not
                            # trust other recipient-like headers or metadata.
                            if not _to_header_matches(msg, receiver_email):
                                continue
                            email_date, time_text = _format_email_time(msg.get("Date"))
                            if email_date and email_date < threshold:
                                continue
                            subject = msg.get("Subject") or ""
                            body = (_extract_plain_text_body(msg)
                                    if category_key in ("verification_code", "verification_code_after_login")
                                    else _extract_body(msg))
                            found, result, items = _build_result(receiver_email, category_key, body, time_text, subject=subject)
                            if found:
                                found_result = {
                                    "email": receiver_email,
                                    "category": category_key,
                                    "status": "found",
                                    "result": result,
                                    "items": items,
                                    "fetch_time": round(time.time() - start_time, 2),
                                }
                                break
                        if found_result:
                            break
                if found_result:
                    break

            # If TO search failed and category is 'reset', try TEXT search as fallback
            if not found_result and category_key == "reset":
                for mailbox in mailboxes_to_check:
                    try:
                        status, _ = imap.select(mailbox, readonly=True)
                        if status != "OK":
                            continue
                    except Exception:
                        continue

                    status, messages = imap.search(None, search_criteria_text)
                    if status == "OK" and messages[0]:
                        email_ids = messages[0].split()
                        for eid in reversed(email_ids):
                            status, msg_data = imap.fetch(eid, "(RFC822)")
                            if status != "OK" or not isinstance(msg_data, list):
                                continue
                            for response_part in msg_data:
                                if not isinstance(response_part, tuple):
                                    continue
                                msg = email.message_from_bytes(response_part[1])
                                if not _to_header_matches(msg, receiver_email):
                                    continue
                                email_date, time_text = _format_email_time(msg.get("Date"))
                                if email_date and email_date < threshold:
                                    continue
                                subject = msg.get("Subject") or ""
                                body = (_extract_plain_text_body(msg)
                                        if category_key in ("verification_code", "verification_code_after_login")
                                        else _extract_body(msg))
                                found, result, items = _build_result(receiver_email, category_key, body, time_text, subject=subject)
                                if found:
                                    found_result = {
                                        "email": receiver_email,
                                        "category": category_key,
                                        "status": "found",
                                        "result": result,
                                        "items": items,
                                        "fetch_time": round(time.time() - start_time, 2),
                                    }
                                    break
                            if found_result:
                                break
                    if found_result:
                        break

            if found_result:
                return found_result

            return {
                "email": receiver_email,
                "category": category_key,
                "status": "not_found",
                "result": not_found_message(category_key),
                "items": [],
                "fetch_time": round(time.time() - start_time, 2),
            }

        except (imaplib.IMAP4.error, ConnectionResetError, ConnectionError) as exc:
            logger.warning("IMAP attempt %s/%s failed for %s: %s", attempt + 1, max_retries, receiver_email, exc)
            if attempt + 1 == max_retries and account is accounts[-1][0]:
                return {
                    "email": receiver_email,
                    "category": category_key,
                    "status": "error",
                    "result": f"Email server connection failed after {max_retries} attempts. Try again later.",
                    "items": [],
                    "fetch_time": round(time.time() - start_time, 2),
                }
            time.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            logger.warning("Unexpected fetch error for %s", receiver_email)
            if account is accounts[-1][0]:
                return {
                    "email": receiver_email,
                    "category": category_key,
                    "status": "error",
                    "result": "Email server connection failed. Try again later.",
                    "items": [],
                    "fetch_time": round(time.time() - start_time, 2),
                }
        finally:
            if imap:
                try:
                    imap.close()
                except Exception:
                    pass
                try:
                    imap.logout()
                except Exception:
                    pass
      # Try the next enabled account after a failed connection.
    return {
        "email": receiver_email, "category": category_key, "status": "error",
        "result": "Email server connection failed. Try again later.", "items": [],
        "fetch_time": round(time.time() - start_time, 2),
    }


def fetch_emails_concurrently(email_list, category_key):
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(fetch_email_for_account, addr, category_key): addr for addr in email_list}
        for future in concurrent.futures.as_completed(future_map):
            try:
                results.append(future.result())
            except Exception as exc:
                addr = future_map[future]
                results.append({
                    "email": addr,
                    "category": category_key,
                    "status": "error",
                    "result": f"Error: {exc}",
                    "items": [],
                    "fetch_time": 0,
                })
    return sorted(results, key=lambda row: row["email"])
