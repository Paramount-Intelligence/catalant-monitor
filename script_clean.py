import sys
import time
import smtplib
import json
import os
import re

# Ensure UTF-8 output on all platforms (fixes Windows emoji crash)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
from pymongo import MongoClient, UpdateOne
from datetime import datetime, timezone, timedelta

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5)
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ============================
# CONFIGURATION
# ============================
class Config:
    """Load configuration from environment variables"""
    CATALANT_EMAIL = os.getenv("CATALANT_EMAIL")
    CATALANT_PASSWORD = os.getenv("CATALANT_PASSWORD")
    SMTP_SERVER = os.getenv("SMTP_SERVER")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SENDER_EMAIL = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [e.strip() for e in os.getenv("RECIPIENT_EMAILS", "ahmedghazi459@gmail.com,ahsanuddin3522@gmail.com").split(",") if e.strip()]
    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
    MAX_AGE_MINUTES = int(os.getenv("MAX_AGE_MINUTES", 60))
    HEADLESS = os.getenv("HEADLESS", "False").lower() == "true"
    COOKIES_FILE = os.getenv("COOKIES_FILE", "catalant_cookies.json")
    MONGO_URI    = os.getenv("MONGO_URI", "mongodb://localhost:27017/")

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
    # MongoDB (primary)
    try:
        from datetime import timezone
        _get_session_collection().update_one(
            {"_id": "catalant_cookies"},
            {"$set": {"cookies": cookies, "saved_at": datetime.now(timezone.utc)}},
            upsert=True
        )
    except Exception as e:
        print(f"  ⚠️ Could not save cookies to MongoDB: {e}")
    # Local file fallback
    try:
        with open(Config.COOKIES_FILE, 'w') as f:
            json.dump(cookies, f)
    except Exception:
        pass
    return True

def load_cookies(driver):
    """Load cookies from MongoDB first, fall back to local file."""
    cookies = None
    # Try MongoDB first
    try:
        doc = _get_session_collection().find_one({"_id": "catalant_cookies"})
        if doc and doc.get("cookies"):
            cookies = doc["cookies"]
            print("  Loaded cookies from MongoDB")
    except Exception as e:
        print(f"  ⚠️ Could not load cookies from MongoDB: {e}")
    # Fall back to local file
    if not cookies:
        if not os.path.exists(Config.COOKIES_FILE):
            return False
        try:
            with open(Config.COOKIES_FILE, 'r') as f:
                cookies = json.load(f)
            print("  Loaded cookies from local file")
        except Exception:
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
    except Exception:
        return False

def perform_login(driver):
    """Perform login to Catalant"""
    try:
        driver.get("https://app.gocatalant.com/c/_/u/0/dashboard/")
        time.sleep(3)
        
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.NAME, "email"))
        )
        
        driver.find_element(By.NAME, "email").send_keys(Config.CATALANT_EMAIL)
        driver.find_element(By.NAME, "password").send_keys(Config.CATALANT_PASSWORD)
        driver.find_element(By.XPATH, "//button[contains(text(), 'Login') or @type='submit']").click()
        
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".need-card-inline-name"))
        )
        
        save_cookies(driver)
        # Navigate to Search Projects page after login
        _navigate_to_search(driver)
        print("Login successful -> Search Projects")
        return True
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return False

# ============================
# PROJECT EXTRACTION
# ============================
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
        
        # Optional fields with safe fallbacks
        categories = []
        try:
            cat_text = card.find_element(By.CSS_SELECTOR, ".need-card-inline-pools .small.text-muted").text.strip()
            categories = [c.strip() for c in cat_text.split("|") if c.strip()]
        except:
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
            "time_posted": time_posted,
            "status": status,
            "url": url,
            "detected_at": datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")
        }
    except:
        return None

def scan_for_projects(driver):
    """Scan Search Projects page for project cards - returns only valid projects"""
    try:
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".need-card-inline-name"))
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
        return projects
    except TimeoutException:
        print("⏳ Timeout waiting for projects")
        return []
    except Exception as e:
        print(f"❌ Error scanning: {e}")
        return []

# ============================
# PROJECT DATABASE (MongoDB)
# ============================
_projects_client = None

def _get_collection():
    """Return the MongoDB projects collection, reusing the client across calls."""
    global _projects_client
    if _projects_client is None:
        _projects_client = MongoClient(Config.MONGO_URI)
    return _projects_client["office_monitor"]["projects"]

