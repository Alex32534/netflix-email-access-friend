import csv
import io
import json
import logging
import time
import zipfile
from datetime import timedelta
from functools import wraps
from urllib.parse import quote

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_socketio import SocketIO, join_room
from werkzeug.security import check_password_hash, generate_password_hash

import config
from db import connect, get_user, get_user_by_login, init_db, is_admin
from services.email_fetcher import fetch_emails_concurrently
from services.security import decrypt_secret, encrypt_secret
from services.utils import CATEGORIES, DATE_FORMAT, iso_now, json_dumps, parse_emails, today_ist

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.update(SECRET_KEY=config.SECRET_KEY, PERMANENT_SESSION_LIFETIME=timedelta(days=14))
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*", transports=["polling"])

init_db()
failed_code_attempts = {}
PROTECTED_CATEGORIES = ("login_code", "reset", "verification_code", "verification_code_after_login", "verify_email")


def row_to_dict(row):
    return dict(row) if row else None


def setting(key, default=""):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def save_setting(key, value):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)", (key, value, iso_now()))
        conn.commit()


def category_protected(category_key):
    return category_key in PROTECTED_CATEGORIES and setting(f"protection_enabled_{category_key}", "0") == "1" and bool(setting(f"protection_hash_{category_key}"))


def log_activity(event_type, message, actor_user_id=None, meta=None):
    now = iso_now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO activity_events (event_type, message, actor_user_id, meta_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_type, message, actor_user_id, json_dumps(meta or {}), now),
        )
        conn.commit()
    payload = {"id": cur.lastrowid, "event_type": event_type, "message": message, "created_at": now}
    socketio.emit("activity", payload, room="admins")
    return payload


@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.user = get_user(user_id) if user_id else None


@app.context_processor
def inject_globals():
    number = setting("whatsapp_number")
    message = setting("whatsapp_message", "Hello, I need help with the email code service.")
    whatsapp_url = f"https://wa.me/{number}?text={quote(message)}" if number and setting("whatsapp_enabled", "1") == "1" else ""
    return {"app_name": config.APP_NAME, "current_user": g.get("user"), "categories": CATEGORIES,
            "is_admin_user": is_admin(g.get("user")), "whatsapp_url": whatsapp_url,
            "whatsapp_enabled": bool(whatsapp_url),
            "protected_categories": [key for key in PROTECTED_CATEGORIES if category_protected(key)]}


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user or not is_admin(g.user):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@socketio.on("connect")
def socket_connect():
    user_id = session.get("user_id")
    user = get_user(user_id) if user_id else None
    if user and is_admin(user):
        join_room("admins")


