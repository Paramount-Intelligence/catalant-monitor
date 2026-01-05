import time
import smtplib
import json
import os
import re
from datetime import datetime
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
    RECIPIENT_EMAILS = os.getenv("RECIPIENT_EMAILS", "").split(",")
    HEADLESS = os.getenv("HEADLESS", "True").lower() == "true"
    COOKIES_FILE = os.getenv("COOKIES_FILE", "catalant_cookies.json")
    PROJECTS_DB = os.getenv("PROJECTS_DB", "seen_projects.json")

# ============================
# SESSION MANAGEMENT
# ============================
def save_cookies(driver):
    """Save session cookies to file"""
    try:
        with open(Config.COOKIES_FILE, 'w') as f:
            json.dump(driver.get_cookies(), f)
        return True
    except Exception:
        return False

def load_cookies(driver):
    """Load cookies from file"""
    if not os.path.exists(Config.COOKIES_FILE):
        return False
    try:
        with open(Config.COOKIES_FILE, 'r') as f:
            cookies = json.load(f)
        driver.get("https://app.gocatalant.com")
        time.sleep(2)
        driver.delete_all_cookies()
        for cookie in cookies:
            if 'domain' in cookie and '.gocatalant.com' in cookie['domain']:
                driver.add_cookie(cookie)
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
        print("✅ Login successful")
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
            loc_text = card.find_element(By.CSS_SELECTOR, ".text-gray-25.font-weight-semibold").text
            location = loc_text.replace("Remote", "").strip()
        except:
            pass
        
        time_posted = "Unknown"
        try:
            time_elems = card.find_elements(By.XPATH, ".//div[contains(@class, 'small') and contains(@class, 'text-gray-20') and contains(@class, 'mt-1')]//span[contains(text(), 'Posted')]")
            if time_elems:
                time_posted = time_elems[0].text.replace("Posted", "").replace("ago", "").strip()
        except:
            pass
        
        status = "Posted"
        try:
            card.find_element(By.CSS_SELECTOR, ".badge-success")
            status = "New Project"
        except:
            pass
        
        return {
            "id": project_id,
            "title": title,
            "categories": categories,
            "description": description,
            "location": location,
            "time_posted": time_posted,
            "status": status,
            "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    except:
        return None

def scan_for_projects(driver):
    """Scan dashboard for project cards - returns only valid projects"""
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".need-card-inline-name"))
        )
        
        # Get all card blocks
        all_cards = driver.find_elements(By.CSS_SELECTOR, "div.card-block")
        
        # Filter to only those with project content
        project_cards = [c for c in all_cards if c.find_elements(By.CSS_SELECTOR, ".need-card-inline")]
        
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
# PROJECT DATABASE
# ============================
def load_seen_projects():
    """Load previously seen projects"""
    if not os.path.exists(Config.PROJECTS_DB):
        return []
    try:
        with open(Config.PROJECTS_DB, 'r') as f:
            return json.load(f)
    except:
        return []

def save_seen_projects(projects):
    """Save projects to database"""
    try:
        with open(Config.PROJECTS_DB, 'w') as f:
            json.dump(projects, f, indent=2)
    except Exception as e:
        print(f"⚠️ Failed to save projects: {e}")

def filter_new_projects(all_projects, seen_projects):
    """Filter out previously seen projects"""
    seen_ids = {p.get("id") for p in seen_projects if p.get("id")}
    return [p for p in all_projects if p.get("id") and p["id"] not in seen_ids]

