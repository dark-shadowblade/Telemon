# Telegram Activity Monitor

Starter FastAPI + Telethon application.

Flow:
1. Server stores API ID/API hash in environment variables.
2. User logs into Telegram through the web UI.
3. The server stores the authorized session.
4. User adds Telegram usernames/IDs.
5. The background worker records status observations.
6. Dashboard displays the recorded activity.

The app only uses status information available to the authorized Telegram
account and does not bypass Telegram privacy controls.

Run:
    pip install -r requirements.txt
    copy .env.example .env
    uvicorn app:app --host 0.0.0.0 --port 8000

Never commit .env or Telegram session credentials.
