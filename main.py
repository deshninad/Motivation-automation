import os
import time
import json
import gc
import stat
import shutil
import sqlite3
import smtplib
import schedule
import easyocr
import instaloader
import gspread
import traceback
from pathlib import Path
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

# --- CONFIGURATION ---
load_dotenv()
INSTA_USER     = os.environ.get("INSTA_USER_PROD")
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER_PROD")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD_PROD")
SHEET_ID       = os.environ.get("SHEET_ID_PROD")
GCP_JSON_STR   = os.environ.get("GCP_JSON_CREDENTIALS_PROD")
DB_NAME        = "motivation.db"

# Initialize Tools
L = instaloader.Instaloader(
    download_videos=False, save_metadata=False, compress_json=False,
    download_geotags=False, download_comments=False
)
# Load OCR model once at start
reader = easyocr.Reader(['en'])

# --- CORE UTILITIES ---

def remove_readonly(func, path, excinfo):
    """Force delete read-only files (Common Windows issue)."""
    os.chmod(path, stat.S_IWRITE)
    func(path)

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS sent_posts (post_hash TEXT PRIMARY KEY)')
        conn.execute('CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY)')
    print("Database Initialized.")

def sync_subscribers():
    """Syncs emails from Google Sheets to local SQLite DB."""
    print("Syncing subscribers...")
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    
    try:
        if os.path.exists("service_account.json"):
            creds = ServiceAccountCredentials.from_json_keyfile_name("service_account.json", scope)
        elif GCP_JSON_STR:
            info = json.loads(GCP_JSON_STR)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
        else:
            raise ValueError("No Google Credentials found.")

        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1 
        records = sheet.get_all_records()
        
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            for row in records:
                email = str(row.get("Email", "")).strip().lower()
                if "@" in email:
                    conn.execute("INSERT OR IGNORE INTO users VALUES (?)", (email,))
        print("Sync Successful.")
    except Exception:
        traceback.print_exc()

# --- CONTENT LOGIC ---

def get_latest_design(username):
    """Scrapes Insta, runs OCR, returns image path and text."""
    target_dir = Path.cwd() / username
    if target_dir.exists():
        shutil.rmtree(target_dir, onerror=remove_readonly)
    target_dir.mkdir(exist_ok=True)

    try:
        profile = instaloader.Profile.from_username(L.context, username)
        for post in profile.get_posts():
            if not post.is_video:
                print(f"Checking post: {post.shortcode}")
                L.download_post(post, target=username)
                
                img_path = next(target_dir.glob("*.jpg"), None)
                if not img_path: continue
                
                text = " ".join(reader.readtext(str(img_path), detail=0)).lower()
                
                # Production filter
                blacklist = ['shop', 'link', 'bio', 'promo', 'limited', 'sale']
                if any(word in text for word in blacklist):
                    continue 
                
                return img_path, text
    except Exception as e:
        print(f"Scraper Error: {e}")
    return None, None

def send_email(recipients, img_path, text):
    """Constructs and sends the HTML email."""
    msg = MIMEMultipart()
    msg['Subject'] = "Your Daily Reminder"
    msg['From'] = f"Stoic Bot <{EMAIL_SENDER}>"
    msg['To'] = ", ".join(recipients)
    
    html = f"""
    <html>
      <body style="text-align: center; font-family: sans-serif;">
        <h2 style="color: #333;">{text.title()}</h2>
        <img src="cid:motivational_img" style="max-width: 100%; border-radius: 12px;">
      </body>
    </html>
    """
    msg.attach(MIMEText(html, 'html'))
    
    with open(img_path, 'rb') as f:
        img = MIMEImage(f.read())
        img.add_header('Content-ID', '<motivational_img>')
        msg.attach(img)

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        print(f"Emails sent to {len(recipients)} subscribers.")
    except Exception as e:
        print(f"SMTP Error: {e}")

# --- THE AUTOMATED JOB ---

def run_automation():
    print(f"\n[JOB TRIGGERED] {time.strftime('%Y-%m-%d %H:%M:%S')}")
    sync_subscribers()
    
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        cursor = conn.cursor()
        subs = [r[0] for r in cursor.execute("SELECT email FROM users").fetchall()]
        
        if not subs:
            print("No subscribers found. Aborting.")
            return

        img_path, text = get_latest_design(INSTA_USER)
        if img_path and text:
            post_hash = str(hash(text))
            already_sent = cursor.execute("SELECT 1 FROM sent_posts WHERE post_hash=?", (post_hash,)).fetchone()
            
            if not already_sent:
                send_email(subs, img_path, text)
                cursor.execute("INSERT INTO sent_posts VALUES (?)", (post_hash,))
                conn.commit()
            else:
                print("Content already sent today. Skipping.")

    # Production Cleanup
    gc.collect() 
    time.sleep(2) 
    if os.path.exists(INSTA_USER):
        shutil.rmtree(INSTA_USER, onerror=remove_readonly)

# --- EXECUTION ---

if __name__ == "__main__":
    init_db()
    
    # Schedule production times
    schedule.every().day.at("09:00").do(run_automation)
    schedule.every().day.at("20:00").do(run_automation)

    print("--- Production Automation Live ---")
    
    # Run once at startup to confirm everything works
    run_automation()

    while True:
        schedule.run_pending()
        time.sleep(60)