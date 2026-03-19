import os
import json
import re
import time
from flask import Flask, render_template, request, jsonify
import gspread
from google.oauth2.service_account import Credentials
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# ------------------ ENV & GLOBALS ------------------
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")  # e.g., whatsapp:+1415xxxxxxx
TWILIO_CONTENT_SID = os.getenv("TWILIO_CONTENT_SID_CONCERT")  # HX...

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "ConcertBookings")
GOOGLE_SHEET_KEY = os.getenv("GOOGLE_SHEET_KEY")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "/etc/secrets/service_account.json")
CLEAR_TOKEN = os.getenv("CLEAR_TOKEN")

TZ = pytz.timezone(os.getenv("APP_TIMEZONE", "Asia/Kolkata"))
BROADCAST_ENABLE = os.getenv("BROADCAST_ENABLE", "true").lower() == "true"
BROADCAST_EVENT_TEXT = os.getenv("BROADCAST_EVENT_TEXT", "This Week")
NAME_FALLBACK = os.getenv("BROADCAST_NAME_FALLBACK", "Friend")
THROTTLE_SECONDS = float(os.getenv("THROTTLE_PER_MESSAGE_SECONDS", "0.15"))
MAX_RECIPIENTS_PER_RUN = int(os.getenv("MAX_RECIPIENTS_PER_RUN", "2000"))
START_SCHEDULER = os.getenv("START_SCHEDULER", "true").lower() == "true"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

app = Flask(__name__)

# Twilio client
twilio_client = None
if TWILIO_SID and TWILIO_AUTH:
    try:
        twilio_client = Client(TWILIO_SID, TWILIO_AUTH)
    except Exception as e:
        print(f"[WARN] Twilio client init failed: {e}")

# ---------- Google Sheets ----------

def build_creds():
    return Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)


def get_sheet():
    creds = build_creds()
    client = gspread.authorize(creds)
    sh = client.open_by_key(GOOGLE_SHEET_KEY) if GOOGLE_SHEET_KEY else client.open(GOOGLE_SHEET_NAME)
    ws = sh.sheet1
    # Ensure headers exist: Name, Mobile
    values = ws.get_all_values()
    if not values:
        ws.append_row(["Name", "Mobile"])  # minimal columns
    return ws

# ---------- Helpers ----------

def extract_digits(s: str) -> str:
    return "".join(re.findall(r"\d+", s or ""))


def format_whatsapp_to(digits: str) -> str:
    if digits.startswith("91") and len(digits) == 12:
        return f"whatsapp:+{digits}"
    elif len(digits) == 10:
        return f"whatsapp:+91{digits}"
    return f"whatsapp:+{digits}"


def get_recipients_from_sheet():
    ws = get_sheet()
    values = ws.get_all_values()
    if not values or len(values) <= 1:
        return []
    # Expect columns: 0 Name, 1 Mobile
    pairs = {}
    for row in values[1:]:
        name = (row[0].strip() if len(row) > 0 else "")
        mobile = (row[1].strip() if len(row) > 1 else "")
        digits = extract_digits(mobile)
        if not digits:
            continue
        pairs[digits] = name or ""
    recipients = [(n, m) for m, n in pairs.items()]
    if len(recipients) > MAX_RECIPIENTS_PER_RUN:
        recipients = recipients[:MAX_RECIPIENTS_PER_RUN]
    return recipients


def send_template_message(name: str, mobile_digits: str):
    if not (TWILIO_SID and TWILIO_AUTH and TWILIO_WHATSAPP_FROM and TWILIO_CONTENT_SID and twilio_client):
        print("[ERROR] Missing Twilio configuration")
        return None
    safe_name = name.strip() if name and name.strip() else NAME_FALLBACK
    to_wa = format_whatsapp_to(mobile_digits)
    payload = {
        "from_": TWILIO_WHATSAPP_FROM,
        "to": to_wa,
        "content_sid": TWILIO_CONTENT_SID,
        # Adjust to your approved template variable count/order
        "content_variables": json.dumps({
            "1": safe_name,
            "2": BROADCAST_EVENT_TEXT,
        }),
    }
    try:
        msg = twilio_client.messages.create(**payload)
        print(f"[INFO] Sent to {to_wa}: SID={msg.sid}")
        return msg.sid
    except Exception as e:
        print(f"[ERROR] Send failed to {to_wa}: {e}")
        return None


def do_broadcast(reason: str = "scheduled"):
    if not BROADCAST_ENABLE:
        print("[INFO] Broadcast disabled")
        return {"ok": True, "message": "disabled"}
    recipients = get_recipients_from_sheet()
    print(f"[INFO] Broadcast run ({reason}) recipients={len(recipients)}")
    sent = 0
    failed = 0
    for name, digits in recipients:
        sid = send_template_message(name, digits)
        sent += 1 if sid else 0
        failed += 0 if sid else 1
        time.sleep(THROTTLE_SECONDS)
    summary = {"ok": True, "sent": sent, "failed": failed, "total": len(recipients)}
    print(f"[INFO] Summary: {summary}")
    return summary

# ---------- Routes ----------
@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/broadcast/run', methods=['POST'])
def broadcast_run():
    if CLEAR_TOKEN and request.headers.get('X-CLEAR-TOKEN') != CLEAR_TOKEN:
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    try:
        res = do_broadcast(reason='manual')
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@app.route('/clear-sheet', methods=['POST'])
def clear_sheet():
    if CLEAR_TOKEN and request.headers.get('X-CLEAR-TOKEN') != CLEAR_TOKEN:
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    try:
        ws = get_sheet()
        ws.batch_clear(['A2:ZZZ'])
        return jsonify({"ok": True, "message": "Sheet cleared"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@app.route('/health', methods=['GET','HEAD'])
def health():
    return jsonify({"status": "ok"}), 200

# ---------- Scheduler ----------
scheduler = BackgroundScheduler(timezone=TZ)

def start_scheduler():
    try:
        # Wednesday 09:00 IST
        scheduler.add_job(lambda: do_broadcast('cron_wed_09'), CronTrigger(day_of_week='wed', hour=9, minute=0, timezone=TZ), id='wed_09', replace_existing=True)
        # Saturday 20:00 IST
        scheduler.add_job(lambda: do_broadcast('cron_sat_20'), CronTrigger(day_of_week='sat', hour=20, minute=0, timezone=TZ), id='sat_20', replace_existing=True)
        scheduler.start()
        print('[INFO] APScheduler started (Wed 09:00, Sat 20:00 Asia/Kolkata)')
    except Exception as e:
        print(f'[ERROR] Scheduler start failed: {e}')

if START_SCHEDULER:
    start_scheduler()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