def init_db():
    """Ensure a unique index on 'project_id' exists."""
    try:
        _get_collection().create_index("project_id", unique=True, name="idx_project_id_unique")
    except Exception:
        pass  # Index already exists — safe to ignore

def db_is_cold_start():
    """True if the collection has no documents (first ever run)."""
    return _get_collection().find_one({}, {"_id": 1}) is None

def get_seen_ids():
    """Return set of all project IDs already in DB."""
    try:
        docs = _get_collection().find({}, {"project_id": 1, "_id": 0})
        return {d["project_id"] for d in docs if d.get("project_id")}
    except Exception:
        return set()

def insert_project(project, emailed=True):
    """Upsert one project record. Silently skips if ID already exists."""
    try:
        doc = {
            "project_id":       project.get("id"),
            "title":            project.get("title"),
            "description":      project.get("description"),
            "location":         project.get("location"),
            "budget":           project.get("budget"),
            "duration":         project.get("duration"),
            "start_date":       project.get("start_date"),
            "project_length":   project.get("project_length"),
            "location_pref":    project.get("location_pref"),
            "level_of_support": project.get("level_of_support"),
            "industry":         project.get("industry"),
            "contracting":      project.get("contracting"),
            "time_posted":      project.get("time_posted"),
            "status":           project.get("status"),
            "url":              project.get("url"),
            "detected_at":      project.get("detected_at"),
            "platform":         "catalant",
            "emailed":          bool(emailed),
        }
        _get_collection().update_one(
            {"project_id": doc["project_id"]},
            {"$setOnInsert": doc},
            upsert=True,
        )
    except Exception as e:
        print(f"⚠️ DB insert failed: {e}")

def bulk_insert_projects(projects, emailed=False):
    """Upsert many projects at once (used for cold-start seeding)."""
    try:
        ops = []
        for p in projects:
            if not p.get("id"):
                continue
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
            ops.append(UpdateOne({"project_id": doc["project_id"]}, {"$setOnInsert": doc}, upsert=True))
        if ops:
            result = _get_collection().bulk_write(ops, ordered=False)
            print(f"  DB: inserted {result.upserted_count} records (emailed={'yes' if emailed else 'no'})")
    except Exception as e:
        print(f"⚠️ DB bulk insert failed: {e}")

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

    except Exception as e:
        print(f"  ⚠️ Detail fetch failed: {e}")
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
        print(f"❌ Email failed: {e}")
        return False

# ============================
# DRIVER INITIALIZATION
# ============================
def _find_binary(env_var, candidates):
    """Return a resolved executable path from env var, candidate paths, or PATH."""
    import shutil

    configured = os.getenv(env_var, "").strip()
    search_items = []
    if configured:
        search_items.append(configured)
    search_items.extend(candidates)

    for item in search_items:
        if not item:
            continue
        if os.path.exists(item):
            return item
        found = shutil.which(item)
        if found:
            return found
        basename = os.path.basename(item)
        if basename and basename != item:
            found = shutil.which(basename)
            if found:
                return found

    if configured:
        print(f"  {env_var} was set but no executable was found: {configured}")
    return ""


