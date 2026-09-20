import asyncio
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusOffline

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
DB_PATH = os.getenv("DATABASE_PATH", "telegram_activity.db")
CHECK_INTERVAL = max(2, int(os.getenv("CHECK_INTERVAL", "5")))
IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Telegram Activity Monitor")
clients = {}
pending = {}
monitor_task = None


def now():
    return datetime.now(IST).isoformat(timespec="seconds")


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE NOT NULL,
        session TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS targets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        telegram_id INTEGER NOT NULL,
        username TEXT,
        display_name TEXT,
        enabled INTEGER DEFAULT 1,
        created_at TEXT NOT NULL,
        UNIQUE(account_id, telegram_id)
    );
    CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        telegram_time TEXT
    );
    """)
    c.commit()
    c.close()


class LoginStart(BaseModel):
    phone: str


class LoginFinish(BaseModel):
    login_id: str
    code: str
    password: str | None = None


class TargetAdd(BaseModel):
    account_id: int
    target: str


@app.on_event("startup")
async def startup():
    global monitor_task
    init_db()
    monitor_task = asyncio.create_task(monitor())


@app.on_event("shutdown")
async def shutdown():
    for client in clients.values():
        await client.disconnect()


@app.get("/", response_class=HTMLResponse)
async def home():
    return Path("static/index.html").read_text(encoding="utf-8")


@app.post("/api/login/start")
async def login_start(x: LoginStart):
    if not API_ID or not API_HASH:
        raise HTTPException(500, "Configure API_ID and API_HASH first.")

    import secrets
    login_id = secrets.token_urlsafe(24)

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    result = await client.send_code_request(x.phone)

    pending[login_id] = {
        "client": client,
        "phone": x.phone,
        "hash": result.phone_code_hash
    }

    return {"login_id": login_id, "message": "Telegram code sent."}


@app.post("/api/login/finish")
async def login_finish(x: LoginFinish):
    state = pending.get(x.login_id)
    if not state:
        raise HTTPException(400, "Login expired.")

    client = state["client"]

    try:
        await client.sign_in(
            phone=state["phone"],
            code=x.code,
            phone_code_hash=state["hash"]
        )
    except Exception as e:
        if e.__class__.__name__ != "SessionPasswordNeededError":
            raise HTTPException(400, str(e))
        if not x.password:
            return {"needs_password": True}
        await client.sign_in(password=x.password)

    session = client.session.save()

    c = db()
    c.execute("""
        INSERT INTO accounts(phone, session, created_at)
        VALUES(?,?,?)
        ON CONFLICT(phone) DO UPDATE SET
        session=excluded.session
    """, (state["phone"], session, now()))
    c.commit()

    account = c.execute(
        "SELECT id FROM accounts WHERE phone=?",
        (state["phone"],)
    ).fetchone()
    c.close()

    account_id = account["id"]
    clients[account_id] = client
    del pending[x.login_id]

    me = await client.get_me()

    return {
        "success": True,
        "account_id": account_id,
        "telegram_id": me.id,
        "name": " ".join(
            p for p in [me.first_name, me.last_name] if p
        )
    }


async def get_client(account_id):
    if account_id in clients:
        return clients[account_id]

    c = db()
    row = c.execute(
        "SELECT session FROM accounts WHERE id=?",
        (account_id,)
    ).fetchone()
    c.close()

    if not row:
        raise HTTPException(404, "Account not found.")

    client = TelegramClient(
        StringSession(row["session"]),
        API_ID,
        API_HASH
    )
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise HTTPException(401, "Telegram session expired.")

    clients[account_id] = client
    return client


@app.post("/api/targets")
async def add_target(x: TargetAdd):
    client = await get_client(x.account_id)

    try:
        entity = await client.get_entity(x.target)
    except Exception as e:
        raise HTTPException(400, f"Cannot resolve target: {e}")

    if not getattr(entity, "id", None):
        raise HTTPException(400, "Target is not a Telegram user.")

    username = getattr(entity, "username", None)
    name = " ".join(
        p for p in [
            getattr(entity, "first_name", None),
            getattr(entity, "last_name", None)
        ] if p
    ) or username or str(entity.id)

    c = db()
    c.execute("""
        INSERT INTO targets(
            account_id, telegram_id, username, display_name, created_at
        )
        VALUES(?,?,?,?,?)
        ON CONFLICT(account_id, telegram_id) DO UPDATE SET
            username=excluded.username,
            display_name=excluded.display_name,
            enabled=1
    """, (x.account_id, entity.id, username, name, now()))
    c.commit()

    row = c.execute("""
        SELECT id, telegram_id, username, display_name, enabled
        FROM targets
        WHERE account_id=? AND telegram_id=?
    """, (x.account_id, entity.id)).fetchone()
    c.close()

    return dict(row)


@app.get("/api/targets/{account_id}")
async def targets(account_id: int):
    c = db()
    rows = c.execute("""
        SELECT id, telegram_id, username, display_name, enabled
        FROM targets WHERE account_id=?
        ORDER BY id DESC
    """, (account_id,)).fetchall()
    c.close()
    return [dict(x) for x in rows]


@app.get("/api/activity/{target_id}")
async def activity(target_id: int):
    c = db()
    target = c.execute(
        "SELECT * FROM targets WHERE id=?",
        (target_id,)
    ).fetchone()

    events = c.execute("""
        SELECT status, observed_at, telegram_time
        FROM events
        WHERE target_id=?
        ORDER BY id DESC
        LIMIT 500
    """, (target_id,)).fetchall()
    c.close()

    if not target:
        raise HTTPException(404, "Target not found.")

    return {
        "target": dict(target),
        "events": [dict(x) for x in events]
    }


async def save_event(target_id, status, telegram_time=None):
    c = db()
    previous = c.execute("""
        SELECT status FROM events
        WHERE target_id=?
        ORDER BY id DESC LIMIT 1
    """, (target_id,)).fetchone()

    if previous and previous["status"] == status:
        c.close()
        return

    c.execute("""
        INSERT INTO events(
            target_id, status, observed_at, telegram_time
        ) VALUES(?,?,?,?)
    """, (target_id, status, now(), telegram_time))
    c.commit()
    c.close()


async def monitor():
    await asyncio.sleep(2)

    while True:
        try:
            c = db()
            rows = c.execute("""
                SELECT * FROM targets WHERE enabled=1
            """).fetchall()
            c.close()

            for target in rows:
                try:
                    client = await get_client(target["account_id"])
                    entity = await client.get_entity(target["telegram_id"])
                    status = entity.status

                    if isinstance(status, UserStatusOnline):
                        state = "ONLINE"
                        telegram_time = None
                    elif isinstance(status, UserStatusOffline):
                        state = "OFFLINE"
                        telegram_time = (
                            status.was_online.isoformat()
                            if status.was_online else None
                        )
                    else:
                        state = status.__class__.__name__.replace(
                            "UserStatus", ""
                        ).upper()
                        telegram_time = None

                    await save_event(
                        target["id"],
                        state,
                        telegram_time
                    )
                except Exception:
                    continue

        except Exception:
            pass

        await asyncio.sleep(CHECK_INTERVAL)
