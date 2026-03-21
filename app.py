import os
import json
import re
import time
from datetime import datetime, timedelta
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

# Computed-preference thresholds
PREF_WINDOW_WEEKLY_DAYS = int(os.getenv("PREF_WINDOW_WEEKLY_DAYS", "35"))
PREF_THRESHOLD_WEEKLY = int(os.getenv("PREF_THRESHOLD_WEEKLY", "3"))
PREF_WINDOW_BIWEEKLY_DAYS = int(os.getenv("PREF_WINDOW_BIWEEKLY_DAYS", "60"))
PREF_THRESHOLD_BIWEEKLY = int(os.getenv("PREF_THRESHOLD_BIWEEKLY", "1"))
PREFERENCE_DEFAULT = os.getenv("BROADCAST_PREFERENCE_DEFAULT", "Weekly")

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
    # Ensure headers per screenshot
    values = ws.get_all_values()
    if not values:
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
    """Return list of (name, digits). Reads C(Name), D(Mobile). Dedupe by mobile (last wins)."""
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
    if len(recipients) > MAX_RECIPIENTS_PER_RUN:
        recipients = recipients[:MAX_RECIPIENTS_PER_RUN]
    return recipients


def load_contact_events():
    """Builds map: digits -> [timestamps...] using A (Timestamp) and D (Mobile)."""
    ws = get_sheet()
    values = ws.get_all_values()
    events = {}
    if not values or len(values) <= 1:
        return events
    for row in values[1:]:
        ts = parse_timestamp(row[0] if len(row) > 0 else "")
        mobile = (row[3].strip() if len(row) > 3 else "")
        digits = extract_digits(mobile)
        if ts and digits:
            events.setdefault(digits, []).append(ts)
    for k in events:
        events[k].sort(reverse=True)
    return events


def compute_preference_from_history(digits: str, events_by_digits: dict, now_dt=None) -> str:
    now_dt = now_dt or datetime.utcnow()
    evts = events_by_digits.get(digits, [])

    def count_in_last(days: int) -> int:
        cutoff = now_dt - timedelta(days=days)
        return sum(1 for t in evts if t >= cutoff)

    if count_in_last(PREF_WINDOW_WEEKLY_DAYS) >= PREF_THRESHOLD_WEEKLY:
        return "Weekly"
    if count_in_last(PREF_WINDOW_BIWEEKLY_DAYS) >= PREF_THRESHOLD_BIWEEKLY:
        return "Biweekly"
    return "Monthly"


def compute_upcoming_sunday_str(tz) -> str:
    now_local = pytz.utc.localize(datetime.utcnow()).astimezone(tz)
    dow = now_local.weekday()  # Mon=0 ... Sun=6
    days_ahead = (6 - dow) % 7
    target = (now_local + timedelta(days=days_ahead)).date()
    return target.strftime("%d-%m-%Y")


def compute_upcoming_wednesday_str(tz) -> str:
    now_local = pytz.utc.localize(datetime.utcnow()).astimezone(tz)
    dow = now_local.weekday()  # Wed=2
    days_ahead = (2 - dow) % 7
    target = (now_local + timedelta(days=days_ahead)).date()
    return target.strftime("%d-%m-%Y")


def pick_content_sid(kind: str):
    if kind == "wednesday":
        return TWILIO_CONTENT_SID_WEDNESDAY
    return TWILIO_CONTENT_SID_SUNDAY

# ---------- Send ----------

def send_template_message(name: str, mobile_digits: str, kind: str, *, date_str: str, preference: str):
    if not (TWILIO_SID and TWILIO_AUTH and TWILIO_WHATSAPP_FROM and twilio_client):
        print("[ERROR] Missing Twilio configuration")
        return None

    content_sid = pick_content_sid(kind)
    if not content_sid:
        print(f"[ERROR] No Content SID configured for kind={kind}")
        return None

    to_wa = format_whatsapp_to(mobile_digits)
    safe_name = name.strip() if name and name.strip() else NAME_FALLBACK
    pref_val = preference or PREFERENCE_DEFAULT

    variables = {"1": safe_name, "2": date_str, "3": pref_val}

    payload = {
        "from_": TWILIO_WHATSAPP_FROM,
        "to": to_wa,
        "content_sid": content_sid,
        "content_variables": json.dumps(variables),
    }

    try:
        msg = twilio_client.messages.create(**payload)
        print(f"[INFO] Sent ({kind}) to {to_wa}: SID={msg.sid}")
        return msg.sid
    except Exception as e:
        print(f"[ERROR] Send failed to {to_wa} ({kind}): {e}")
        return None

# ---------- Broadcast ----------

def resolve_broadcast_kind(now_dt) -> str:
    dow = now_dt.weekday()
    if dow == 2:  # Wednesday
        return "wednesday"
    if dow == 5:  # Saturday
        return "sunday"
    return "sunday"


def do_broadcast(reason: str = "scheduled", override_kind: str | None = None):
    if not BROADCAST_ENABLE:
        print("[INFO] Broadcast disabled")
        return {"ok": True, "message": "disabled"}

    now_local = pytz.utc.localize(datetime.utcnow()).astimezone(TZ)
    kind = override_kind or resolve_broadcast_kind(now_local)
    if kind not in ("sunday", "wednesday"):
        kind = "sunday"

    recipients = get_recipients_from_sheet()  # (name, digits)
    events_by_digits = load_contact_events()

    if kind == "wednesday":
        date_str = compute_upcoming_wednesday_str(TZ)
    else:
        date_str = compute_upcoming_sunday_str(TZ)

    print(f"[INFO] Broadcast run ({reason}) kind={kind} recipients={len(recipients)} date={date_str}")

    sent = failed = 0
    for (name, digits) in recipients:
        pref = compute_preference_from_history(digits, events_by_digits) or PREFERENCE_DEFAULT
        sid = send_template_message(name, digits, kind, date_str=date_str, preference=pref)
        sent += 1 if sid else 0
        failed += 0 if sid else 1
        time.sleep(THROTTLE_SECONDS)

    summary = {"ok": True, "sent": sent, "failed": failed, "total": len(recipients), "kind": kind, "date": date_str}
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
        override = request.args.get("kind")  # sunday | wednesday
        res = do_broadcast(reason='manual', override_kind=override)
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