# ============================
# EMAIL NOTIFICATIONS
# ============================
def create_email_html(project):
    """Create HTML email for a project"""
    categories_html = ""
    if project.get("categories"):
        cats = "<br>".join([f"• {cat}" for cat in project["categories"]])
        categories_html = f"<div style='margin: 10px 0;'><strong>📁 Categories:</strong><br>{cats}</div>"
    
    description = project.get("description", "No description available").replace("\n", "<br>")
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
            .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}
            .header {{ background-color: #4CAF50; color: white; padding: 15px; border-radius: 5px 5px 0 0; }}
            .content {{ padding: 20px; border: 1px solid #ddd; border-top: none; background-color: #fff; }}
            .project-title {{ color: #2c3e50; margin-top: 0; }}
            .badge {{ background-color: #e74c3c; color: white; padding: 5px 10px; border-radius: 3px; font-size: 12px; display: inline-block; margin-bottom: 10px; }}
            .info-section {{ background-color: #f8f9fa; padding: 15px; border-radius: 5px; margin: 15px 0; }}
            .description-section {{ background-color: #ffffff; padding: 15px; border-left: 4px solid #4CAF50; margin: 15px 0; }}
            .footer {{ margin-top: 20px; padding-top: 20px; border-top: 1px solid #eee; font-size: 12px; color: #777; }}
            .action-button {{ display: inline-block; background-color: #4CAF50; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; margin-top: 15px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2 style="margin: 0;">🚀 New Project on Catalant</h2>
            </div>
            <div class="content">
                <h3 class="project-title">{project.get('title', 'Untitled Project')}</h3>
                {"<span class='badge'>New Project</span>" if project.get('status') == 'New Project' else ''}
                
                <div class="info-section">
                    <p style="margin: 5px 0;"><strong>📍 Location:</strong> {project.get('location', 'Remote / Not specified')}</p>
                    <p style="margin: 5px 0;"><strong>⏰ Posted:</strong> {project.get('time_posted', 'Unknown')} ago</p>
                    <p style="margin: 5px 0;"><strong>🆔 Project ID:</strong> {project.get('id', 'N/A')}</p>
                    <p style="margin: 5px 0;"><strong>🕒 Detected:</strong> {project.get('detected_at', '')}</p>
                </div>
                
                {categories_html}
                
                <div class="description-section">
                    <h4 style="margin-top: 0;">📋 Project Description:</h4>
                    <p style="white-space: pre-wrap;">{description}</p>
                </div>
                
                <div style="text-align: center; margin-top: 20px;">
                    <a href="https://app.gocatalant.com/c/_/u/0/dashboard/" class="action-button">View on Catalant Dashboard</a>
                </div>
            </div>
            <div class="footer">
                <p>Automated notification from Catalant Project Monitor</p>
            </div>
        </div>
    </body>
    </html>
    """

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
def initialize_driver():
    """Initialize Chrome WebDriver"""
    options = Options()
    if Config.HEADLESS:
        options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    
    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd('Network.setUserAgentOverride', {
        "userAgent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    return driver

def setup_session(driver):
    """Setup browser session with cookies or login"""
    if load_cookies(driver):
        driver.get("https://app.gocatalant.com/c/_/u/0/dashboard/")
        time.sleep(5)
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".need-card-inline-name"))
            )
            print("✅ Logged in via cookies")
            return True
        except:
            pass
    
    return perform_login(driver)

# ============================
# SINGLE CHECK - NO LOOP
# ============================
def main():
    """Single check - runs once and exits (for GitHub Actions)"""
    print("=" * 50)
    print("🚀 Catalant Project Monitor - Single Check")
    print("=" * 50)
    
    driver = initialize_driver()
    
    try:
        if not setup_session(driver):
            print("❌ Failed to establish session")
            return
        
        seen_projects = load_seen_projects()
        print(f"📁 Loaded {len(seen_projects)} seen projects\n")
        
        print(f"🔄 Checking at {datetime.now().strftime('%H:%M:%S')}")
        
        all_projects = scan_for_projects(driver)
        
        if not all_projects:
            print("⚠️ No projects found")
            return
        
        new_projects = filter_new_projects(all_projects, seen_projects)
        
        if new_projects:
            print(f"🎯 Found {len(new_projects)} NEW project(s)!")
            for project in new_projects:
                print(f"  → {project['title'][:60]}...")
                send_notification(project)
                seen_projects.append(project)
            save_seen_projects(seen_projects)
        else:
            print("⏳ No new projects")
        
        print(f"📊 Stats: {len(all_projects)} total, {len(seen_projects)} tracked")
        print("✅ Check complete")
            
    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        driver.quit()
        print("✅ Browser closed")

if __name__ == "__main__":
    main()
