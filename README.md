# Catalant Project Monitor

Clean, minimal, and robust Selenium scraper for monitoring Catalant projects with email notifications.

## Features

- ✅ Extracts only valid projects (requires both title and project ID)
- ✅ No placeholder or 'Unknown' entries
- ✅ Secure: All credentials stored in `.env` file
- ✅ Robust error handling with safe fallbacks
- ✅ Email notifications with full project details
- ✅ Session persistence with cookies
- ✅ Clean, minimal code with no redundancy

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Create a `.env` file in the project directory (already created for you):

```env
# Catalant Login Credentials
CATALANT_EMAIL=your_email@example.com
CATALANT_PASSWORD=your_password

# Email Settings
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SENDER_EMAIL=your_email@gmail.com
SENDER_PASSWORD=your_app_password
RECIPIENT_EMAILS=email1@example.com,email2@example.com

# Monitoring Settings
CHECK_INTERVAL=300
HEADLESS=False

# File Paths
COOKIES_FILE=catalant_cookies.json
PROJECTS_DB=seen_projects.json
```

**Note for Gmail users:** 
- Enable 2-Step Verification in your Google Account
- Generate an App Password (Google Account > Security > App Passwords)
- Use the App Password in `SENDER_PASSWORD`

### 3. Run the Monitor

```bash
python script_clean.py
```

## How It Works

1. **Session Management**: Logs in once and saves cookies for future runs
2. **Project Extraction**: Only extracts projects with valid title and ID
3. **Smart Filtering**: Skips non-project elements and duplicates
4. **Email Notifications**: Sends detailed HTML emails for new projects
5. **Continuous Monitoring**: Checks for new projects at configured intervals

## Script Structure

- `script_clean.py` - Main refactored script (clean and minimal)
- `script_test.py` - Original script (kept for reference)
- `.env` - Environment variables (credentials and config)
- `requirements.txt` - Python dependencies
- `.gitignore` - Protects sensitive files

## Security

- ✅ No hardcoded credentials in code
- ✅ `.env` file excluded from git
- ✅ All sensitive data loaded from environment variables

## Key Improvements

1. **No Placeholder Entries**: Returns `None` if title or ID is missing
2. **Strict Validation**: Only appends projects with valid data
3. **Clean Selectors**: Targets `.need-card-inline` elements specifically
4. **Safe Fallbacks**: Optional fields wrapped in try-except (no crashes)
5. **Environment Variables**: All secrets in `.env` via python-dotenv
6. **Minimal Logging**: Only essential information, no redundancy
7. **Clean Code**: Removed duplicate logic, simplified functions

## Deployment (Free & Automatic)

### Option 1: GitHub Actions ⭐ (Recommended)

**Pros:** Completely free, reliable, runs every 30 minutes automatically
**Cons:** Requires GitHub account

#### Quick Setup:

1. **Push to GitHub:**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/catalant-monitor.git
   git push -u origin main
   ```

2. **Add Secrets:**
   - Go to: Repository → Settings → Secrets and variables → Actions
   - Click "New repository secret" for each:
     - `CATALANT_EMAIL`
     - `CATALANT_PASSWORD`
     - `SMTP_SERVER` = `smtp.gmail.com`
     - `SMTP_PORT` = `587`
     - `SENDER_EMAIL`
     - `SENDER_PASSWORD`
     - `RECIPIENT_EMAILS`

3. **Enable Actions:**
   - Go to Actions tab → Enable workflows
   - Done! Monitors automatically every 30 minutes

4. **Test It:**
   - Actions → "Catalant Project Monitor" → Run workflow
   - Check logs to verify it's working

---

### Option 2: Railway.app

**Pros:** Runs 24/7, good for continuous monitoring
**Cons:** $5 free credit/month (~500 hours)

1. Sign up at https://railway.app
2. New Project → Deploy from GitHub
3. Add environment variables from `.env`
4. Set `HEADLESS=True`

---

### Option 3: PythonAnywhere

**Pros:** Simple setup, free tier
**Cons:** Only 1 scheduled task daily on free tier

1. Sign up at https://pythonanywhere.com
2. Upload files via Files tab
3. Install: `pip3.10 install --user -r requirements.txt`
4. Tasks tab → Schedule daily run

---

### Option 4: Render.com

**Pros:** Free background workers
**Cons:** Spins down after inactivity

1. Sign up at https://render.com
2. New Web Service → Connect GitHub
3. Add environment variables
4. Deploy

## Deployment (Free & Automatic)

### GitHub Actions (Recommended - Completely Free)

Deploy this monitor to run automatically every 30 minutes on GitHub's servers:

#### 1. Create GitHub Repository

```bash
# Initialize git (if not already done)
git init
git add .
git commit -m "Initial commit"

# Create repo on GitHub, then:
git remote add origin https://github.com/YOUR_USERNAME/catalant-monitor.git
git push -u origin main
```

#### 2. Add Secrets to GitHub

Go to your repository → Settings → Secrets and variables → Actions → New repository secret

Add these secrets (copy values from your `.env` file):

- `CATALANT_EMAIL` → Your Catalant email
- `CATALANT_PASSWORD` → Your Catalant password
- `SMTP_SERVER` → `smtp.gmail.com`
- `SMTP_PORT` → `587`
- `SENDER_EMAIL` → Your Gmail address
- `SENDER_PASSWORD` → Your Gmail App Password
- `RECIPIENT_EMAILS` → Comma-separated email list

#### 3. Enable GitHub Actions

- Go to Actions tab in your repository
- Click "I understand my workflows, go ahead and enable them"
- The workflow will run automatically every 30 minutes

#### 4. Manual Trigger (Optional)

- Go to Actions → "Catalant Project Monitor"
- Click "Run workflow" to test immediately

#### 5. Check Logs

- Go to Actions → Click on any workflow run
- View logs to see what projects were found

### How it Works

- **Runs every 30 minutes** automatically
- **Saves session data** between runs (cookies persist)
- **Tracks seen projects** to avoid duplicate emails
- **Sends emails** only for new projects
- **Completely free** (unlimited for public repos, 2000 min/month for private)

### Files for GitHub Actions

- `.github/workflows/monitor.yml` - Workflow configuration
- `script_clean_single.py` - Single-check version (no loop)
- `.gitignore` - Protects secrets from being committed

## Troubleshooting

**No projects found:**
- Check if you're logged in correctly
- Verify the page structure hasn't changed
- Run with `HEADLESS=False` to see browser

**Email not sending:**
- Verify SMTP credentials in `.env`
- For Gmail, ensure App Password is used
- Check firewall/antivirus settings

**Session expires:**
- Delete `catalant_cookies.json` to force fresh login
- Check Catalant account status

**GitHub Actions Issues:**
- Check Actions logs for error messages
- Verify all secrets are set correctly
- Ensure repository Actions are enabled