@app.route("/")
@app.route("/search")
def search_page():
    return render_template("search.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        login_id = (request.form.get("username") or "").strip()
        user = get_user_by_login(login_id)
        if not user or not is_admin(user) or not check_password_hash(user["password_hash"], request.form.get("password", "")):
            flash("Invalid administrator credentials.", "danger")
            return render_template("login.html")
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        log_activity("admin_login", "Administrator signed in", user["id"])
        return redirect(request.args.get("next") or url_for("dashboard"))
    return render_template("login.html")


@app.route("/login")
def legacy_login():
    return redirect(url_for("admin_login"))


@app.route("/logout")
@admin_required
def logout():
    session.clear()
    return redirect(url_for("search_page"))


def dashboard_stats():
    today = today_ist().strftime(DATE_FORMAT)
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM search_history").fetchone()["c"]
        today_count = conn.execute("SELECT COUNT(*) c FROM search_history WHERE substr(created_at,1,10)=?", (today,)).fetchone()["c"]
        successful = conn.execute("SELECT COUNT(*) c FROM search_history WHERE status='found'").fetchone()["c"]
        failed = conn.execute("SELECT COUNT(*) c FROM search_history WHERE status!='found'").fetchone()["c"]
        by_category = conn.execute("SELECT category, COUNT(*) count FROM search_history GROUP BY category ORDER BY count DESC").fetchall()
        events = conn.execute("SELECT * FROM activity_events ORDER BY id DESC LIMIT 12").fetchall()
        recent = conn.execute("SELECT * FROM search_history ORDER BY id DESC LIMIT 10").fetchall()
        account = conn.execute("SELECT 1 FROM imap_accounts WHERE enabled=1 LIMIT 1").fetchone()
    return {"total_searches": total, "searches_today": today_count, "successful_searches": successful,
            "failed_searches": failed, "by_category": by_category, "recent_events": events,
            "recent_searches": recent, "imap_ready": bool(account)}


@app.route("/dashboard")
@admin_required
def dashboard():
    return render_template("dashboard.html", stats=dashboard_stats())


@app.route("/api/dashboard/stats")
@admin_required
def api_dashboard_stats():
    stats = dashboard_stats()
    stats["by_category"] = [row_to_dict(row) for row in stats["by_category"]]
    stats["recent_events"] = [row_to_dict(row) for row in stats["recent_events"]]
    stats["recent_searches"] = [row_to_dict(row) for row in stats["recent_searches"]]
    return jsonify(stats)


def _code_rate_limited(category_key):
    now = time.time()
    item = failed_code_attempts.get((request.remote_addr, category_key), {"count": 0, "until": 0})
    return item["until"] > now


def _authorize_category(category_key, data):
    if not category_protected(category_key):
        return True
    session_key = f"protected_until_{category_key}"
    if session.get(session_key, 0) > time.time():
        return True
    if _code_rate_limited(category_key):
        return False
    supplied = data.get("private_code") or ""
    stored_hash = setting(f"protection_hash_{category_key}")
    if supplied and check_password_hash(stored_hash, supplied):
        session[session_key] = time.time() + 900
        failed_code_attempts.pop((request.remote_addr, category_key), None)
        return True
    item = failed_code_attempts.setdefault((request.remote_addr, category_key), {"count": 0, "until": 0})
    item["count"] += 1
    if item["count"] >= 5:
        item["until"] = time.time() + 300
    return False


@app.route("/api/private-code", methods=["POST"])
def private_code():
    data = request.get_json(silent=True) or request.form
    category_key = data.get("category")
    if category_key == "signin_code":
        category_key = "login_code"
    if category_key not in PROTECTED_CATEGORIES:
        return jsonify({"ok": False, "error": "Invalid category."}), 400
    if not category_protected(category_key):
        return jsonify({"ok": True})
    if not _authorize_category(category_key, data):
        return jsonify({"ok": False, "error": "Invalid access code."}), 403
    return jsonify({"ok": True})


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(silent=True) or request.form
    category_key = data.get("category")
    if category_key == "signin_code":
        category_key = "login_code"
    if category_key not in CATEGORIES:
        return jsonify({"ok": False, "error": "Invalid category."}), 400
    if category_key in PROTECTED_CATEGORIES and not _authorize_category(category_key, data):
        return jsonify({"ok": False, "error": "Invalid access code."}), 403
    emails = parse_emails(data.get("emails") or data.get("email") or "")
    if not emails:
        return jsonify({"ok": False, "error": "Enter a valid email address."}), 400
    label = CATEGORIES[category_key]["label"]
    log_activity("search_started", f"Public {label} search started for {len(emails)} email(s)", None, {"category": category_key, "count": len(emails)})
    results = fetch_emails_concurrently(emails, category_key)
    now = iso_now()
    with connect() as conn:
        for result in results:
            conn.execute("INSERT INTO search_history (user_id, email, category, status, result_text, fetch_time, created_at) VALUES (NULL, ?, ?, ?, ?, ?, ?)",
                         (result["email"], category_key, result["status"], result["result"], result.get("fetch_time", 0), now))
        conn.commit()
    for result in results:
        log_activity("search_result", f"{label}: {result['status']}", None, {"email": result["email"], "category": category_key, "status": result["status"]})
    return jsonify({"ok": True, "results": results})


@app.route("/logs")
@admin_required
def logs_page():
    with connect() as conn:
        searches = conn.execute("SELECT * FROM search_history ORDER BY id DESC LIMIT 250").fetchall()
        events = conn.execute("SELECT * FROM activity_events ORDER BY id DESC LIMIT 250").fetchall()
    return render_template("logs.html", searches=searches, events=events)


def _active_account():
    with connect() as conn:
        return conn.execute("SELECT * FROM imap_accounts ORDER BY enabled DESC, id LIMIT 1").fetchone()


@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings_page():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "signin":
            category_key = request.form.get("category_key")
            if category_key not in PROTECTED_CATEGORIES:
                abort(400)
            enabled = request.form.get("protection_enabled") == "1"
            code = request.form.get("private_code") or ""
            session.pop(f"protected_until_{category_key}", None)
            if code:
                save_setting(f"protection_hash_{category_key}", generate_password_hash(code))
            save_setting(f"protection_enabled_{category_key}", "1" if enabled else "0")
            flash("Category protection saved.", "success")
        elif action == "admin_password":
            password = request.form.get("new_password") or ""
            confirm = request.form.get("confirm_password") or ""
            if len(password) < 8 or password != confirm:
                flash("Password must match and contain at least 8 characters.", "danger")
            else:
                with connect() as conn:
                    conn.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?", (generate_password_hash(password), iso_now(), g.user["id"]))
                    conn.commit()
                session.clear()
                flash("Administrator password changed. Sign in again.", "success")
                return redirect(url_for("admin_login"))
        elif action == "whatsapp":
            save_setting("whatsapp_enabled", "1" if request.form.get("whatsapp_enabled") == "1" else "0")
            save_setting("whatsapp_number", (request.form.get("whatsapp_number") or "").replace("+", "").replace(" ", ""))
            save_setting("whatsapp_message", request.form.get("whatsapp_message") or "Hello, I need help with the email code service.")
            flash("WhatsApp support saved.", "success")
        elif action == "imap":
            email_address = (request.form.get("email") or "").strip()
            password = request.form.get("app_password") or ""
            host = (request.form.get("imap_host") or "imap.gmail.com").strip()
            mailbox = (request.form.get("mailbox") or "INBOX").strip()
            account_id = request.form.get("account_id")
            encrypted = encrypt_secret(password) if password else None
            if not email_address or (not encrypted and not account_id):
                flash("Email and App Password are required. Set IMAP_ENCRYPTION_KEY first.", "danger")
            else:
                with connect() as conn:
                    if account_id:
                        if encrypted:
                            conn.execute("UPDATE imap_accounts SET email=?, encrypted_password=?, imap_host=?, mailbox=?, updated_at=? WHERE id=?", (email_address, encrypted, host, mailbox, iso_now(), account_id))
                        else:
                            conn.execute("UPDATE imap_accounts SET email=?, imap_host=?, mailbox=?, updated_at=? WHERE id=?", (email_address, host, mailbox, iso_now(), account_id))
                    else:
                        conn.execute("UPDATE imap_accounts SET enabled=0 WHERE enabled=1")
                        conn.execute("INSERT INTO imap_accounts (email, encrypted_password, imap_host, mailbox, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)", (email_address, encrypted, host, mailbox, iso_now(), iso_now()))
                    conn.commit()
                flash("IMAP account saved.", "success")
        elif action == "delete_imap":
            with connect() as conn:
                conn.execute("DELETE FROM imap_accounts WHERE id=?", (request.form.get("account_id"),))
                conn.commit()
            flash("IMAP account deleted.", "success")
        elif action == "test_imap":
            account = _active_account()
            ok, message = _test_imap(account)
            flash(message, "success" if ok else "danger")
    with connect() as conn:
        account = conn.execute("SELECT * FROM imap_accounts ORDER BY enabled DESC, id LIMIT 1").fetchone()
    protections = {key: {"enabled": category_protected(key)} for key in PROTECTED_CATEGORIES}
    return render_template("settings.html", account=account, protections=protections, whatsapp_enabled=setting("whatsapp_enabled", "1") == "1", whatsapp_number=setting("whatsapp_number"), whatsapp_message=setting("whatsapp_message", "Hello, I need help with the email code service."))


def _test_imap(account):
    if not account:
        return False, "IMAP connection failed. No account is configured."
    password = decrypt_secret(account["encrypted_password"])
    if not password:
        return False, "IMAP connection failed. Credential encryption is unavailable."
    import imaplib
    try:
        client = imaplib.IMAP4_SSL(account["imap_host"], timeout=15)
        client.login(account["email"], password)
        status, _ = client.select(account["mailbox"], readonly=True)
        client.logout()
        return (status == "OK", "IMAP connection successful" if status == "OK" else "IMAP connection failed. Check the mailbox.")
    except Exception:
        logger.warning("IMAP connection test failed")
        return False, "IMAP connection failed. Check the email, app password, host, or mailbox."


@app.route("/download-data")
@admin_required
def download_data():
    with connect() as conn:
        searches = [dict(row) for row in conn.execute("SELECT id,email,category,status,result_text,fetch_time,created_at FROM search_history ORDER BY id DESC").fetchall()]
        events = [dict(row) for row in conn.execute("SELECT id,event_type,message,created_at FROM activity_events ORDER BY id DESC").fetchall()]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("search_history.csv", _csv_bytes(searches, ["id", "email", "category", "status", "result_text", "fetch_time", "created_at"]))
        archive.writestr("activity_events.csv", _csv_bytes(events, ["id", "event_type", "message", "created_at"]))
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="search_data.zip", mimetype="application/zip")


def _csv_bytes(rows, fields):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


@app.route("/health")
def health():
    with connect() as conn:
        account = conn.execute("SELECT 1 FROM imap_accounts WHERE enabled=1 LIMIT 1").fetchone()
    return jsonify({"ok": True, "app": config.APP_NAME, "imap_ready": bool(account)})


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, title="Forbidden", message="You do not have permission for this page/action."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, title="Not Found", message="This page does not exist."), 404


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
