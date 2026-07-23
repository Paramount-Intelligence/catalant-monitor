import sys
import time
import smtplib
import json
import os
import re
import html
import socket
import threading
import traceback
import hashlib
import tempfile
import mimetypes
from email.message import EmailMessage

# Ensure UTF-8 output on all platforms (fixes Windows emoji crash)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
from pymongo import MongoClient, UpdateOne
from datetime import datetime, timezone, timedelta

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5)
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

TEST_ERROR_EMAIL_MODE = "--test-error-email" in sys.argv

# ============================
# CONFIGURATION
# ============================
def _resolve_evidence_dir():
    configured = (os.getenv("EVIDENCE_DIR") or "").strip()
    if configured:
        return configured
    return os.path.join(tempfile.gettempdir(), "catalant-evidence")


class Config:
    """Load configuration from environment variables"""
    CATALANT_EMAIL = os.getenv("CATALANT_EMAIL")
    CATALANT_PASSWORD = os.getenv("CATALANT_PASSWORD")
    SMTP_SERVER = os.getenv("SMTP_SERVER")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SENDER_EMAIL = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [e.strip() for e in os.getenv("RECIPIENT_EMAILS", "ahmedghazi459@gmail.com,ahsanuddin3522@gmail.com").split(",") if e.strip()]
    _ERROR_RECIPIENTS_RAW = (
        os.getenv("ERROR_RECIPIENTS")
        or os.getenv("ERROR_RECIPIENT")
        or os.getenv("ERROR_RECIPENT")
        or os.getenv("error_recipent")
        or ""
    )
    ERROR_RECIPIENTS = [
        email.strip()
        for email in _ERROR_RECIPIENTS_RAW.split(",")
        if email.strip()
    ]
    ERROR_EMAIL_COOLDOWN_MINUTES = int(os.getenv("ERROR_EMAIL_COOLDOWN_MINUTES", "30"))
    LOGIN_RETRY_INTERVAL = int(os.getenv("LOGIN_RETRY_INTERVAL", "300"))
    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
    MAX_AGE_MINUTES = int(os.getenv("MAX_AGE_MINUTES", 60))
    HEADLESS = os.getenv("HEADLESS", "False").lower() == "true"
    COOKIES_FILE = os.getenv("COOKIES_FILE", "catalant_cookies.json")
    MONGO_URI    = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    EVIDENCE_DIR = _resolve_evidence_dir()
    EVIDENCE_RETENTION_HOURS = int(os.getenv("EVIDENCE_RETENTION_HOURS", "24"))


# Runtime state for operational alerts (never stores secrets)
_error_alert_lock = threading.Lock()
_error_alert_last_sent = {}
_error_alert_in_progress = False
_monitor_check_count = 0
_monitor_state = "starting"
_last_successful_scan_at = None
_last_login_alert = {"alert_sent": False, "classification": None}
last_scan_issue = None  # set by scan_for_projects; consumed once by main loop
_browser_versions_cache = None
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_BLOCKED_ATTACHMENT_NAMES = {
    ".env", "catalant_cookies.json", "cookies.json", "credentials.json",
}

# ============================
# OPERATIONAL ERROR ALERTS
# ============================
def redact_sensitive_text(value):
    """Redact credentials, tokens, cookies, and Mongo URI userinfo from text."""
    if value is None:
        return ""
    out = str(value)
    for secret in (
        Config.CATALANT_PASSWORD,
        Config.SENDER_PASSWORD,
    ):
        if secret:
            out = out.replace(secret, "[REDACTED_PASSWORD]")
    # MongoDB credentials in URI
    out = re.sub(
        r"(mongodb(?:\+srv)?://)([^:@/\s]+):([^@/\s]+)@",
        r"\1[REDACTED_USER]:[REDACTED_PASSWORD]@",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*", "Bearer [REDACTED_TOKEN]", out)
    out = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "[REDACTED_JWT]",
        out,
    )
    out = re.sub(
        r"(?i)(cookie|set-cookie|authorization|x-api-key|session[_-]?token)\s*[:=]\s*[^;\s]+",
        r"\1=[REDACTED]",
        out,
    )
    out = re.sub(
        r"(?i)(password|passwd|pwd|token|access_token|refresh_token)\s*[=:]\s*[^\s&]+",
        r"\1=[REDACTED]",
        out,
    )
    return out


def _password_fingerprint(password):
    if not password:
        return ""
    return hashlib.sha256(password.encode("utf-8", errors="replace")).hexdigest()[:12]


def _evidence_dir():
    path = Config.EVIDENCE_DIR
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        fallback = os.path.join(tempfile.gettempdir(), "catalant-evidence")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def clean_old_evidence_files():
    """Delete generated Catalant evidence files older than retention period."""
    root = Config.EVIDENCE_DIR
    try:
        if not os.path.isdir(root):
            return
        cutoff = time.time() - max(Config.EVIDENCE_RETENTION_HOURS, 1) * 3600
        removed = 0
        for name in os.listdir(root):
            if not name.startswith("catalant_"):
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except Exception:
                pass
        if removed:
            print(f"  🧹 Evidence cleanup: removed {removed} old file(s)")
    except Exception as e:
        print(f"  ⚠️ Evidence cleanup failed: {redact_sensitive_text(e)}")


def get_browser_versions():
    """Cached Chromium / ChromeDriver version strings for diagnostics."""
    global _browser_versions_cache
    if _browser_versions_cache is not None:
        return _browser_versions_cache

    def _run(cmd):
        try:
            import subprocess
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=8)
            return out.decode("utf-8", errors="replace").strip() or "unknown"
        except FileNotFoundError:
            return "not found"
        except Exception as e:
            return f"failed: {redact_sensitive_text(e)}"

    versions = {
        "chromium": _run(["chromium", "--version"]),
        "chromedriver": _run(["chromedriver", "--version"]),
    }
    if versions["chromium"] in ("not found",) or str(versions["chromium"]).startswith("failed"):
        for cmd in (
            ["chromium-browser", "--version"],
            ["google-chrome", "--version"],
            ["google-chrome-stable", "--version"],
        ):
            out = _run(cmd)
            if out != "not found" and not str(out).startswith("failed"):
                versions["chromium"] = out
                break
    _browser_versions_cache = versions
    return versions


def build_error_signature(context, error):
    error_type = type(error).__name__ if error is not None and not isinstance(error, str) else "Error"
    message = str(error or "").strip()
    return f"{context}|{error_type}|{message}"[:1000]


def should_send_error_alert(signature, force=False):
    """Return (ok_to_send, remaining_seconds). Does not update last-sent timestamp."""
    if force:
        return True, 0
    cooldown_s = max(Config.ERROR_EMAIL_COOLDOWN_MINUTES, 0) * 60
    with _error_alert_lock:
        last = _error_alert_last_sent.get(signature)
        if last is None:
            return True, 0
        elapsed = time.time() - last
        if elapsed >= cooldown_s:
            return True, 0
        return False, int(cooldown_s - elapsed)


def _safe_driver_info(driver):
    info = {"current_url": "", "page_title": ""}
    if not driver:
        return info
    try:
        info["current_url"] = driver.current_url or ""
    except Exception:
        pass
    try:
        info["page_title"] = driver.title or ""
    except Exception:
        pass
    return info


def _safe_page_text(driver, limit=2000):
    if not driver:
        return ""
    try:
        text = driver.find_element(By.TAG_NAME, "body").text or ""
        return redact_sensitive_text(text[:limit])
    except Exception:
        return ""


