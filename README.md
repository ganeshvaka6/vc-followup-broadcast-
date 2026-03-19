# Victory Church Follow‑Up (Flask + Google Sheets + Twilio WhatsApp)

Send approved WhatsApp template messages via Twilio to contacts stored in a Google Sheet.
Automatically runs **every Wednesday 09:00** and **every Saturday 20:00** (Asia/Kolkata) using APScheduler.

> **Note**: This package intentionally **omits** the `/qr` endpoint and the `APP_BASE_URL` since you indicated it is not required.

---

## Features
- Read **Name** and **Mobile** from your Google Sheet (columns C and D)
- Send WhatsApp Content Template messages using **Twilio Content SID**
- Scheduled broadcasts: Wed 09:00 and Sat 20:00 (IST)
- Manual broadcast endpoint with header auth
- Existing booking endpoints preserved (`/submit`, `/booked-seats`) if you still use them

---

## 1) Prerequisites
- Python 3.10+
- A Google Cloud **Service Account** with access to your Sheet
- Twilio WhatsApp Business API setup, an approved **template** (Content SID), and a sender (e.g., `whatsapp:+1415...`).

---

## 2) Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` (or export env vars in your platform) based on `.env.example`.
Ensure `SERVICE_ACCOUNT_FILE` points to a valid service account JSON with Sheets/Drive access.

> If using a **spreadsheet key**, set `GOOGLE_SHEET_KEY` and optionally remove `GOOGLE_SHEET_NAME`.

---

## 3) Run locally (development)

```bash
# Option 1: Flask dev server
python app.py

# Option 2: Gunicorn
export PORT=5000
START_SCHEDULER=true gunicorn app:app --bind 0.0.0.0:$PORT
```

Check:
- Health: `GET http://localhost:5000/health`
- Manual broadcast (requires token):

```bash
curl -X POST http://localhost:5000/broadcast/run   -H "X-CLEAR-TOKEN: <your CLEAR_TOKEN>"
```

---

## 4) Environment Variables
See `.env.example` for all variables. Key ones:

- **SERVICE_ACCOUNT_FILE** – Path to your Google credentials JSON
- **GOOGLE_SHEET_KEY** or **GOOGLE_SHEET_NAME** – Your sheet reference
- **TWILIO_ACCOUNT_SID**, **TWILIO_AUTH_TOKEN**, **TWILIO_WHATSAPP_FROM**
- **TWILIO_CONTENT_SID_CONCERT** – Your approved content template SID
- **APP_TIMEZONE** – Defaults to `Asia/Kolkata`
- **START_SCHEDULER** – Set to `true` to enable APScheduler

---

## 5) Scheduler Notes (Production)
- If your host sleeps idly (free dynos), scheduled jobs may not run. Use a separate worker or an external cron that calls `POST /broadcast/run` with the `X-CLEAR-TOKEN` header.
- To avoid duplicate sends across multiple instances, run **one** web/worker or implement a distributed lock (Redis, DB) and idempotency keys (date-slot like `2026-W12-WED09`).

---

## 6) Sheet Format
The app expects the first worksheet (`sheet1`) with columns:

| A Timestamp | B User Code | C Name | D Mobile | E Selected Seats |
|-------------|-------------|--------|----------|------------------|

Broadcast reads columns **C (Name)** and **D (Mobile)**, deduplicated by mobile.

---

## 7) Message Template Variables
The code maps your template variables as:

- `{{1}}` → name (or fallback `Guest`)
- `{{2}}` → seat placeholder (`-` by default)
- `{{3}}` → event time label (`This Week` by default)

Change these in `send_broadcast_message` if your template is different.

---

## 8) Deploying
- **Gunicorn** Procfile is provided: `web: gunicorn app:app`
- Provide env vars in your platform dashboard or a secrets manager.
- Mount or upload your service account JSON at the path specified by `SERVICE_ACCOUNT_FILE`.

---

## 9) Security
- Protect `/broadcast/run` and `/clear-sheet` with a strong `CLEAR_TOKEN`.
- Ensure recipients have explicitly **opted-in** to WhatsApp messaging.

---

## 10) Example /submit payload
```json
{
  "users": [
    {"user_code": "U1", "name": "John Doe", "mobile": "9876543210", "seats": [1,2]},
    {"user_code": "U2", "name": "Mary", "mobile": "919876543210", "seats": "3,4"}
  ]
}
```

---

## Troubleshooting
- **Twilio 63016**: Template mismatch → verify `content_variables` count/order.
- **403/401** from Sheets: Check service account access and sharing.
- **No messages sent**: Verify scheduler running and `BROADCAST_ENABLE=true`.
- **Timezones**: Ensure server time isn’t used; we force `Asia/Kolkata` via APScheduler’s timezone.