def _get_binary_version(binary_path):
    """Return a short version string for startup diagnostics."""
    import subprocess

    if not binary_path:
        return "not selected"
    try:
        result = subprocess.run(
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = (result.stdout or result.stderr).strip()
        return version or f"version command returned no output (exit {result.returncode})"
    except Exception as exc:
        return f"unavailable ({exc})"


def initialize_driver():
    """Initialize Chrome WebDriver"""
    options = Options()
    chrome_args = [
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-setuid-sandbox",
        "--window-size=1920,1080",
        "--remote-debugging-port=9222",
        "--user-data-dir=/tmp/chrome-user-data",
        "--disable-blink-features=AutomationControlled",
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    ]
    for arg in chrome_args:
        options.add_argument(arg)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    os.makedirs("/tmp/chrome-user-data", exist_ok=True)

    chrome_bin = _find_binary("CHROME_BIN", [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
    ])
    if chrome_bin:
        options.binary_location = chrome_bin

    from selenium.webdriver.chrome.service import Service

    # On Linux containers: use the apt-installed chromedriver (always version-matched)
    # On Windows (local dev): fall through to Service() — Selenium's SeleniumManager
    #   will auto-download the correct chromedriver for the installed Chrome.
    chromedriver_path = _find_binary("CHROMEDRIVER_PATH", [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "chromedriver",
    ])
    if chromedriver_path:
        service = Service(chromedriver_path)
    else:
        # Let Selenium's built-in SeleniumManager resolve the right chromedriver
        service = Service()

    print("  Selenium Chrome startup:")
    print(f"    selected Chrome binary path: {chrome_bin or 'Selenium default'}")
    print(f"    Chromium version: {_get_binary_version(chrome_bin)}")
    print(f"    selected Chromedriver path: {chromedriver_path or 'SeleniumManager auto'}")
    print(f"    Chromedriver version: {_get_binary_version(chromedriver_path)}")

    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd('Network.setUserAgentOverride', {
        "userAgent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    return driver

DASHBOARD_URL = "https://app.gocatalant.com/c/_/u/0/dashboard/"
SEARCH_URL = "https://app.gocatalant.com/c/_/u/0/search/?form_name=SearchForm&enable_pagination=True&enable_facets=True&card_action_show_need=True&use_recommended=y&display_result_count=True"

def _navigate_to_search(driver):
    """Navigate to Search Projects page. Loads dashboard first so the AJAX session is active."""
    driver.get(DASHBOARD_URL)
    time.sleep(4)
    driver.get(SEARCH_URL)
    time.sleep(8)

def setup_session(driver):
    """Setup browser session with cookies or login"""
    if load_cookies(driver):
        _navigate_to_search(driver)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".need-card-inline-name"))
            )
            print("Logged in via cookies -> Search Projects")
            return True
        except:
            pass
    return perform_login(driver)

# ============================
# MAIN MONITORING LOOP
# ============================
def main():
    """Main monitoring loop"""
    print("=" * 50)
    print("🚀 Catalant Project Monitor")
    print("=" * 50)
    
    driver = initialize_driver()
    
    try:
        if not setup_session(driver):
            print("❌ Failed to establish session")
            return
        
        cold_start = db_is_cold_start()
        init_db()
        seen_ids = get_seen_ids()
        print(f"📁 DB loaded — {len(seen_ids)} projects on record\n")

        # ── STARTUP RECONCILIATION ───────────────────────────────────────────
        # Jobs ≤12h old  → insert to DB silently (no email)
        # Jobs  >12h old → add to seen_ids in-memory only (no DB insert, no email)
        # This ensures NOTHING currently on the page ever triggers an email.
        # Only jobs that appear AFTER this startup scan will be emailed.
        SEED_MAX_AGE_MINUTES = 720  # 12 hours
        label = "First run" if cold_start else "Restart"
        print(f"⚙️  {label} — reconciling current page silently (no emails sent)...")
        seed_projects = scan_for_projects(driver)
        if seed_projects:
            recent, old = [], []
            for p in seed_projects:
                age = parse_posted_minutes(p.get("time_posted", ""))
                if age is None or age <= SEED_MAX_AGE_MINUTES:
                    recent.append(p)
                else:
                    old.append(p)
            bulk_insert_projects(recent, emailed=False)
            seen_ids = get_seen_ids()
            # Also mark old jobs as seen in-memory so they never trigger an email
            for p in old:
                if p.get("id"):
                    seen_ids.add(p["id"])
            print(f"✅ Reconciled — {len(recent)} recent (saved to DB), {len(old)} old (ignored). Only NEW posts will trigger emails.\n")
        else:
            print("⚠️  Could not reconcile on startup — will retry next cycle.\n")
        # ─────────────────────────────────────────────────────────────────────

        check_count = 0
        while True:
            try:
                check_count += 1
                print(f"\n{'='*30}")
                print(f"🔄 Check #{check_count} - {datetime.now(PKT).strftime('%H:%M:%S')} PKT")
                print(f"{'='*30}")

                _navigate_to_search(driver)

                all_projects = scan_for_projects(driver)

                if not all_projects:
                    print("⚠️ No projects found")
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
                print(f"⚠️ Check failed: {loop_err} — retrying in {Config.CHECK_INTERVAL}s...")
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(Config.CHECK_INTERVAL)
                driver = initialize_driver()
                if not setup_session(driver):
                    print("Re-login failed -- will retry next cycle")
            
    except KeyboardInterrupt:
        print("\n\n⏹️ Stopped by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        driver.quit()
        print("✅ Monitor stopped")

if __name__ == "__main__":
    main()