def create_error_email_html(
    context,
    error,
    details="",
    traceback_text="",
    diagnostics=None,
):
    err_type = type(error).__name__ if error is not None and not isinstance(error, str) else "Error"
    err_msg = html.escape(redact_sensitive_text(str(error) if error is not None else ""))
    versions = get_browser_versions()
    hostname = socket.gethostname()
    now = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S PKT")
    diag = diagnostics or {}

    rows = [
        ("Context", html.escape(str(context))),
        ("Exception", f"{html.escape(err_type)}: {err_msg}"),
        ("Timestamp", html.escape(now)),
        ("Hostname", html.escape(hostname)),
        ("Check #", html.escape(str(_monitor_check_count or "—"))),
        ("Headless", html.escape(str(Config.HEADLESS))),
        ("Chromium", html.escape(versions.get("chromium", "unknown"))),
        ("ChromeDriver", html.escape(versions.get("chromedriver", "unknown"))),
        ("Current URL", html.escape(redact_sensitive_text(diag.get("current_url", "")))),
        ("Page title", html.escape(redact_sensitive_text(diag.get("page_title", "")))),
        ("Account email", html.escape(Config.CATALANT_EMAIL or "(not set)")),
        ("Monitor state", html.escape(str(_monitor_state))),
    ]
    for label, key in (
        ("Configured password length", "configured_password_length"),
        ("Typed password length", "typed_password_length"),
        ("Password fingerprint (configured)", "configured_password_fingerprint"),
        ("Password fingerprint (typed)", "typed_password_fingerprint"),
        ("Password values match", "password_values_match"),
        ("Email field found", "email_field_found"),
        ("Password field found", "password_field_found"),
        ("Submit button found", "submit_button_found"),
        ("Submitted", "submitted"),
        ("CAPTCHA detected", "captcha_detected"),
        ("MFA detected", "mfa_detected"),
        ("Project ID", "project_id"),
        ("Project title", "project_title"),
        ("Project URL", "project_url"),
        ("Selector", "selector"),
        ("Database", "database"),
        ("Collection", "collection"),
        ("Operation", "operation"),
        ("Record count", "record_count"),
    ):
        if key in diag and diag[key] not in (None, ""):
            rows.append((label, html.escape(redact_sensitive_text(str(diag[key])))))

    if details:
        rows.append((
            "Details",
            f"<pre style='white-space:pre-wrap;margin:0;font-size:12px;'>"
            f"{html.escape(redact_sensitive_text(details))}</pre>",
        ))
    if traceback_text:
        rows.append((
            "Traceback",
            f"<pre style='white-space:pre-wrap;margin:0;font-size:11px;color:#7f1d1d;'>"
            f"{html.escape(redact_sensitive_text(traceback_text[:8000]))}</pre>",
        ))

    body_rows = "".join(
        f"<tr>"
        f"<td style='padding:10px 14px;width:200px;background:#fef2f2;border-bottom:1px solid #fecaca;"
        f"font-weight:bold;color:#7f1d1d;vertical-align:top;'>{label}</td>"
        f"<td style='padding:10px 14px;border-bottom:1px solid #fecaca;color:#111;vertical-align:top;'>{value}</td>"
        f"</tr>"
        for label, value in rows
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:720px;margin:24px auto;background:#fff;border-radius:8px;overflow:hidden;
       box-shadow:0 4px 14px rgba(0,0,0,0.12);">
    <div style="background:linear-gradient(135deg,#b91c1c,#ef4444);padding:20px 24px;color:#fff;">
      <p style="margin:0;font-size:11px;letter-spacing:1px;text-transform:uppercase;opacity:0.85;">
        Catalant Project Monitor</p>
      <h2 style="margin:6px 0 0;font-size:22px;">Operational Error Alert</h2>
    </div>
    <div style="padding:18px 20px 24px;">
      <table style="width:100%;border-collapse:collapse;border:1px solid #fecaca;">{body_rows}</table>
      <p style="margin:16px 0 0;font-size:12px;color:#6b7280;">
        This alert was sent only to configured error recipients.
        Passwords, cookies and tokens are never included.
      </p>
    </div>
  </div>
</body></html>"""


def _attachment_allowed(path):
    name = os.path.basename(path).lower()
    if name in _BLOCKED_ATTACHMENT_NAMES or name.endswith(".env"):
        return False
    if "cookie" in name and name.endswith((".json", ".txt")):
        return False
    lower = path.replace("\\", "/").lower()
    if "/.env" in lower or lower.endswith(".env"):
        return False
    return True


def send_error_notification(
    context,
    error,
    details="",
    traceback_text="",
    force=False,
    attachments=None,
    diagnostics=None,
):
    """Send operational error email to ERROR_RECIPIENTS only. Never raises."""
    global _error_alert_in_progress

    try:
        if _error_alert_in_progress:
            print("  ⚠️ Error-email function failed recursively — alert suppressed")
            return False

        if not Config.ERROR_RECIPIENTS:
            print("  ⚠️ Error alert skipped — no ERROR_RECIPIENTS / aliases configured")
            return False

        if not Config.SENDER_EMAIL or not Config.SENDER_PASSWORD or not Config.SMTP_SERVER:
            print("  ⚠️ Error alert skipped — SMTP sender configuration incomplete")
            return False

        signature = build_error_signature(context, error)
        ok, remaining = should_send_error_alert(signature, force=force)
        if not ok:
            print(f"⏳ Error alert suppressed (cooldown {remaining}s remaining): {context}")
            return False

        _error_alert_in_progress = True
        try:
            safe_context = str(context or "UNKNOWN")[:120]
            html_body = create_error_email_html(
                safe_context,
                error,
                details=details,
                traceback_text=traceback_text,
                diagnostics=diagnostics,
            )
            err_type = type(error).__name__ if error is not None and not isinstance(error, str) else "Error"
            plain = (
                f"Catalant Project Monitor — Operational Error Alert\n\n"
                f"Context: {safe_context}\n"
                f"Exception: {err_type}: {redact_sensitive_text(error)}\n"
                f"Timestamp: {datetime.now(PKT).strftime('%Y-%m-%d %H:%M:%S PKT')}\n"
                f"Hostname: {socket.gethostname()}\n"
                f"Check #: {_monitor_check_count or '—'}\n"
                f"Monitor state: {_monitor_state}\n\n"
                f"{redact_sensitive_text(details)}\n\n"
                f"{redact_sensitive_text(traceback_text[:4000])}\n"
            )

            msg = MIMEMultipart("mixed")
            msg["Subject"] = f"🚨 Catalant Monitor Error: {safe_context}"
            msg["From"] = Config.SENDER_EMAIL
            msg["To"] = ", ".join(Config.ERROR_RECIPIENTS)

            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(plain, "plain", "utf-8"))
            alt.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(alt)

            attached = []
            for path in attachments or []:
                if not path or not os.path.isfile(path):
                    continue
                if not _attachment_allowed(path):
                    print(f"  ⚠️ Skipped blocked attachment: {os.path.basename(path)}")
                    continue
                try:
                    size = os.path.getsize(path)
                    if size > _MAX_ATTACHMENT_BYTES:
                        print(f"  ⚠️ Skipped oversized attachment ({size} bytes): {os.path.basename(path)}")
                        continue
                    ctype, _ = mimetypes.guess_type(path)
                    if not ctype:
                        ctype = "application/octet-stream"
                    maintype, subtype = ctype.split("/", 1)
                    with open(path, "rb") as fh:
                        part = MIMEBase(maintype, subtype)
                        part.set_payload(fh.read())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition",
                        f'attachment; filename="{os.path.basename(path)}"',
                    )
                    msg.attach(part)
                    attached.append(os.path.basename(path))
                except Exception as attach_err:
                    print(f"  ⚠️ Could not attach {os.path.basename(path)}: {redact_sensitive_text(attach_err)}")

            with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
                server.send_message(msg)

            with _error_alert_lock:
                _error_alert_last_sent[signature] = time.time()

            suffix = f"\n(attachments: {', '.join(attached)})" if attached else ""
            print(f"📧 Error alert sent to configured error recipient{suffix}")
            return True
        except Exception as smtp_err:
            print(f"⚠️ Failed to send Catalant error alert: {redact_sensitive_text(smtp_err)}")
            return False
        finally:
            _error_alert_in_progress = False
    except Exception as outer:
        print(f"⚠️ Failed to send Catalant error alert: {redact_sensitive_text(outer)}")
        _error_alert_in_progress = False
        return False


def validate_error_email_configuration():
    """Return (ok, missing_names) for operational alert SMTP config."""
    missing = []
    if not Config.SMTP_SERVER:
        missing.append("SMTP_SERVER")
    if not Config.SMTP_PORT:
        missing.append("SMTP_PORT")
    if not Config.SENDER_EMAIL:
        missing.append("SENDER_EMAIL")
    if not Config.SENDER_PASSWORD:
        missing.append("SENDER_PASSWORD")
    if not Config.ERROR_RECIPIENTS:
        missing.extend(["ERROR_RECIPIENTS", "ERROR_RECIPIENT", "ERROR_RECIPENT", "error_recipent"])
    return (len(missing) == 0), missing


def print_startup_banner():
    print("=" * 50)
    print("Catalant Project Monitor")
    print(f"Account: {Config.CATALANT_EMAIL or '(not set)'}")
    print(f"Interval: {Config.CHECK_INTERVAL}s")
    print(f"Project recipients: {', '.join(Config.RECIPIENT_EMAILS) if Config.RECIPIENT_EMAILS else '(none)'}")
    if Config.ERROR_RECIPIENTS:
        print(f"Error recipients: {', '.join(Config.ERROR_RECIPIENTS)}")
        print(f"Error alerts: {', '.join(Config.ERROR_RECIPIENTS)}")
    else:
        print("Error recipients: (none)")
        print("Error alerts: disabled")
    print(f"Error cooldown: {Config.ERROR_EMAIL_COOLDOWN_MINUTES} minutes")
    print(f"Headless: {Config.HEADLESS}")
    print("=" * 50)
    ok, missing = validate_error_email_configuration()
    if not ok:
        print(f"⚠️ Operational alerts disabled — missing: {', '.join(missing)}")


def run_test_error_email():
    """Force-send a test operational alert; skip Selenium/Mongo."""
    print_startup_banner()
    ok, missing = validate_error_email_configuration()
    if not ok:
        print(f"❌ Cannot send test — missing: {', '.join(missing)}")
        return 2
    success = send_error_notification(
        context="TEST_ERROR_NOTIFICATION",
        error=RuntimeError("This is a test Catalant operational error notification."),
        details=(
            "Generated by --test-error-email. "
            "No scraper failure occurred."
        ),
        force=True,
        diagnostics={"monitor_state": "test"},
    )
    print("✅ Test error email sent" if success else "❌ Test error email failed")
    return 0 if success else 1


def save_login_failure_evidence(driver, diagnostics=None, prefix="catalant_login_failure"):
    """Save screenshot + HTML + safe JSON. Returns list of existing file paths."""
    ts = datetime.now(PKT).strftime("%Y%m%d_%H%M%S")
    base = os.path.join(_evidence_dir(), f"{prefix}_{ts}")
    paths = {"png": f"{base}.png", "html": f"{base}.html", "json": f"{base}.json"}
    out = []
    if driver:
        try:
            driver.save_screenshot(paths["png"])
            out.append(paths["png"])
        except Exception as e:
            print(f"  ⚠️ Screenshot failed: {redact_sensitive_text(e)}")
        try:
            with open(paths["html"], "w", encoding="utf-8", errors="replace") as fh:
                fh.write(driver.page_source or "")
            out.append(paths["html"])
        except Exception as e:
            print(f"  ⚠️ HTML capture failed: {redact_sensitive_text(e)}")
    try:
        safe = diagnostics or {}
        with open(paths["json"], "w", encoding="utf-8") as fh:
            json.dump(safe, fh, indent=2, default=str)
        out.append(paths["json"])
    except Exception as e:
        print(f"  ⚠️ JSON diagnostics failed: {redact_sensitive_text(e)}")
    return out


def classify_login_failure(driver, exc=None):
    """Best-effort classification of Catalant login failure."""
    text = ""
    url = ""
    title = ""
    if driver:
        try:
            url = (driver.current_url or "").lower()
        except Exception:
            pass
        try:
            title = (driver.title or "").lower()
        except Exception:
            pass
        text = (_safe_page_text(driver, 4000) or "").lower()
    blob = f"{text} {url} {title} {str(exc or '').lower()}"
    if isinstance(exc, TimeoutException) or "timeout" in blob:
        # Prefer more specific page signals when present
        pass
    if any(w in blob for w in ("captcha", "recaptcha", "hcaptcha", "verify you are human")):
        return "CAPTCHA_REQUIRED"
    if any(w in blob for w in ("two-factor", "2fa", "mfa", "verification code", "one-time")):
        return "MFA_REQUIRED"
    if any(w in blob for w in ("locked", "disabled", "suspended")):
        return "ACCOUNT_LOCKED"
    if any(w in blob for w in ("access denied", "forbidden", "not authorized")):
        return "ACCESS_DENIED"
    if any(w in blob for w in ("cors", "preflight")):
        return "CORS_PREFLIGHT_FAILED"
    if any(w in blob for w in ("invalid", "incorrect", "wrong password", "authentication failed", "login failed")):
        return "INVALID_CREDENTIALS_RESPONSE"
    if isinstance(exc, TimeoutException) or "timeout" in str(exc or "").lower():
        return "LOGIN_TIMEOUT"
    return "UNKNOWN"


# ============================
# SESSION MANAGEMENT
# ============================
_mongo_client = None

def _get_session_collection():
    """MongoDB collection for storing Catalant session cookies."""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI)
    return _mongo_client["office_monitor"]["sessions"]

def save_cookies(driver):
    """Save session cookies to MongoDB AND local file as fallback."""
    cookies = driver.get_cookies()
    cookie_count = len(cookies) if cookies is not None else 0
    # MongoDB (primary)
    try:
        _get_session_collection().update_one(
            {"_id": "catalant_cookies"},
            {"$set": {"cookies": cookies, "saved_at": datetime.now(timezone.utc)}},
            upsert=True
        )
    except Exception as e:
        print(f"  ⚠️ Could not save cookies to MongoDB: {redact_sensitive_text(e)}")
        send_error_notification(
            "COOKIE_SAVE:MONGODB",
            e,
            details=f"cookie_count={cookie_count}\nsource=mongodb",
            traceback_text=traceback.format_exc(),
            diagnostics={**_safe_driver_info(driver), "operation": "cookie_save_mongo", "record_count": cookie_count},
        )
    # Local file fallback
    try:
        with open(Config.COOKIES_FILE, 'w') as f:
            json.dump(cookies, f)
    except Exception as e:
        print(f"  ⚠️ Could not save cookies to local file: {redact_sensitive_text(e)}")
        send_error_notification(
            "COOKIE_SAVE:LOCAL_FILE",
            e,
            details=f"path={Config.COOKIES_FILE}\ncookie_count={cookie_count}",
            traceback_text=traceback.format_exc(),
            diagnostics={**_safe_driver_info(driver), "operation": "cookie_save_file", "record_count": cookie_count},
        )
    return True

def load_cookies(driver):
    """Load cookies from MongoDB first, fall back to local file."""
    cookies = None
    source = None
    # Try MongoDB first
    try:
        doc = _get_session_collection().find_one({"_id": "catalant_cookies"})
        if doc and doc.get("cookies"):
            cookies = doc["cookies"]
            source = "mongodb"
            print("  Loaded cookies from MongoDB")
    except Exception as e:
        print(f"  ⚠️ Could not load cookies from MongoDB: {redact_sensitive_text(e)}")
        send_error_notification(
            "COOKIE_LOAD:MONGODB",
            e,
            details="source=mongodb",
            traceback_text=traceback.format_exc(),
            diagnostics={**_safe_driver_info(driver), "operation": "cookie_load_mongo"},
        )
    # Fall back to local file
    if not cookies:
        if not os.path.exists(Config.COOKIES_FILE):
            return False
        try:
            with open(Config.COOKIES_FILE, 'r') as f:
                cookies = json.load(f)
            source = "local_file"
            print("  Loaded cookies from local file")
        except Exception as e:
            print(f"  ⚠️ Could not load cookies from local file: {redact_sensitive_text(e)}")
            send_error_notification(
                "COOKIE_LOAD:LOCAL_FILE",
                e,
                details=f"path={Config.COOKIES_FILE}",
                traceback_text=traceback.format_exc(),
                diagnostics={**_safe_driver_info(driver), "operation": "cookie_load_file"},
            )
            return False
    if not cookies:
        return False
    try:
        driver.get("https://app.gocatalant.com")
        time.sleep(2)
        driver.delete_all_cookies()
        for cookie in cookies:
            if 'domain' in cookie and '.gocatalant.com' in cookie['domain']:
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass
        return True
    except Exception as e:
        print(f"  ⚠️ Could not restore cookies in browser: {redact_sensitive_text(e)}")
        send_error_notification(
            "COOKIE_RESTORE:BROWSER",
            e,
            details=f"source={source}\ncookie_count={len(cookies)}",
            traceback_text=traceback.format_exc(),
            diagnostics={**_safe_driver_info(driver), "operation": "cookie_restore", "record_count": len(cookies)},
        )
        return False

def perform_login(driver):
    """Perform login to Catalant. Returns dict with success/classification/alert_sent."""
    global _last_login_alert
    _last_login_alert = {"alert_sent": False, "classification": None}
    email_found = password_found = submit_found = submitted = False
    typed_password = Config.CATALANT_PASSWORD or ""
    configured_password = Config.CATALANT_PASSWORD or ""
    try:
        driver.get("https://app.gocatalant.com/c/_/u/0/dashboard/")
        time.sleep(3)

        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.NAME, "email"))
        )
        email_found = True
        password_found = True
        driver.find_element(By.NAME, "email").send_keys(Config.CATALANT_EMAIL)
        driver.find_element(By.NAME, "password").send_keys(typed_password)
        submit = driver.find_element(By.XPATH, "//button[contains(text(), 'Login') or @type='submit']")
        submit_found = True
        submit.click()
        submitted = True

        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".need-card-inline-name"))
        )

        save_cookies(driver)
        _navigate_to_search(driver)
        print("Login successful -> Search Projects")
        return {"success": True, "classification": None, "alert_sent": False, "message": "ok"}
    except Exception as e:
        print(f"❌ Login failed: {redact_sensitive_text(e)}")
        classification = classify_login_failure(driver, e)
        context = f"LOGIN_FAILURE:{classification}"
        info = _safe_driver_info(driver)
        diagnostics = {
            **info,
            "result": classification,
            "headless": Config.HEADLESS,
            "email_field_found": email_found,
            "password_field_found": password_found,
            "submit_button_found": submit_found,
            "submitted": submitted,
            "configured_password_length": len(configured_password),
            "typed_password_length": len(typed_password),
            "configured_password_fingerprint": _password_fingerprint(configured_password),
            "typed_password_fingerprint": _password_fingerprint(typed_password),
            "password_values_match": configured_password == typed_password,
            "captcha_detected": classification == "CAPTCHA_REQUIRED",
            "mfa_detected": classification == "MFA_REQUIRED",
            "visible_error": _safe_page_text(driver, 500),
        }
        attachments = []
        try:
            attachments = save_login_failure_evidence(driver, diagnostics=diagnostics)
        except Exception as ev_err:
            print(f"  ⚠️ Evidence capture failed: {redact_sensitive_text(ev_err)}")
        details = (
            f"Login classification: {classification}\n"
            f"Current URL: {info.get('current_url')}\n"
            f"Page title: {info.get('page_title')}\n"
            f"Email field found: {email_found}\n"
            f"Password field found: {password_found}\n"
            f"Submit button found: {submit_found}\n"
            f"Submitted: {submitted}\n"
            f"Configured password length: {len(configured_password)}\n"
            f"Typed password length: {len(typed_password)}\n"
            f"Password fingerprints match: {configured_password == typed_password}\n"
            f"Evidence files: {', '.join(attachments) if attachments else 'none'}\n"
        )
        alert_sent = send_error_notification(
            context,
            e,
            details=details,
            traceback_text=traceback.format_exc(),
            attachments=attachments,
            diagnostics=diagnostics,
        )
        _last_login_alert = {"alert_sent": bool(alert_sent), "classification": classification}
        return {
            "success": False,
            "classification": classification,
            "alert_sent": bool(alert_sent),
            "message": redact_sensitive_text(e),
        }

# ============================
# PROJECT EXTRACTION
# ============================
def _first_platform_category(cat_text):
    """Take the top-level category from a path like 'A > B > C' or 'A | B'."""
    if not cat_text:
        return ""
    parts = re.split(r"\s*[>|]\s*", cat_text.strip())
    return parts[0].strip() if parts and parts[0].strip() else ""


def extract_project_data(card):
    """Extract data from a project card - returns None if invalid"""
    try:
        # Required: Title
        title_elem = card.find_element(By.CSS_SELECTOR, ".need-card-inline-name .line-clamp-2")
        title = title_elem.text.strip()
        if not title:
            return None
        
        # Required: Project ID
        try:
            like_button = card.find_element(By.CSS_SELECTOR, "[data-ajax-post*='need/']")
            match = re.search(r'/need/([^/]+)/', like_button.get_attribute("data-ajax-post"))
            if not match:
                return None
            project_id = match.group(1)
        except:
            return None
        
        # Optional: Platform Category (first segment of category path)
        platform_category = ""
        for sel in (
            ".text-gray.text-size-14.line-height-170",
            ".need-card-inline-pools .small.text-muted",
            ".need-card-inline-pools .text-muted",
        ):
            try:
                cat_text = card.find_element(By.CSS_SELECTOR, sel).text.strip()
                platform_category = _first_platform_category(cat_text)
                if platform_category:
                    break
            except Exception:
                pass
        
        description = ""
        try:
            description = card.find_element(By.CSS_SELECTOR, ".need-card-inline-details .line-clamp-2").text.strip()
        except:
            pass
        
        location = ""
        try:
            loc_text = card.find_element(By.CSS_SELECTOR, ".text-gray-25.font-weight-semibold").text.strip()
            location = loc_text if loc_text else ""
        except:
            pass

        time_posted = "Unknown"
        try:
            time_elems = card.find_elements(By.XPATH, ".//div[contains(@class, 'small') and contains(@class, 'text-gray-20') and contains(@class, 'mt-1')]//span[contains(text(), 'Posted')]")
            if time_elems:
                time_posted = time_elems[0].text.replace("Posted", "").replace("ago", "").strip()
        except:
            pass

        # Optional: Budget
        budget = ""
        try:
            budget = card.find_element(By.CSS_SELECTOR, ".need-card-inline-budget").text.strip()
        except:
            pass
        if not budget:
            try:
                for el in card.find_elements(By.XPATH, ".//*[contains(text(),'$')]"):
                    t = el.text.strip()
                    if '$' in t and len(t) < 60:
                        budget = t
                        break
            except:
                pass

        # Optional: Duration / Project Length
        duration = ""
        try:
            duration = card.find_element(By.CSS_SELECTOR, ".need-card-inline-duration").text.strip()
        except:
            pass
        if not duration:
            try:
                for el in card.find_elements(By.XPATH, ".//span[contains(@class,'text-gray') or contains(@class,'small')]"):
                    t = el.text.strip()
                    if any(w in t.lower() for w in ("week", "month", "day")) and 2 < len(t) < 40:
                        duration = t
                        break
            except:
                pass

        status = "Posted"
        try:
            card.find_element(By.CSS_SELECTOR, ".badge-success")
            status = "New Project"
        except:
            pass

        # Optional: Direct project URL
        url = f"https://app.gocatalant.com/c/_/u/0/need/{project_id}/"
        try:
            link = card.find_element(By.CSS_SELECTOR, "a[href*='need']")
            href = link.get_attribute("href") or ""
            if href and "need" in href:
                url = href
        except:
            pass

        return {
            "id": project_id,
            "title": title,
            "description": description,
            "location": location,
            "budget": budget,
            "duration": duration,
            "platform_category": platform_category,
            "time_posted": time_posted,
            "status": status,
            "url": url,
            "detected_at": datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")
        }
    except:
        return None

def scan_for_projects(driver):
    """Scan Search Projects page for project cards - returns only valid projects.
    Sets last_scan_issue when a classified scan failure occurs (one alert owner).
    """
    global last_scan_issue, _last_successful_scan_at
    last_scan_issue = None
    selector = ".need-card-inline-name"
    try:
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )

        # Get all card blocks that contain a project title
        all_cards = driver.find_elements(By.CSS_SELECTOR, "div.card-block")
        project_cards = [c for c in all_cards if c.find_elements(By.CSS_SELECTOR, ".need-card-inline-name")]

        projects = []
        for card in project_cards:
            project = extract_project_data(card)
            if project and project.get('title') and project.get('id'):
                projects.append(project)

        print(f"✅ Extracted {len(projects)} valid projects")
        if projects:
            _last_successful_scan_at = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S PKT")
        elif project_cards:
            # Cards present but extraction failed for all — selector/structure issue
            err = RuntimeError("Project cards present but no valid projects extracted")
            last_scan_issue = {
                "classification": "SCAN:SELECTOR_FAILURE",
                "error": err,
                "alert_sent": False,
                "selector": selector,
            }
            last_scan_issue["alert_sent"] = send_error_notification(
                "SCAN:SELECTOR_FAILURE",
                err,
                details=f"card_blocks={len(all_cards)} named_cards={len(project_cards)}",
                diagnostics={**_safe_driver_info(driver), "selector": selector},
            )
        return projects
    except TimeoutException as e:
        print("⏳ Timeout waiting for projects")
        last_scan_issue = {
            "classification": "SCAN:TIMEOUT",
            "error": e,
            "alert_sent": False,
            "selector": selector,
        }
        last_scan_issue["alert_sent"] = send_error_notification(
            "SCAN:TIMEOUT",
            e,
            details=f"selector={selector}\npage_text={_safe_page_text(driver, 1500)}",
            traceback_text=traceback.format_exc(),
            diagnostics={**_safe_driver_info(driver), "selector": selector},
        )
        return []
    except Exception as e:
        print(f"❌ Error scanning: {redact_sensitive_text(e)}")
        last_scan_issue = {
            "classification": "SCAN:PROJECT_LIST_FAILED",
            "error": e,
            "alert_sent": False,
            "selector": selector,
        }
        last_scan_issue["alert_sent"] = send_error_notification(
            "SCAN:PROJECT_LIST_FAILED",
            e,
            details=f"selector={selector}\npage_text={_safe_page_text(driver, 1500)}",
            traceback_text=traceback.format_exc(),
            diagnostics={**_safe_driver_info(driver), "selector": selector},
        )
        return []

# ============================
# PROJECT DATABASE (MongoDB)
# ============================
_projects_client = None

def _get_collection():
    """Return the MongoDB projects collection, reusing the client across calls."""
    global _projects_client
    try:
        if _projects_client is None:
            _projects_client = MongoClient(Config.MONGO_URI)
        return _projects_client["office_monitor"]["projects"]
    except Exception as e:
        print(f"⚠️ MongoDB connection failed: {redact_sensitive_text(e)}")
        send_error_notification(
            "DATABASE:INITIALIZATION_FAILED",
            e,
            details="database=office_monitor collection=projects",
            traceback_text=traceback.format_exc(),
            diagnostics={"database": "office_monitor", "collection": "projects", "operation": "connect"},
        )
        raise

def init_db():
    """Ensure a unique index on 'project_id' exists."""
    try:
        _get_collection().create_index("project_id", unique=True, name="idx_project_id_unique")
    except Exception as e:
        # Duplicate index name is harmless; alert only unexpected failures
        msg = str(e).lower()
        if "already exists" in msg or "indexoptionsconflict" in msg or "equivalent index" in msg:
            return
        print(f"⚠️ Index creation issue: {redact_sensitive_text(e)}")
        send_error_notification(
            "DATABASE:INDEX_CREATION_FAILED",
            e,
            details="index=idx_project_id_unique",
            traceback_text=traceback.format_exc(),
            diagnostics={"database": "office_monitor", "collection": "projects", "operation": "create_index"},
        )

def db_is_cold_start():
    """True if the collection has no documents (first ever run)."""
    return _get_collection().find_one({}, {"_id": 1}) is None

def get_seen_ids():
    """Return set of all project IDs already in DB."""
    try:
        docs = _get_collection().find({}, {"project_id": 1, "_id": 0})
        return {d["project_id"] for d in docs if d.get("project_id")}
    except Exception as e:
        print(f"⚠️ Project lookup failed: {redact_sensitive_text(e)}")
        send_error_notification(
            "DATABASE:PROJECT_LOOKUP_FAILED",
            e,
            details="operation=get_seen_ids",
            traceback_text=traceback.format_exc(),
            diagnostics={"database": "office_monitor", "collection": "projects", "operation": "lookup"},
        )
        return set()

def insert_project(project, emailed=True):
    """Upsert one project record. Silently skips most fields if ID already exists.
    Always updates platform_category when a non-empty value is scraped.
    """
    try:
        cat = (project.get("platform_category") or "").strip()
        doc = {
            "project_id":         project.get("id"),
            "title":              project.get("title"),
            "description":        project.get("description"),
            "location":           project.get("location"),
            "budget":             project.get("budget"),
            "duration":           project.get("duration"),
            "start_date":         project.get("start_date"),
            "project_length":     project.get("project_length"),
            "location_pref":      project.get("location_pref"),
            "level_of_support":   project.get("level_of_support"),
            "industry":           project.get("industry"),
            "contracting":        project.get("contracting"),
            "time_posted":        project.get("time_posted"),
            "status":             project.get("status"),
            "url":                project.get("url"),
            "detected_at":        project.get("detected_at"),
            "platform":           "catalant",
            "emailed":            bool(emailed),
        }
        # Cannot put the same path in both $set and $setOnInsert
        update = {"$setOnInsert": dict(doc)}
        if cat:
            update["$set"] = {"platform_category": cat}
        else:
            update["$setOnInsert"]["platform_category"] = ""
        _get_collection().update_one(
            {"project_id": doc["project_id"]},
            update,
            upsert=True,
        )
    except Exception as e:
        # Duplicate key on concurrent insert is normal dedup — skip alert
        msg = str(e).lower()
        if "duplicate key" in msg or "e11000" in msg:
            return
        print(f"⚠️ DB insert failed: {redact_sensitive_text(e)}")
        send_error_notification(
            "DATABASE:PROJECT_INSERT_FAILED",
            e,
            details=f"project_id={project.get('id')}\nurl={project.get('url')}",
            traceback_text=traceback.format_exc(),
            diagnostics={
                "database": "office_monitor",
                "collection": "projects",
                "operation": "insert",
                "project_id": project.get("id"),
                "project_url": project.get("url"),
                "project_title": project.get("title"),
            },
        )

def bulk_insert_projects(projects, emailed=False):
    """Upsert many projects at once (used for cold-start seeding).
    Also $set platform_category on existing docs when scraped.
    """
    try:
        ops = []
        for p in projects:
            if not p.get("id"):
                continue
            cat = (p.get("platform_category") or "").strip()
            doc = {
                "project_id":  p.get("id"),
                "title":       p.get("title"),
                "description": p.get("description"),
                "location":    p.get("location"),
                "budget":      p.get("budget"),
                "duration":    p.get("duration"),
                "time_posted": p.get("time_posted"),
                "status":      p.get("status"),
                "url":         p.get("url"),
                "detected_at": p.get("detected_at"),
                "platform":    "catalant",
                "emailed":     bool(emailed),
            }
            update = {"$setOnInsert": dict(doc)}
            if cat:
                update["$set"] = {"platform_category": cat}
            else:
                update["$setOnInsert"]["platform_category"] = ""
            ops.append(UpdateOne({"project_id": doc["project_id"]}, update, upsert=True))
        if ops:
            result = _get_collection().bulk_write(ops, ordered=False)
            modified = getattr(result, "modified_count", 0)
            print(
                f"  DB: inserted {result.upserted_count} records, "
                f"updated category on {modified} (emailed={'yes' if emailed else 'no'})"
            )
    except Exception as e:
        msg = str(e).lower()
        if "duplicate key" in msg or "e11000" in msg:
            return
        print(f"⚠️ DB bulk insert failed: {redact_sensitive_text(e)}")
        send_error_notification(
            "DATABASE:BULK_INSERT_FAILED",
            e,
            details=f"record_count={len(projects)}",
            traceback_text=traceback.format_exc(),
            diagnostics={
                "database": "office_monitor",
                "collection": "projects",
                "operation": "bulk_insert",
                "record_count": len(projects),
            },
        )

def parse_posted_minutes(time_str):
    """Convert a scraped 'time_posted' string into minutes. Returns None if unparseable."""
    if not time_str or time_str == "Unknown":
        return None
    s = time_str.lower().strip()
    if any(w in s for w in ("just", "moment", "second")):
        return 0
    match = re.search(r'(\d+)\s*(minute|hour|day|week|month)', s)
    if not match:
        return None
    val, unit = int(match.group(1)), match.group(2)
    return val * {"minute": 1, "hour": 60, "day": 1440, "week": 10080, "month": 43200}[unit]


def filter_new_projects(all_projects, seen_ids):
    """Filter out already-seen IDs."""
    result = []
    for p in all_projects:
        if not p.get("id") or p["id"] in seen_ids:
            continue
        result.append(p)
    return result

# ============================
# DETAIL PAGE FETCH
# ============================
def fetch_project_details(driver, url):
    """Navigate to a Catalant project detail page and extract full information."""
    details = {}
    try:
        driver.get(url)

        # Wait for the structured fields to appear (JS-heavy SPA needs time)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH,
                    "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'contracting')]"
                ))
            )
        except TimeoutException:
            time.sleep(8)

        # Try CSS selectors for full description
        for sel in [".need-description", ".description-body", "[class*='description-body']",
                    ".need-detail-description", ".project-description", "[class*='need-description']"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                t = el.text.strip()
                if len(t) > 50:
                    details["description"] = t
                    break
            except Exception:
                pass

        body_text = driver.find_element(By.TAG_NAME, "body").text
        # Normalize non-breaking spaces and Windows line endings
        body_text = body_text.replace('\u00a0', ' ').replace('\r\n', '\n').replace('\r', '\n')

        # Fallback: extract description from body text between section headers
        if not details.get("description"):
            m = re.search(
                r'(?:Summary\s*\n+)?Description\s*\n([\s\S]+?)(?=\n(?:Project Logistics|Budget|Expert Preferences|Contracting|Other Details)|\Z)',
                body_text, re.IGNORECASE
            )
            if m:
                txt = m.group(1).strip()
                if len(txt) > 30:
                    details["description"] = txt

        # ── Inline "Label: Value" extraction ─────────────────────────────────
        # The detail page renders all fields as "Label: Value" on a single line.
        inline_patterns = [
            ("start_date",       r'Start Date:\s*(.+)'),
            ("project_length",   r'Timeline:\s*(.+)'),          # page uses "Timeline"
            ("level_of_support", r'(?:Expert Type|Level of Support):\s*(.+)|^(Independent Expert|Open to Both|Consulting Firm|Both)$'),
            ("industry",         r'Industry:\s*(.+)'),           # page uses "Industry"
            ("contracting",      r'Contracting Process:\s*(.+)'),
        ]
        for field, pattern in inline_patterns:
            if details.get(field):
                continue
            m = re.search(pattern, body_text, re.IGNORECASE | re.MULTILINE)
            if m:
                # Handle alternation groups — pick first non-None group
                val = next((g for g in m.groups() if g), None) if m.groups() else m.group(0)
                if val:
                    val = val.strip()
                    if val:
                        details[field] = val

        # Location: two "Location:" lines exist — description prose (first) and
        # the structured sidebar (last). Always take the LAST occurrence.
        if not details.get("location_pref"):
            matches = re.findall(r'^Location:\s*(.+)', body_text, re.IGNORECASE | re.MULTILINE)
            if matches:
                details["location_pref"] = matches[-1].strip()

        # Budget: "Project Budget:\n<value>" — value is on the NEXT line.
        # Use [ \t]* (not \s*) before \n to avoid eating the newline itself.
        if not details.get("detail_budget"):
            m = re.search(r'Project Budget:[ \t]*\n[ \t]*(.+)', body_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if val and val.lower() != "not provided":
                    details["detail_budget"] = val

        # Platform Category: first segment of "A > B > C" breadcrumb on detail page
        if not details.get("platform_category"):
            for sel in (
                ".text-gray.text-size-14.line-height-170",
                "[class*='line-height-170']",
            ):
                try:
                    for el in driver.find_elements(By.CSS_SELECTOR, sel):
                        cat_text = el.text.strip()
                        if ">" in cat_text or "|" in cat_text:
                            platform_category = _first_platform_category(cat_text)
                            if platform_category:
                                details["platform_category"] = platform_category
                                break
                    if details.get("platform_category"):
                        break
                except Exception:
                    pass

    except Exception as e:
        print(f"  ⚠️ Detail fetch failed: {redact_sensitive_text(e)}")
        ctx = "PROJECT_DETAIL:TIMEOUT" if isinstance(e, TimeoutException) else "PROJECT_DETAIL:FETCH_FAILED"
        send_error_notification(
            ctx,
            e,
            details=f"url={url}",
            traceback_text=traceback.format_exc(),
            diagnostics={
                **_safe_driver_info(driver),
                "project_url": url,
                "operation": "fetch_project_details",
            },
        )
    return details


# ============================
# EMAIL NOTIFICATIONS
# ============================
def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section_header(icon, title, color):
    return (
        f'<tr><td colspan="2" style="padding:14px 16px 6px;background:{color};'
        f'color:#fff;font-size:12px;font-weight:bold;'
        f'text-transform:uppercase;letter-spacing:1px;">'
        f'{icon}&nbsp; {title}</td></tr>'
    )


def _row(label, value, alt=False, bold_value=False):
    if not value:
        return ""
    bg   = "background:#f8f9fa;" if alt else "background:#fff;"
    bold = "font-weight:bold;" if bold_value else ""
    return (
        f"<tr>"
        f"<td style='padding:9px 16px;color:#555;width:200px;{bg}border-bottom:1px solid #eee;'>"
        f"<strong>{_esc(label)}</strong></td>"
        f"<td style='padding:9px 16px;{bg}{bold}border-bottom:1px solid #eee;'>{_esc(str(value))}</td>"
        f"</tr>"
    )


def create_email_html(project):
    title         = project.get('title', 'Untitled Project')
    url           = project.get('url', 'https://app.gocatalant.com/c/_/u/0/dashboard/')
    time_posted   = project.get('time_posted', '')
    status        = project.get('status', '')
    detected_at   = project.get('detected_at', '')
    project_id    = project.get('id', '')
    description   = project.get('description', '')
    start_date    = project.get('start_date', '')
    proj_length   = project.get('project_length', '') or project.get('duration', '')
    location_pref = project.get('location_pref', '') or project.get('location', '')
    contracting   = project.get('contracting', '')
    budget        = project.get('budget', '') or project.get('detail_budget', '') or 'Not provided'
    support_level = project.get('level_of_support', '')
    industry      = project.get('industry', '')

    hdr_grad   = "linear-gradient(135deg,#1a6b3c,#27ae60)"
    sec_desc   = "#1a6b3c"
    sec_logist = "#166534"
    sec_budget = "#1d4ed8"
    sec_expert = "#7c3aed"
    btn_color  = "#27ae60"

    badge = ""
    if status == "New Project":
        badge = ("<span style='display:inline-block;background:#e74c3c;color:#fff;"
                 "padding:4px 12px;border-radius:3px;font-size:12px;font-weight:bold;"
                 "margin-bottom:12px;'>🆕 New Project</span>")

    desc_html = ""
    if description:
        paragraphs = _esc(description).replace("\n\n", "|||").replace("\n", " ")
        paras = [f"<p style='margin:0 0 10px;'>{p}</p>" for p in paragraphs.split("|||")]
        desc_html = "".join(paras)

    desc_section = ""
    if desc_html:
        desc_section = (
            _section_header('📋', 'Description', sec_desc) +
            f"<tr><td colspan='2' style='padding:14px 16px;background:#f9fafb;"
            f"font-size:14px;line-height:1.75;color:#333;border-bottom:2px solid #e5e7eb;'>"
            f"{desc_html}</td></tr>"
        )

    logistics_rows = (
        _row("Start Date",              start_date or "TBD",              alt=False) +
        _row("Expected Project Length", proj_length or "Not specified",   alt=True) +
        _row("Location Preference",     location_pref or "Not specified", alt=False) +
        _row("Contracting Process",     contracting or "Standard",        alt=True)
    )
    logistics_section = _section_header('📦', 'Project Logistics', sec_logist) + logistics_rows

    budget_section = (
        _section_header('💰', 'Budget', sec_budget) +
        _row("Project Budget", budget, bold_value=bool(project.get('budget')))
    )

    expert_rows = (
        _row("Level of Support",            support_level or "Not specified", alt=False) +
        _row("Desired Industry Background", industry      or "Not specified", alt=True)
    )
    expert_section = _section_header('👤', 'Expert Preferences', sec_expert) + expert_rows

    meta_rows = (
        _row("Posted",      f"{time_posted} ago" if time_posted and time_posted != "Unknown" else "—", alt=False) +
        _row("Detected at", detected_at, alt=True) +
        _row("Project ID",  project_id, alt=False)
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif;color:#333;">
  <div style="max-width:700px;margin:30px auto;background:#fff;border-radius:10px;
       overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.12);">

    <div style="background:{hdr_grad};padding:24px 28px;">
      <p style="margin:0;color:rgba(255,255,255,0.75);font-size:11px;
          letter-spacing:1.5px;text-transform:uppercase;">Catalant Project Monitor</p>
      <h2 style="margin:6px 0 0;color:#fff;font-size:24px;font-weight:700;">🚀 New Project Alert</h2>
    </div>

    <div style="padding:22px 28px 4px;">
      <h3 style="margin:0 0 10px;color:#1a252f;font-size:20px;line-height:1.4;">{_esc(title)}</h3>
      {badge}
    </div>

    <div style="padding:0 28px 28px;">
      <table style="width:100%;border-collapse:collapse;font-size:14px;
             border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
        {desc_section}
        {logistics_section}
        {budget_section}
        {expert_section}
        {_section_header('🕒', 'Detection Info', '#6b7280')}
        {meta_rows}
      </table>
      <div style="text-align:center;margin-top:28px;">
        <a href="{url}" style="display:inline-block;background:{btn_color};color:#fff;
                  padding:14px 36px;text-decoration:none;border-radius:6px;
                  font-weight:bold;font-size:15px;letter-spacing:0.3px;">
          View Full Project on Catalant →
        </a>
      </div>
    </div>

    <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #eee;
         font-size:12px;color:#999;text-align:center;">
      Catalant Project Monitor &nbsp;|&nbsp; Automated alert &nbsp;|&nbsp; {detected_at}
    </div>
  </div>
</body></html>"""

def send_notification(project):
    """Send email notification for a new project"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔔 Catalant: {project.get('title', 'New Project')}"
        msg["From"] = Config.SENDER_EMAIL
        msg["To"] = ", ".join(Config.RECIPIENT_EMAILS)
        
        msg.attach(MIMEText(create_email_html(project), "html"))
        
        with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT) as server:
            server.starttls()
            server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
            server.send_message(msg)
        
        print(f"📧 Email sent: {project.get('title', 'Unknown')[:50]}...")
        return True
    except Exception as e:
        print(f"❌ Email failed: {redact_sensitive_text(e)}")
        send_error_notification(
            "PROJECT_EMAIL:SEND_FAILED",
            e,
            details=(
                f"project_id={project.get('id')}\n"
                f"title={project.get('title')}\n"
                f"url={project.get('url')}\n"
                f"recipients={', '.join(Config.RECIPIENT_EMAILS)}\n"
                f"smtp={Config.SMTP_SERVER}:{Config.SMTP_PORT}"
            ),
            traceback_text=traceback.format_exc(),
            diagnostics={
                "project_id": project.get("id"),
                "project_title": project.get("title"),
                "project_url": project.get("url"),
                "operation": "project_email",
            },
        )
        return False

# ============================
# DRIVER INITIALIZATION
# ============================
def _find_binary(env_var, candidates):
    """Return the first executable from env var or candidate paths."""
    import shutil

    configured = os.getenv(env_var, "").strip()
    if configured:
        if os.path.exists(configured):
            return configured
        found = shutil.which(configured)
        if found:
            return found

    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


def initialize_driver():
    """Initialize Chrome WebDriver"""
    from selenium.webdriver.chrome.service import Service

    options = Options()

    if Config.HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    chrome_bin = _find_binary("CHROME_BIN", [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ])
    if chrome_bin:
        options.binary_location = chrome_bin

    chromedriver_path = _find_binary("CHROMEDRIVER_PATH", [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ])
    versions = get_browser_versions()
    try:
        if chromedriver_path:
            service = Service(chromedriver_path)
            driver = webdriver.Chrome(service=service, options=options)
        else:
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                service = Service(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service, options=options)
            except Exception:
                # Selenium 4.6+ built-in manager as last resort
                driver = webdriver.Chrome(options=options)

        driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            "userAgent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        return driver
    except Exception as e:
        ctx = "BROWSER_STARTUP:CHROMEDRIVER"
        msg = str(e).lower()
        if "chrome" in msg and "binary" in msg:
            ctx = "BROWSER_STARTUP:CHROMIUM"
        send_error_notification(
            ctx,
            e,
            details=(
                f"chrome_bin={chrome_bin or '(auto)'}\n"
                f"chromedriver_path={chromedriver_path or '(auto)'}\n"
                f"headless={Config.HEADLESS}\n"
                f"hostname={socket.gethostname()}\n"
                f"chromium={versions.get('chromium')}\n"
                f"chromedriver={versions.get('chromedriver')}"
            ),
            traceback_text=traceback.format_exc(),
            diagnostics={"operation": "initialize_driver"},
            force=True,
        )
        raise


DASHBOARD_URL = "https://app.gocatalant.com/c/_/u/0/dashboard/"
SEARCH_URL = "https://app.gocatalant.com/c/_/u/0/search/?form_name=SearchForm&enable_pagination=True&enable_facets=True&card_action_show_need=True&use_recommended=y&display_result_count=True"

def _navigate_to_search(driver):
    """Navigate to Search Projects page. Loads dashboard first so the AJAX session is active."""
    driver.get(DASHBOARD_URL)
    time.sleep(4)
    driver.get(SEARCH_URL)
    time.sleep(8)

def setup_session(driver):
    """Setup browser session with cookies or login.
    Returns dict: {success, classification, alert_sent, message}.
    Does not re-alert when perform_login already sent a classified login alert.
    """
    if load_cookies(driver):
        _navigate_to_search(driver)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".need-card-inline-name"))
            )
            print("Logged in via cookies -> Search Projects")
            return {"success": True, "classification": None, "alert_sent": False, "message": "cookies"}
        except Exception:
            pass
    result = perform_login(driver)
    if isinstance(result, dict):
        if not result.get("success") and not result.get("alert_sent"):
            send_error_notification(
                "SESSION_SETUP:FAILED",
                RuntimeError(result.get("message") or "Session setup failed"),
                details=f"classification={result.get('classification')}",
                diagnostics={**_safe_driver_info(driver), "operation": "setup_session"},
            )
            result["alert_sent"] = True
        return result
    return {"success": bool(result), "classification": None, "alert_sent": False, "message": ""}

# ============================
# MAIN MONITORING LOOP
# ============================
def main():
    """Main monitoring loop"""
    global _monitor_check_count, _monitor_state, last_scan_issue

    print_startup_banner()
    clean_old_evidence_files()
    _monitor_state = "starting"

    driver = None
    try:
        driver = initialize_driver()
    except Exception as e:
        print(f"❌ Browser startup failed: {redact_sensitive_text(e)}")
        _monitor_state = "fatal"
        return

    try:
        session = setup_session(driver)
        if not session.get("success"):
            print("❌ Failed to establish session")
            # Avoid duplicate alert when login already notified
            if not session.get("alert_sent") and not _last_login_alert.get("alert_sent"):
                send_error_notification(
                    "SESSION_SETUP:FAILED",
                    RuntimeError(session.get("message") or "Failed to establish session"),
                    details=f"classification={session.get('classification')}",
                    diagnostics={**_safe_driver_info(driver), "operation": "session_setup"},
                )
            # Auth failure retry with LOGIN_RETRY_INTERVAL (cooldown suppresses repeat emails)
            if session.get("classification"):
                print(f"⏳ Login retry in {Config.LOGIN_RETRY_INTERVAL}s...")
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(Config.LOGIN_RETRY_INTERVAL)
                try:
                    driver = initialize_driver()
                    session = setup_session(driver)
                except Exception as recovery_err:
                    send_error_notification(
                        "MONITORING_RECOVERY:FAILED",
                        recovery_err,
                        traceback_text=traceback.format_exc(),
                        diagnostics={"operation": "login_retry"},
                    )
                    return
                if not session.get("success"):
                    return
            else:
                return

        _monitor_state = "running"
        try:
            cold_start = db_is_cold_start()
            init_db()
            seen_ids = get_seen_ids()
        except Exception as db_err:
            send_error_notification(
                "DATABASE:INITIALIZATION_FAILED",
                db_err,
                traceback_text=traceback.format_exc(),
                diagnostics={"database": "office_monitor", "collection": "projects"},
                force=True,
            )
            raise

        print(f"📁 DB loaded — {len(seen_ids)} projects on record\n")

        # ── STARTUP RECONCILIATION ───────────────────────────────────────────
        SEED_MAX_AGE_MINUTES = 720  # 12 hours
        label = "First run" if cold_start else "Restart"
        print(f"⚙️  {label} — reconciling current page silently (no emails sent)...")
        try:
            seed_projects = scan_for_projects(driver)
            if seed_projects:
                recent, old = [], []
                for p in seed_projects:
                    age = parse_posted_minutes(p.get("time_posted", ""))
                    if age is None or age <= SEED_MAX_AGE_MINUTES:
                        recent.append(p)
                    else:
                        old.append(p)
                for i, p in enumerate(recent, 1):
                    if p.get("platform_category"):
                        continue
                    url = p.get("url")
                    if not url:
                        continue
                    print(f"  [{i}/{len(recent)}] Fetching platform category: {p.get('title', '')[:50]}...")
                    details = fetch_project_details(driver, url)
                    if details.get("platform_category"):
                        p["platform_category"] = details["platform_category"]
                        print(f"      → {p['platform_category']}")
                bulk_insert_projects(recent, emailed=False)
                seen_ids = get_seen_ids()
                for p in old:
                    if p.get("id"):
                        seen_ids.add(p["id"])
                print(f"✅ Reconciled — {len(recent)} recent (saved to DB), {len(old)} old (ignored). Only NEW posts will trigger emails.\n")
            else:
                print("⚠️  Could not reconcile on startup — will retry next cycle.\n")
                if last_scan_issue and not last_scan_issue.get("alert_sent"):
                    send_error_notification(
                        "DATABASE:COLD_START_RECONCILIATION_FAILED",
                        last_scan_issue.get("error") or RuntimeError("No seed projects"),
                        details="startup reconciliation found no projects",
                        diagnostics={**_safe_driver_info(driver), "operation": "cold_start"},
                    )
        except Exception as recon_err:
            print(f"⚠️ Reconciliation failed: {redact_sensitive_text(recon_err)}")
            send_error_notification(
                "DATABASE:COLD_START_RECONCILIATION_FAILED",
                recon_err,
                traceback_text=traceback.format_exc(),
                diagnostics={**_safe_driver_info(driver), "operation": "cold_start"},
            )
        # ─────────────────────────────────────────────────────────────────────

        check_count = 0
        while True:
            try:
                check_count += 1
                _monitor_check_count = check_count
                if check_count % 20 == 0:
                    clean_old_evidence_files()
                print(f"\n{'='*30}")
                print(f"🔄 Check #{check_count} - {datetime.now(PKT).strftime('%H:%M:%S')} PKT")
                print(f"{'='*30}")

                _navigate_to_search(driver)

                all_projects = scan_for_projects(driver)

                if not all_projects:
                    print("⚠️ No projects found")
                    # Scan already alerted if classified failure; do not double-alert
                    time.sleep(Config.CHECK_INTERVAL)
                    continue

                new_projects = filter_new_projects(all_projects, seen_ids)

                if new_projects:
                    print(f"🎯 Found {len(new_projects)} NEW project(s)!")
                    for project in new_projects:
                        print(f"  → {project['title'][:60]}...")
                        print(f"     Fetching full project details...")
                        details = fetch_project_details(driver, project['url'])
                        project.update(details)
                        emailed = send_notification(project)
                        insert_project(project, emailed=emailed)
                        seen_ids.add(project['id'])
                else:
                    print("⏳ No new projects")

                print(f"📊 Stats: {len(all_projects)} visible, {len(seen_ids)} in DB")
                print(f"\n⏳ Next check in {Config.CHECK_INTERVAL} seconds...")
                time.sleep(Config.CHECK_INTERVAL)

            except KeyboardInterrupt:
                raise
            except Exception as loop_err:
                print(f"⚠️ Check failed: {redact_sensitive_text(loop_err)} — retrying in {Config.CHECK_INTERVAL}s...")
                send_error_notification(
                    "MONITORING_CYCLE:FAILED",
                    loop_err,
                    details=(
                        f"check_number={check_count}\n"
                        f"monitor_state={_monitor_state}\n"
                        f"last_successful_scan={_last_successful_scan_at}"
                    ),
                    traceback_text=traceback.format_exc(),
                    diagnostics={**_safe_driver_info(driver), "operation": "monitoring_cycle"},
                )
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(Config.CHECK_INTERVAL)
                try:
                    driver = initialize_driver()
                    session = setup_session(driver)
                    if not session.get("success"):
                        print("Re-login failed -- will retry next cycle")
                        if not session.get("alert_sent"):
                            send_error_notification(
                                "BROWSER_RECOVERY:FAILED",
                                RuntimeError(session.get("message") or "Re-login failed"),
                                diagnostics={"operation": "browser_recovery"},
                            )
                except Exception as recovery_err:
                    print(f"⚠️ Recovery failed: {redact_sensitive_text(recovery_err)}")
                    send_error_notification(
                        "MONITORING_RECOVERY:FAILED",
                        recovery_err,
                        traceback_text=traceback.format_exc(),
                        diagnostics={"operation": "browser_recovery"},
                    )

    except KeyboardInterrupt:
        print("\n\n⏹️ Stopped by user")
        _monitor_state = "stopped"
    except Exception as e:
        print(f"\n❌ Error: {redact_sensitive_text(e)}")
        _monitor_state = "fatal"
        # Do not duplicate classified login alerts
        if not _last_login_alert.get("alert_sent"):
            send_error_notification(
                "MONITORING_LOOP:FATAL_ERROR",
                e,
                traceback_text=traceback.format_exc(),
                diagnostics={"operation": "main"},
                force=True,
            )
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        print("✅ Monitor stopped")

if __name__ == "__main__":
    if TEST_ERROR_EMAIL_MODE:
        raise SystemExit(run_test_error_email())
    main()
