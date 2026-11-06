import argparse
from html import escape
import getpass
import hashlib
import json
import hmac
import os
import re
import secrets
import sqlite3
import socket
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from flask import Flask, Response, abort, current_app, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() == "true",
    MAX_CONTENT_LENGTH=1024 * 1024,
)
legacy_database = Path(__file__).parent / ".venv" / "instance" / "spacezone.db"
data_directory = Path(os.environ.get("SPACEZONE_DATA_DIR") or app.instance_path)
app.config["DATABASE"] = str(
    data_directory / "spacezone.db"
    if os.environ.get("SPACEZONE_DATA_DIR") or not legacy_database.exists()
    else legacy_database
)
_trial_email_lock = Lock()
_trial_email_times: dict[str, float] = {}
_chat_request_lock = Lock()
_chat_request_times: dict[str, list[float]] = {}
_admin_login_lock = Lock()
_admin_login_attempts: dict[str, list[float]] = {}
_admin_password_reset_lock = Lock()
_admin_password_reset_attempts: dict[str, list[float]] = {}
_user_login_lock = Lock()
_user_login_attempts: dict[str, list[float]] = {}

@app.route("/")
def hello_world():
    return render_template("index.html", csrf_token=_csrf_token())


@app.route("/tools")
def all_tools():
    return render_template("index.html", csrf_token=_csrf_token(), standalone_tools=True)


@app.route("/tools/geo-aeo")
def geo_aeo_tool():
    return render_template("standalone_tool.html", tool="geo_aeo")


@app.route("/chatbot")
def chatbot_page():
    return render_template("standalone_tool.html", tool="chatbot")


@app.route("/healthz")
def health_check():
    return jsonify(status="ok")


@app.route("/__dev/reload-state")
def dev_reload_state():
    if not current_app.debug:
        abort(404)

    root = Path(current_app.root_path)
    tracked_files = [root / "app.py"]
    for directory in (root / "templates", root / "static"):
        if directory.exists():
            tracked_files.extend(path for path in directory.rglob("*") if path.is_file())

    digest = hashlib.sha256()
    for path in sorted(tracked_files):
        metadata = path.stat()
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(str(metadata.st_mtime_ns).encode())
        digest.update(str(metadata.st_size).encode())
    return jsonify(token=digest.hexdigest())


@app.route("/chat", methods=["POST"])
def chat_reply():
    if not _csrf_valid():
        return jsonify(error="Refresh the page and try again."), 400

    question = request.form.get("topic", "").strip()
    if not question:
        return jsonify(error="Type a question or topic to chat with Space Zone."), 400
    if len(question) > 300:
        return jsonify(error="Keep your message under 300 characters."), 400

    client_ip = request.remote_addr or "unknown"
    now = monotonic()
    with _chat_request_lock:
        attempts = [
            attempted_at
            for attempted_at in _chat_request_times.get(client_ip, [])
            if now - attempted_at < 60
        ]
        if len(attempts) >= 3:
            _chat_request_times[client_ip] = attempts
            rate_limited = True
        else:
            attempts.append(now)
            _chat_request_times[client_ip] = attempts
            rate_limited = False

    if rate_limited:
        return jsonify(error="You've reached the limit of 3 messages per minute. Please try again shortly."), 429

    normalized = question.casefold()
    words = set(re.findall(r"[a-z0-9]+", normalized))
    topic = re.sub(r"\s+", " ", question).strip(" .?!")
    if words & {"hello", "hi", "hey"}:
        reply = (
            "Hi! I’m Space Zone’s free SEO helper. Ask me about keywords, "
            "SEO titles and descriptions, content ideas, or marketing basics."
        )
    elif words & {"price", "pricing", "cost", "plan", "plans", "trial"}:
        reply = (
            "Space Zone’s listed prices are Starter at ₹399/month and Growth at "
            "₹999/month. UPI payment links are one-time payments that require "
            "manual monthly renewal. Payments are not verified and paid access "
            "is not activated automatically. You can create an account and save tool results."
        )
    elif words & {"keyword", "keywords", "research", "search"}:
        reply = (
            f"For “{topic}”, start with the main service or product, the "
            "problem it solves, and the audience or location you serve. Try "
            "mixing those phrases into specific searches. These are brainstorming "
            "ideas—not live keyword volume or ranking data."
        )
    elif words & {"title", "titles", "meta", "description", "snippet"}:
        reply = (
            f"SEO draft for “{topic}”: write one clear page title that names "
            "the main service and audience. Then write a unique, honest meta "
            "description that explains the page benefit and suggests a next "
            "step. Treat this as a draft and check that it matches the actual page."
        )
    elif words & {"content", "blog", "blogs", "article", "articles", "ideas"}:
        reply = (
            f"Content starters for “{topic}”: answer a common beginner question, "
            "create a practical how-to guide, compare common options, and share "
            "a customer FAQ. Choose the idea that best matches what your real "
            "customers ask."
        )
    elif words & {"marketing", "campaign", "campaigns", "social", "email"}:
        reply = (
            f"Marketing starter for “{topic}”: pick one audience and one goal, "
            "create a useful landing page, share a short tip on a channel your "
            "audience uses, and track clicks or enquiries. Avoid claiming results "
            "until you have real campaign data."
        )
    elif words & {"seo", "ranking", "rank", "traffic", "website", "site"}:
        reply = (
            f"SEO checklist for “{topic}”: make one useful page for the searcher, "
            "write a descriptive title and heading, use clear page links, make "
            "the page mobile-friendly, and review your actual search analytics. "
            "This helper does not access live website or ranking data."
        )
    else:
        reply = (
            f"I can help brainstorm SEO and marketing for “{topic}”. Try asking "
            "about keyword ideas, an SEO title or meta description, content "
            "ideas, marketing campaigns, pricing, or a basic SEO checklist. "
            "My answers use built-in guidance, not live web or analytics data."
        )
    return jsonify(suggestions=reply)


