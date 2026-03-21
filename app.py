import os
import json
import re
import time
from datetime import datetime, timedelta
from typing import Optional

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
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")

# Utility Content SIDs (separate templates)
TWILIO_CONTENT_SID_SUNDAY = os.getenv("TWILIO_CONTENT_SID_SUNDAY")
TWILIO_CONTENT_SID_WEDNESDAY = os.getenv("TWILIO_CONTENT_SID_WEDNESDAY")

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "ConcertBookings")
GOOGLE_SHEET_KEY = os.getenv("GOOGLE_SHEET_KEY")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "/etc/secrets/service_account.json")
CLEAR_TOKEN = os.getenv("CLEAR_TOKEN")

TZ = pytz.timezone(os.getenv("APP_TIMEZONE", "Asia/Kolkata"))
BROADCAST_ENABLE = os.getenv("BROADCAST_ENABLE", "true").lower() == "true"
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
    if not ws.get_all_values():
        ws.append_row(["Timestamp", "User Code", "Name", "Mobile"])
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

def parse_timestamp(ts_str: str):
    if not ts_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(ts_str.strip(), fmt)
        except Exception:
            pass
    return None

def get_recipients_from_sheet():
    ws = get_sheet()
    values = ws.get_all_values()
    if not values or len(values) <= 1:
        return []
    by_mobile = {}
    for row in values[1:]:
        name = (row[2].strip() if len(row) > 2 else "")
        mobile = (row[3].strip() if len(row) > 3 else "")
        digits = extract_digits(mobile)
        if not digits:
            continue
        by_mobile[digits] = name or ""
    recipients = [(n, m) for m, n in by_mobile.items()]
    return recipients[:MAX_RECIPIENTS_PER_RUN]

def compute_upcoming_sunday_str(tz) -> str:
    now_local = pytz.utc.localize(datetime.utcnow()).astimezone(tz)
    dow = now_local.weekday()
    days_ahead = (6 - dow) % 7
    target = (now_local + timedelta(days=days_ahead)).date()
    return target.strftime("%d-%m-%Y")

def compute_upcoming_wednesday_str(tz) -> str:
    now_local = pytz.utc.localize(datetime.utcnow()).astimezone(tz)
    dow = now_local.weekday()
    days_ahead = (2 - dow) % 7
    target = (now_local + timedelta(days=days_ahead)).date()
    return target.strftime("%d-%m-%Y")

def pick_content_sid(kind: str) -> Optional[str]:
    return TWILIO_CONTENT_SID_WEDNESDAY if kind == "wednesday" else TWILIO_CONTENT_SID_SUNDAY

# ---------- Sending ----------
def send_template_message(name: str, mobile_digits: str, kind: str, *, date_str: str):
    if not twilio_client:
        print("[ERROR] Twilio not configured")
        return None

    content_sid = pick_content_sid(kind)
    if not content_sid:
        print(f"[ERROR] Missing Content SID for {kind}")
        return None

    to_wa = format_whatsapp_to(mobile_digits)
    safe_name = name.strip() if name.strip() else NAME_FALLBACK

    # Only 2 variables now
    variables = {"1": safe_name, "2": date_str}

    try:
        msg = twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_wa,
            content_sid=content_sid,
            content_variables=json.dumps(variables)
        )
        print(f"[INFO] Sent ({kind}) to {to_wa} SID={msg.sid}")
        return msg.sid
    except Exception as e:
        print(f"[ERROR] Failed to send to {to_wa}: {e}")
        return None

# ---------- Broadcast ----------
def resolve_broadcast_kind(now_dt):
    dow = now_dt.weekday()
    if dow == 2:
        return "wednesday"
    if dow == 5:
        return "sunday"
    return "sunday"

def do_broadcast(reason="scheduled", override_kind=None):
    if not BROADCAST_ENABLE:
        return {"ok": True, "message": "disabled"}

    now_local = pytz.utc.localize(datetime.utcnow()).astimezone(TZ)
    kind = override_kind or resolve_broadcast_kind(now_local)

    recipients = get_recipients_from_sheet()

    date_str = compute_upcoming_wednesday_str(TZ) if kind == "wednesday" else compute_upcoming_sunday_str(TZ)

    sent = failed = 0
    for (name, digits) in recipients:
        sid = send_template_message(name, digits, kind, date_str=date_str)
        sent += 1 if sid else 0
        failed += 0 if sid else 1
        time.sleep(THROTTLE_SECONDS)

    return {"ok": True, "kind": kind, "date": date_str, "sent": sent, "failed": failed, "total": len(recipients)}

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/broadcast/run", methods=["POST"])
def broadcast_run():
    if request.headers.get("X-CLEAR-TOKEN") != CLEAR_TOKEN:
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    kind = request.args.get("kind")
    return jsonify(do_broadcast("manual", kind)), 200

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

# ---------- Scheduler ----------
scheduler = BackgroundScheduler(timezone=TZ)

def start_scheduler():
    scheduler.add_job(lambda: do_broadcast("auto"), CronTrigger(day_of_week="wed", hour=9, minute=0, timezone=TZ))
    scheduler.add_job(lambda: do_broadcast("auto"), CronTrigger(day_of_week="sat", hour=20, minute=0, timezone=TZ))
    scheduler.start()

if START_SCHEDULER:
    start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
