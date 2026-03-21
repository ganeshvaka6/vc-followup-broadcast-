# Victory Church Follow‑Up (Computed Preference + Utility Templates)

Sends **Utility** WhatsApp templates for **Sunday** and **Wednesday** using Twilio Content SIDs.
Variables per template: `{{1}}` Name (from sheet), `{{2}}` Date (computed), `{{3}}` Preference (computed from sheet history).

## Google Sheet Layout (as per your screenshot)
```
A Timestamp | B User Code | C Name | D Mobile
```
No "Preference" column required. The app computes a label per contact from history:
- >= `PREF_THRESHOLD_WEEKLY` rows in last `PREF_WINDOW_WEEKLY_DAYS` -> **Weekly**
- >= `PREF_THRESHOLD_BIWEEKLY` rows in last `PREF_WINDOW_BIWEEKLY_DAYS` -> **Biweekly**
- else -> **Monthly**

## Environment Variables
See `.env.example` for all required settings.
- Add your two Utility template SIDs: `TWILIO_CONTENT_SID_SUNDAY`, `TWILIO_CONTENT_SID_WEDNESDAY`.
- Add Google credential secret file at `/etc/secrets/service_account.json` and share the sheet with that service account email (Editor).

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Endpoints
- `GET /health`
- `POST /broadcast/run?kind=sunday|wednesday` (header: `X-CLEAR-TOKEN`)
- `POST /clear-sheet` (header: `X-CLEAR-TOKEN`)

## Scheduler
APScheduler triggers at IST times:
- Wednesday 09:00 -> sends Wednesday Utility (auto date + preference)
- Saturday 20:00 -> sends Sunday Utility (auto date + preference)

## Notes
- Ensure template variable counts match (3 variables). Twilio error 63016 indicates a mismatch.
- Increase `THROTTLE_PER_MESSAGE_SECONDS` if your list is very large.