@contextmanager
def _connect_db() -> Iterator[sqlite3.Connection]:
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(app.config["DATABASE"], timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _init_db() -> None:
    with _connect_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trial_signups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                requested_at TEXT NOT NULL,
                email_status TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_password_resets (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                token_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO app_settings (name, value) VALUES (?, ?)",
            ("flask_secret_key", secrets.token_hex(32)),
        )
        connection.execute(
            "INSERT OR IGNORE INTO app_settings (name, value) VALUES (?, ?)",
            ("admin_session_epoch", "0"),
        )


_init_db()
if not os.environ.get("FLASK_SECRET_KEY"):
    with _connect_db() as connection:
        app.secret_key = connection.execute(
            "SELECT value FROM app_settings WHERE name = ?",
            ("flask_secret_key",),
        ).fetchone()["value"]


def _set_email_status(email: str, status: str) -> None:
    with _connect_db() as connection:
        connection.execute(
            "UPDATE trial_signups SET email_status = ? WHERE email = ? COLLATE NOCASE",
            (status, email),
        )


def _send_resend_email(message: dict[str, object]) -> str:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key or not os.environ.get("SPACEZONE_FROM_EMAIL"):
        return "not_configured"

    email_request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(message).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(email_request, timeout=10) as response:
            response.read()
    except HTTPError as error:
        current_app.logger.error("Resend rejected an email (HTTP %s).", error.code)
        return "failed"
    except (URLError, TimeoutError) as error:
        current_app.logger.error("Could not connect to Resend: %s", error)
        return "failed"
    return "sent"


def _send_welcome_email(email: str) -> str:
    api_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("SPACEZONE_FROM_EMAIL")
    if not api_key or not sender:
        return "not_configured"

    message = {
        "from": sender,
        "to": [email],
        "subject": "Welcome to Space Zone",
        "text": (
            "Welcome to Space Zone!\n\n"
            "We're glad you're here. Space Zone is being built to bring SEO "
            "insights, keyword research, performance graphs, and marketing "
            "planning together in one clear workspace.\n\n"
            "Your 10-day free trial is not active yet. Trial activation "
            "and billing are not connected, so you will not be charged. "
            "We'll let you know when you can get started.\n\n"
            "Thanks for being among the first to explore Space Zone."
        ),
        "html": (
            "<div style=\"margin:0;padding:40px 16px;background:#080b18;"
            "font-family:Arial,sans-serif;color:#f5f6ff\">"
            "<div style=\"max-width:560px;margin:0 auto;padding:32px;"
            "border:1px solid #292e48;border-radius:16px;background:#101528\">"
            "<p style=\"margin:0 0 12px;color:#77e3e2;font-size:12px;"
            "font-weight:bold;letter-spacing:2px;text-transform:uppercase\">"
            "SPACE ZONE</p>"
            "<h1 style=\"margin:0 0 16px;font-size:28px\">Welcome aboard.</h1>"
            "<p style=\"color:#c4c9dc;line-height:1.7\">We're glad you're here. "
            "Space Zone is being built to bring SEO insights, keyword research, "
            "performance graphs, and marketing planning together in one clear "
            "workspace.</p>"
            "<div style=\"margin:24px 0;padding:16px;border-radius:10px;"
            "background:#191f38;color:#c4c9dc;line-height:1.7\">"
            "<strong style=\"color:#f5f6ff\">About your 10-day trial</strong><br>"
            "Your trial is not active yet. Trial activation and billing are "
            "not connected, so you will not be charged. We'll let you know when "
            "you can get started.</div>"
            "<p style=\"margin-bottom:0;color:#a1a8c2;line-height:1.7\">"
            "Thanks for being among the first to explore Space Zone.</p>"
            "</div></div>"
        ),
    }
    email_request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(message).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(email_request, timeout=10) as response:
            response.read()
    except HTTPError as error:
        current_app.logger.error("Resend rejected a trial email (HTTP %s).", error.code)
        return "failed"
    except (URLError, TimeoutError) as error:
        current_app.logger.error("Could not connect to Resend: %s", error)
        return "failed"
    return "sent"


def _admin_reset_base_url() -> str | None:
    configured_url = os.environ.get("SPACEZONE_PUBLIC_URL", "").strip()
    parsed_url = urlsplit(configured_url)
    local_http = parsed_url.hostname in {"localhost", "127.0.0.1"}
    if (
        parsed_url.scheme not in ({"https", "http"} if local_http else {"https"})
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.path not in ("", "/")
        or parsed_url.query
        or parsed_url.fragment
    ):
        current_app.logger.error(
            "Admin password recovery requires a valid SPACEZONE_PUBLIC_URL "
            "(HTTPS except for localhost)."
        )
        return None
    return configured_url.rstrip("/")


def _send_admin_password_reset_email(email: str, reset_url: str) -> str:
    message: dict[str, object] = {
        "from": os.environ.get("SPACEZONE_FROM_EMAIL", ""),
        "to": [email],
        "subject": "Reset your Space Zone admin password",
        "text": (
            "A password reset was requested for the Space Zone admin account.\n\n"
            f"Set a new password using this one-time link (valid for 30 minutes):\n"
            f"{reset_url}\n\n"
            "If you did not request this, you can ignore this email."
        ),
        "html": (
            "<p>A password reset was requested for the Space Zone admin account.</p>"
            f'<p><a href="{escape(reset_url, quote=True)}">Set a new password</a></p>'
            "<p>This one-time link expires in 30 minutes. If you did not request "
            "this, you can ignore this email.</p>"
        ),
    }
    return _send_resend_email(message)


def _get_admin_user():
    if all(os.environ.get(name) for name in ("ADMIN_USERNAME", "ADMIN_PASSWORD")):
        with _connect_db() as connection:
            override = connection.execute(
                "SELECT value FROM app_settings WHERE name = ?",
                ("admin_password_hash_override",),
            ).fetchone()
        if override is not None:
            return {
                "username": os.environ["ADMIN_USERNAME"],
                "password_hash": override["value"],
                "environment_password": None,
            }
        return {
            "username": os.environ["ADMIN_USERNAME"],
            "password_hash": None,
            "environment_password": os.environ["ADMIN_PASSWORD"],
        }
    with _connect_db() as connection:
        row = connection.execute(
            "SELECT username, password_hash FROM admin_users WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    return {
        "username": row["username"],
        "password_hash": row["password_hash"],
        "environment_password": None,
    }


def _check_admin_configured() -> bool:
    return _get_admin_user() is not None


def _is_local_request() -> bool:
    return request.remote_addr in {"127.0.0.1", "::1"}


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_public_page_context():
    return {
        "csrf_token": _csrf_token(),
        "current_user": _current_user(),
    }


def _csrf_valid() -> bool:
    submitted = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf_token", "")
    return bool(submitted and expected and hmac.compare_digest(submitted, expected))


def _current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    with _connect_db() as connection:
        return connection.execute(
            "SELECT id, display_name, email FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


def _user_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _current_user() is None:
            session.pop("user_id", None)
            return redirect(url_for("user_login"))
        return view(*args, **kwargs)

    return wrapped


def _limit_user_login(ip_address: str) -> bool:
    now = monotonic()
    with _user_login_lock:
        attempts = [
            attempted_at
            for attempted_at in _user_login_attempts.get(ip_address, [])
            if now - attempted_at < 300
        ]
        _user_login_attempts[ip_address] = attempts
        return len(attempts) >= 5


def _record_user_login_failure(ip_address: str) -> None:
    with _user_login_lock:
        _user_login_attempts.setdefault(ip_address, []).append(monotonic())


@app.route("/signup", methods=["GET", "POST"])
def user_signup():
    if _current_user() is not None:
        return redirect(url_for("user_dashboard"))

    message = None
    status_code = 200
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirmation = request.form.get("confirm_password", "")
        if not _csrf_valid():
            message = "Refresh this page and try signing up again."
            status_code = 400
        elif not display_name or len(display_name) > 80:
            message = "Enter a name up to 80 characters long."
            status_code = 400
        elif len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            message = "Enter a valid email address."
            status_code = 400
        elif len(password) < 12:
            message = "Choose a password with at least 12 characters."
            status_code = 400
        elif password != confirmation:
            message = "The passwords do not match."
            status_code = 400
        else:
            try:
                with _connect_db() as connection:
                    cursor = connection.execute(
                        "INSERT INTO users (display_name, email, password_hash, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            display_name,
                            email,
                            generate_password_hash(password),
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        ),
                    )
                    user_id = cursor.lastrowid
            except sqlite3.IntegrityError:
                message = "An account with that email already exists. Sign in instead."
                status_code = 409
            if message is None:
                session.clear()
                session["user_id"] = user_id
                return redirect(url_for("user_dashboard"))

    return render_template(
        "user_auth.html",
        mode="signup",
        message=message,
        csrf_token=_csrf_token(),
    ), status_code


@app.route("/login", methods=["GET", "POST"])
def user_login():
    if _current_user() is not None:
        return redirect(url_for("user_dashboard"))

    message = None
    status_code = 200
    ip_address = request.remote_addr or "unknown"
    if request.method == "POST":
        if not _csrf_valid():
            message = "Refresh this page and try signing in again."
            status_code = 400
        elif _limit_user_login(ip_address):
            message = "Too many sign-in attempts. Wait five minutes and try again."
            status_code = 429
        else:
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            with _connect_db() as connection:
                user = connection.execute(
                    "SELECT id, password_hash FROM users WHERE email = ? COLLATE NOCASE",
                    (email,),
                ).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                with _user_login_lock:
                    _user_login_attempts.pop(ip_address, None)
                session.clear()
                session["user_id"] = user["id"]
                return redirect(url_for("user_dashboard"))
            _record_user_login_failure(ip_address)
            message = "The email or password was not correct."

    return render_template(
        "user_auth.html",
        mode="login",
        message=message,
        csrf_token=_csrf_token(),
    ), status_code


@app.route("/logout", methods=["POST"])
@_user_required
def user_logout():
    if not _csrf_valid():
        return "Invalid security token. Refresh the page and try again.", 400
    session.clear()
    return redirect(url_for("hello_world"))


@app.route("/dashboard")
@_user_required
def user_dashboard():
    user = _current_user()
    with _connect_db() as connection:
        saved_items = connection.execute(
            "SELECT id, title, filename, content, created_at FROM saved_items "
            "WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (user["id"],),
        ).fetchall()
    response = current_app.make_response(render_template(
        "user_dashboard.html",
        user=user,
        saved_items=saved_items,
        csrf_token=_csrf_token(),
    ))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/dashboard/save", methods=["POST"])
@_user_required
def save_dashboard_item():
    if not _csrf_valid():
        return jsonify(error="Refresh the page and try saving again."), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Invalid saved item."), 400
    title = data.get("title")
    filename = data.get("filename")
    content = data.get("content")
    if (
        not isinstance(title, str)
        or not title.strip()
        or len(title.strip()) > 120
        or not isinstance(filename, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", filename)
        or not isinstance(content, str)
        or not content.strip()
        or len(content.encode("utf-8")) > 100_000
    ):
        return jsonify(error="Check the result title and content, then try again."), 400

    user = _current_user()
    with _connect_db() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM saved_items WHERE user_id = ?",
            (user["id"],),
        ).fetchone()[0]
        if count >= 100:
            return jsonify(error="Your dashboard is full. Delete an older file before saving another."), 409
        cursor = connection.execute(
            "INSERT INTO saved_items (user_id, title, filename, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                user["id"],
                title.strip(),
                filename,
                content,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        item_id = cursor.lastrowid
    return jsonify(id=item_id, message="Saved to your dashboard."), 201


@app.route("/dashboard/files/<int:item_id>/download")
@_user_required
def download_dashboard_item(item_id: int):
    user = _current_user()
    with _connect_db() as connection:
        item = connection.execute(
            "SELECT filename, content FROM saved_items WHERE id = ? AND user_id = ?",
            (item_id, user["id"]),
        ).fetchone()
    if item is None:
        abort(404)
    response = Response(
        item["content"],
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{item["filename"]}"'},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/dashboard/files/<int:item_id>/delete", methods=["POST"])
@_user_required
def delete_dashboard_item(item_id: int):
    if not _csrf_valid():
        return "Invalid security token. Refresh the page and try again.", 400
    user = _current_user()
    with _connect_db() as connection:
        cursor = connection.execute(
            "DELETE FROM saved_items WHERE id = ? AND user_id = ?",
            (item_id, user["id"]),
        )
    if cursor.rowcount == 0:
        abort(404)
    return redirect(url_for("user_dashboard"))


def _admin_session_epoch() -> int:
    with _connect_db() as connection:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE name = ?",
            ("admin_session_epoch",),
        ).fetchone()
    return int(row["value"])


def _limit_admin_login(ip_address: str) -> bool:
    now = monotonic()
    with _admin_login_lock:
        attempts = [
            attempted_at
            for attempted_at in _admin_login_attempts.get(ip_address, [])
            if now - attempted_at < 300
        ]
        _admin_login_attempts[ip_address] = attempts
        return len(attempts) >= 5


def _record_admin_login_failure(ip_address: str) -> None:
    with _admin_login_lock:
        _admin_login_attempts.setdefault(ip_address, []).append(monotonic())


def _limit_admin_password_reset(ip_address: str) -> bool:
    now = monotonic()
    with _admin_password_reset_lock:
        attempts = [
            attempted_at
            for attempted_at in _admin_password_reset_attempts.get(ip_address, [])
            if now - attempted_at < 3600
        ]
        if len(attempts) >= 3:
            _admin_password_reset_attempts[ip_address] = attempts
            return True
        attempts.append(now)
        _admin_password_reset_attempts[ip_address] = attempts
    return False


def _admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if (
            not session.get("admin_authenticated")
            or session.get("admin_session_epoch") != _admin_session_epoch()
        ):
            session.clear()
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


@app.route("/admin", methods=["GET"])
@_admin_required
def admin_dashboard():
    with _connect_db() as connection:
        signups = connection.execute(
            "SELECT id, email, requested_at, email_status FROM trial_signups "
            "ORDER BY requested_at DESC LIMIT 200"
        ).fetchall()
        totals = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN email_status = 'sent' THEN 1 ELSE 0 END) AS sent,
                   SUM(CASE WHEN email_status != 'sent' THEN 1 ELSE 0 END) AS needs_attention
            FROM trial_signups
            """
        ).fetchone()
    return render_template(
        "admin.html",
        signups=signups,
        totals=totals,
        csrf_token=_csrf_token(),
        admin_username=_get_admin_user()["username"],
        email_configured=bool(
            os.environ.get("RESEND_API_KEY")
            and os.environ.get("SPACEZONE_FROM_EMAIL")
        ),
        notice=request.args.get("notice"),
    )


@app.route("/admin/resend/<int:signup_id>", methods=["POST"])
@_admin_required
def admin_resend_welcome_email(signup_id: int):
    if not _csrf_valid():
        return "Invalid security token. Refresh the admin page and try again.", 400

    with _connect_db() as connection:
        signup = connection.execute(
            "SELECT email FROM trial_signups WHERE id = ?",
            (signup_id,),
        ).fetchone()
    if signup is None:
        abort(404)

    email = signup["email"]
    status = _send_welcome_email(email)
    _set_email_status(email, status)
    notices = {
        "sent": "Welcome email sent to the selected address.",
        "not_configured": (
            "Email was not sent: Resend API key and verified sender address "
            "are missing. Configure both in the server environment and restart it."
        ),
        "failed": "Resend could not deliver the email. Check the server logs and Resend settings.",
    }
    return redirect(url_for("admin_dashboard", notice=notices[status]))


@app.route("/admin/setup", methods=["GET", "POST"])
def admin_setup():
    if not _is_local_request():
        abort(403)
    if _get_admin_user() is not None:
        return redirect(url_for("admin_login"))

    csrf_token = _csrf_token()
    message = None
    status_code = 200
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirmation = request.form.get("confirm_password", "")
        if not _csrf_valid():
            message = "Refresh this page and try again."
            status_code = 400
        elif not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
            message = "Use 3–32 letters, numbers, dots, underscores, or hyphens for the username."
            status_code = 400
        elif len(password) < 12:
            message = "Choose a password with at least 12 characters."
            status_code = 400
        elif password != confirmation:
            message = "The passwords do not match."
            status_code = 400
        else:
            try:
                with _connect_db() as connection:
                    connection.execute(
                        "INSERT INTO admin_users (id, username, password_hash) VALUES (1, ?, ?)",
                        (username, generate_password_hash(password)),
                    )
            except sqlite3.IntegrityError:
                return redirect(url_for("admin_login"))
            session.clear()
            session["admin_authenticated"] = True
            session["admin_session_epoch"] = _admin_session_epoch()
            return redirect(url_for("admin_dashboard"))

    return render_template(
        "admin_setup.html",
        csrf_token=csrf_token,
        message=message,
    ), status_code


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_authenticated"):
        return redirect(url_for("admin_dashboard"))

    csrf_token = _csrf_token()
    admin_user = _get_admin_user()
    if admin_user is None:
        return redirect(url_for("admin_setup"))
    message = None
    status_code = 200
    ip_address = request.remote_addr or "unknown"

    if request.method == "POST":
        if not _csrf_valid():
            message = "Refresh the page and try signing in again."
            status_code = 400
        elif _limit_admin_login(ip_address):
            message = "Too many sign-in attempts. Wait five minutes and try again."
            status_code = 429
        else:
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            username_matches = hmac.compare_digest(
                username.casefold(),
                admin_user["username"].casefold(),
            )
            if admin_user["password_hash"] is not None:
                password_matches = check_password_hash(
                    admin_user["password_hash"],
                    password,
                )
            else:
                password_matches = hmac.compare_digest(
                    password,
                    admin_user["environment_password"],
                )
            if username_matches and password_matches:
                with _admin_login_lock:
                    _admin_login_attempts.pop(ip_address, None)
                session.clear()
                session["admin_authenticated"] = True
                session["admin_session_epoch"] = _admin_session_epoch()
                return redirect(url_for("admin_dashboard"))
            _record_admin_login_failure(ip_address)
            message = "The username or password was not correct."

    return render_template(
        "admin_login.html",
        csrf_token=csrf_token,
        message=message,
        server_name=socket.gethostname(),
    ), status_code


@app.route("/admin/forgot-password", methods=["GET", "POST"])
def admin_forgot_password():
    message = None
    status_code = 200
    if request.method == "POST":
        ip_address = request.remote_addr or "unknown"
        if not _csrf_valid():
            message = "Refresh this page and try again."
            status_code = 400
        elif _limit_admin_password_reset(ip_address):
            message = "Too many requests. Please try again later."
            status_code = 429
        else:
            submitted_email = request.form.get("email", "").strip()
            recovery_email = os.environ.get("ADMIN_RECOVERY_EMAIL", "").strip()
            admin_user = _get_admin_user()
            if (
                admin_user is not None
                and recovery_email
                and len(submitted_email) <= 254
                and hmac.compare_digest(
                    submitted_email.casefold(),
                    recovery_email.casefold(),
                )
            ):
                base_url = _admin_reset_base_url()
                if base_url is not None:
                    token = secrets.token_urlsafe(32)
                    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                    expires_at = (
                        datetime.now(timezone.utc) + timedelta(minutes=30)
                    ).isoformat(timespec="seconds")
                    with _connect_db() as connection:
                        connection.execute("DELETE FROM admin_password_resets")
                        connection.execute(
                            """
                            INSERT INTO admin_password_resets
                                (id, token_hash, expires_at)
                            VALUES (1, ?, ?)
                            """,
                            (token_hash, expires_at),
                        )
                    reset_url = (
                        f"{base_url}{url_for('admin_reset_password', token=token)}"
                    )
                    email_status = _send_admin_password_reset_email(
                        recovery_email,
                        reset_url,
                    )
                    if email_status != "sent":
                        with _connect_db() as connection:
                            connection.execute(
                                "DELETE FROM admin_password_resets WHERE token_hash = ?",
                                (token_hash,),
                            )
                        if email_status == "not_configured":
                            current_app.logger.error(
                                "Admin password recovery email unavailable: "
                                "configure RESEND_API_KEY and SPACEZONE_FROM_EMAIL."
                            )

            message = (
                "If that address matches the configured admin recovery email "
                "and email delivery is available, recovery instructions will "
                "be sent. Check your inbox and spam folder."
            )

    response = current_app.make_response(
        (
            render_template(
                "admin_password_recovery.html",
                mode="request",
                message=message,
                token=None,
            ),
            status_code,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route("/admin/reset-password/<token>", methods=["GET", "POST"])
def admin_reset_password(token: str):
    valid_token_format = bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", token))
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect_db() as connection:
        reset_record = None
        if valid_token_format:
            reset_record = connection.execute(
                """
                SELECT token_hash FROM admin_password_resets
                WHERE id = 1 AND token_hash = ? AND expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()

    mode = "reset" if reset_record is not None else "invalid"
    message = None
    status_code = 200 if reset_record is not None else 400

    if request.method == "POST" and reset_record is not None:
        if not _csrf_valid():
            message = "Refresh this page and try again."
            status_code = 400
        else:
            password = request.form.get("password", "")
            confirmation = request.form.get("confirm_password", "")
            if len(password) < 12:
                message = "Choose a password with at least 12 characters."
                status_code = 400
            elif len(password) > 128:
                message = "Choose a password with no more than 128 characters."
                status_code = 400
            elif password != confirmation:
                message = "The passwords do not match."
                status_code = 400
            else:
                password_hash = generate_password_hash(password)
                with _connect_db() as connection:
                    current_record = connection.execute(
                        """
                        SELECT token_hash FROM admin_password_resets
                        WHERE id = 1 AND token_hash = ? AND expires_at > ?
                        """,
                        (token_hash, now),
                    ).fetchone()
                    if current_record is not None:
                        if all(
                            os.environ.get(name)
                            for name in ("ADMIN_USERNAME", "ADMIN_PASSWORD")
                        ):
                            connection.execute(
                                """
                                INSERT INTO app_settings (name, value)
                                VALUES (?, ?)
                                ON CONFLICT(name) DO UPDATE SET value = excluded.value
                                """,
                                ("admin_password_hash_override", password_hash),
                            )
                        else:
                            connection.execute(
                                "UPDATE admin_users SET password_hash = ? WHERE id = 1",
                                (password_hash,),
                            )
                        connection.execute("DELETE FROM admin_password_resets WHERE id = 1")
                        connection.execute(
                            "UPDATE app_settings SET value = "
                            "CAST(value AS INTEGER) + 1 WHERE name = ?",
                            ("admin_session_epoch",),
                        )
                        mode = "complete"
                        message = "Your admin password has been reset. Sign in with the new password."
                    else:
                        mode = "invalid"
                        status_code = 400

                if mode == "complete":
                    session.clear()
                else:
                    status_code = 400

    response = current_app.make_response(
        (
            render_template(
                "admin_password_recovery.html",
                mode=mode,
                message=message,
                token=token,
            ),
            status_code,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route("/admin/logout", methods=["POST"])
@_admin_required
def admin_logout():
    if not _csrf_valid():
        return "Invalid security token. Refresh the admin page and try again.", 400
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/trial", methods=["POST"])
def start_trial():
    if not _csrf_valid():
        return render_template(
            "index.html",
            trial_message="Refresh the page and try your request again.",
            trial_message_type="error",
        ), 400

    email = request.form.get("email", "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return render_template(
            "index.html",
            trial_message="Enter a valid email address and try again.",
            trial_message_type="error",
        ), 400

    client_ip = request.remote_addr or "unknown"
    with _trial_email_lock:
        now = monotonic()
        expired_ips = [
            ip for ip, sent_at in _trial_email_times.items()
            if now - sent_at >= 60
        ]
        for ip in expired_ips:
            del _trial_email_times[ip]
        last_sent_at = _trial_email_times.get(client_ip)
        if last_sent_at is not None and now - last_sent_at < 60:
            rate_limited = True
        else:
            _trial_email_times[client_ip] = now
            rate_limited = False

    if rate_limited:
        return render_template(
            "index.html",
            trial_message="Please wait a minute before requesting another email.",
            trial_message_type="error",
        ), 429

    with _connect_db() as connection:
        connection.execute(
            """
            INSERT INTO trial_signups (email, requested_at, email_status)
            VALUES (?, ?, 'pending')
            ON CONFLICT(email) DO UPDATE SET
                requested_at = excluded.requested_at,
                email_status = 'pending'
            """,
            (email, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )

    email_status = _send_welcome_email(email)
    _set_email_status(email, email_status)
    if email_status == "not_configured":
        current_app.logger.error(
            "Trial welcome email unavailable: configure RESEND_API_KEY and SPACEZONE_FROM_EMAIL."
        )
        return render_template(
            "index.html",
            trial_message=(
                "Your request was saved, but no email was sent because email delivery "
                "isn't configured yet. You can create a free account now; the 10-day "
                "trial and paid plans are not active."
            ),
            trial_message_type="info",
        )
    if email_status == "failed":
        return render_template(
            "index.html",
            trial_message="We couldn't send the email. Please try again later.",
            trial_message_type="error",
        ), 502

    return render_template(
        "index.html",
        trial_message=f"Welcome to Space Zone! Your welcome email is on its way to {email}.",
        trial_message_type="success",
    )

@app.route("/products")
def products():
    return "this is MY Page!"


def _reset_admin_password() -> int:
    if os.environ.get("ADMIN_USERNAME") or os.environ.get("ADMIN_PASSWORD"):
        print("Admin credentials are set in environment variables; update those credentials instead.")
        return 1

    with _connect_db() as connection:
        admin = connection.execute(
            "SELECT username FROM admin_users WHERE id = 1"
        ).fetchone()
    if admin is None:
        print("No admin account exists yet. Visit http://127.0.0.1:5000/admin/setup to create one.")
        return 1

    password = getpass.getpass("New admin password (input hidden): ")
    confirmation = getpass.getpass("Confirm new password (input hidden): ")
    if len(password) < 12:
        print("Password was not changed. Use at least 12 characters.")
        return 1
    if password != confirmation:
        print("Password was not changed because the entries did not match.")
        return 1

    with _connect_db() as connection:
        connection.execute(
            "UPDATE admin_users SET password_hash = ? WHERE id = 1",
            (generate_password_hash(password),),
        )
        connection.execute(
            "UPDATE app_settings SET value = CAST(value AS INTEGER) + 1 "
            "WHERE name = ?",
            ("admin_session_epoch",),
        )
    print(f"Admin password reset for '{admin['username']}'. All admin sessions have been invalidated.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Space Zone or manage its local admin account.")
    parser.add_argument(
        "--reset-admin-password",
        action="store_true",
        help="securely reset the existing admin password from this computer",
    )
    arguments = parser.parse_args()
    if arguments.reset_admin_password:
        raise SystemExit(_reset_admin_password())
    app.run(debug=os.environ.get("FLASK_DEBUG", "").lower() == "true")