import asyncio
import os
import sqlite3
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusOffline


DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "telegram_activity.db"
)

CHECK_INTERVAL = max(
    2,
    int(os.getenv("CHECK_INTERVAL", "5"))
)

IST = timezone(
    timedelta(hours=5, minutes=30)
)

app = FastAPI(
    title="Telegram Activity Monitor"
)

clients = {}
pending = {}


# ==================================================
# TIME
# ==================================================

def now():
    return datetime.now(
        IST
    ).isoformat(
        timespec="seconds"
    )


# ==================================================
# DATABASE
# ==================================================

def db():
    c = sqlite3.connect(
        DATABASE_PATH
    )

    c.row_factory = sqlite3.Row

    return c


def init_db():

    c = db()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE NOT NULL,
        api_id INTEGER NOT NULL,
        api_hash TEXT NOT NULL,
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


# ==================================================
# MODELS
# ==================================================

class LoginStart(BaseModel):

    api_id: int
    api_hash: str
    phone: str


class LoginFinish(BaseModel):

    login_id: str
    code: str
    password: str | None = None


class TargetAdd(BaseModel):

    account_id: int
    target: str


# ==================================================
# STARTUP
# ==================================================

@app.on_event("startup")
async def startup():

    init_db()

    asyncio.create_task(
        monitor()
    )


@app.on_event("shutdown")
async def shutdown():

    for client in clients.values():

        try:
            await client.disconnect()

        except Exception:
            pass


# ==================================================
# HOME
# ==================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
async def home():

    return Path(
        "static/index.html"
    ).read_text(
        encoding="utf-8"
    )


# ==================================================
# LOGIN START
# ==================================================

@app.post("/api/login/start")
async def login_start(x: LoginStart):

    if not x.api_id:

        raise HTTPException(
            400,
            "API ID is required."
        )

    if not x.api_hash:

        raise HTTPException(
            400,
            "API Hash is required."
        )

    if not x.phone:

        raise HTTPException(
            400,
            "Phone number is required."
        )


    login_id = secrets.token_urlsafe(24)


    client = TelegramClient(
        StringSession(),
        x.api_id,
        x.api_hash
    )


    try:

        await client.connect()

        result = await client.send_code_request(
            x.phone
        )

    except Exception as e:

        try:
            await client.disconnect()

        except Exception:
            pass

        raise HTTPException(
            400,
            str(e)
        )


    pending[login_id] = {

        "client": client,

        "phone": x.phone,

        "api_id": x.api_id,

        "api_hash": x.api_hash,

        "phone_code_hash":
            result.phone_code_hash

    }


    return {

        "login_id": login_id,

        "message":
            "Telegram code sent."

    }


# ==================================================
# LOGIN FINISH
# ==================================================

@app.post("/api/login/finish")
async def login_finish(x: LoginFinish):

    state =
        pending.get(
            x.login_id
        )

    if not state:

        raise HTTPException(
            400,
            "Login expired."
        )


    client =
        state["client"]


    try:

        await client.sign_in(

            phone=state["phone"],

            code=x.code,

            phone_code_hash=
                state["phone_code_hash"]

        )


    except Exception as e:

        if (
            e.__class__.__name__
            ==
            "SessionPasswordNeededError"
        ):

            if not x.password:

                return {
                    "needs_password":
                        True
                }

            try:

                await client.sign_in(
                    password=x.password
                )

            except Exception as password_error:

                raise HTTPException(
                    400,
                    str(password_error)
                )

        else:

            raise HTTPException(
                400,
                str(e)
            )


    session =
        client.session.save()


    c = db()


    c.execute(
        """
        INSERT INTO accounts(
            phone,
            api_id,
            api_hash,
            session,
            created_at
        )
        VALUES(?,?,?,?,?)

        ON CONFLICT(phone)
        DO UPDATE SET

            api_id=excluded.api_id,

            api_hash=excluded.api_hash,

            session=excluded.session
        """,

        (
            state["phone"],

            state["api_id"],

            state["api_hash"],

            session,

            now()
        )
    )


    c.commit()


    account =
        c.execute(
            """
            SELECT id
            FROM accounts
            WHERE phone=?
            """,
            (state["phone"],)
        ).fetchone()


    c.close()


    account_id =
        account["id"]


    clients[account_id] =
        client


    del pending[
        x.login_id
    ]


    me =
        await client.get_me()


    name = " ".join(

        p for p in [

            me.first_name,

            me.last_name

        ]

        if p

    )


    return {

        "success": True,

        "account_id":
            account_id,

        "telegram_id":
            me.id,

        "name":
            name or str(me.id)

    }


# ==================================================
# GET TELEGRAM CLIENT
# ==================================================

async def get_client(
    account_id
):

    if account_id in clients:

        return clients[
            account_id
        ]


    c = db()


    row =
        c.execute(
            """
            SELECT
                api_id,
                api_hash,
                session
            FROM accounts
            WHERE id=?
            """,
            (account_id,)
        ).fetchone()


    c.close()


    if not row:

        raise HTTPException(
            404,
            "Account not found."
        )


    client = TelegramClient(

        StringSession(
            row["session"]
        ),

        row["api_id"],

        row["api_hash"]

    )


    await client.connect()


    if not await client.is_user_authorized():

        await client.disconnect()

        raise HTTPException(
            401,
            "Telegram session expired."
        )


    clients[account_id] =
        client


    return client


# ==================================================
# ADD TARGET
# ==================================================

@app.post("/api/targets")
async def add_target(
    x: TargetAdd
):

    client =
        await get_client(
            x.account_id
        )


    try:

        entity =
            await client.get_entity(
                x.target
            )

    except Exception as e:

        raise HTTPException(
            400,
            f"Cannot resolve target: {e}"
        )


    if not getattr(
        entity,
        "id",
        None
    ):

        raise HTTPException(
            400,
            "Target is not a Telegram user."
        )


    username =
        getattr(
            entity,
            "username",
            None
        )


    name = " ".join(

        p for p in [

            getattr(
                entity,
                "first_name",
                None
            ),

            getattr(
                entity,
                "last_name",
                None
            )

        ]

        if p

    ) or username or str(entity.id)


    c = db()


    c.execute(
        """
        INSERT INTO targets(
            account_id,
            telegram_id,
            username,
            display_name,
            created_at
        )

        VALUES(?,?,?,?,?)

        ON CONFLICT(
            account_id,
            telegram_id
        )

        DO UPDATE SET

            username=excluded.username,

            display_name=excluded.display_name,

            enabled=1
        """,

        (
            x.account_id,

            entity.id,

            username,

            name,

            now()
        )
    )


    c.commit()


    row =
        c.execute(
            """
            SELECT
                id,
                telegram_id,
                username,
                display_name,
                enabled
            FROM targets
            WHERE account_id=?
            AND telegram_id=?
            """,

            (
                x.account_id,
                entity.id
            )
        ).fetchone()


    c.close()


    return dict(row)


# ==================================================
# TARGET LIST
# ==================================================

@app.get(
    "/api/targets/{account_id}"
)
async def targets(
    account_id: int
):

    c = db()


    rows =
        c.execute(
            """
            SELECT
                id,
                telegram_id,
                username,
                display_name,
                enabled
            FROM targets
            WHERE account_id=?
            ORDER BY id DESC
            """,

            (account_id,)
        ).fetchall()


    c.close()


    return [
        dict(row)
        for row in rows
    ]


# ==================================================
# ACTIVITY
# ==================================================

@app.get(
    "/api/activity/{target_id}"
)
async def activity(
    target_id: int
):

    c = db()


    target =
        c.execute(
            """
            SELECT *
            FROM targets
            WHERE id=?
            """,
            (target_id,)
        ).fetchone()


    events =
        c.execute(
            """
            SELECT
                status,
                observed_at,
                telegram_time
            FROM events
            WHERE target_id=?
            ORDER BY id DESC
            LIMIT 500
            """,
            (target_id,)
        ).fetchall()


    c.close()


    if not target:

        raise HTTPException(
            404,
            "Target not found."
        )


    return {

        "target":
            dict(target),

        "events":
            [
                dict(event)
                for event in events
            ]

    }


# ==================================================
# SAVE EVENT
# ==================================================

async def save_event(
    target_id,
    status,
    telegram_time=None
):

    c = db()


    previous =
        c.execute(
            """
            SELECT status
            FROM events
            WHERE target_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (target_id,)
        ).fetchone()


    if (
        previous
        and
        previous["status"]
        == status
    ):

        c.close()

        return


    c.execute(
        """
        INSERT INTO events(
            target_id,
            status,
            observed_at,
            telegram_time
        )
        VALUES(?,?,?,?)
        """,

        (
            target_id,
            status,
            now(),
            telegram_time
        )
    )


    c.commit()
    c.close()


# ==================================================
# MONITOR
# ==================================================

async def monitor():

    await asyncio.sleep(2)


    while True:

        try:

            c = db()


            rows =
                c.execute(
                    """
                    SELECT *
                    FROM targets
                    WHERE enabled=1
                    """
                ).fetchall()


            c.close()


            for target in rows:

                try:

                    client =
                        await get_client(
                            target["account_id"]
                        )


                    entity =
                        await client.get_entity(
                            target["telegram_id"]
                        )


                    status =
                        entity.status


                    if isinstance(
                        status,
                        UserStatusOnline
                    ):

                        state =
                            "ONLINE"

                        telegram_time =
                            None


                    elif isinstance(
                        status,
                        UserStatusOffline
                    ):

                        state =
                            "OFFLINE"


                        telegram_time = (

                            status.was_online.isoformat()

                            if status.was_online

                            else None

                        )


                    else:

                        state =
                            status.__class__.__name__.replace(
                                "UserStatus",
                                ""
                            ).upper()

                        telegram_time =
                            None


                    await save_event(

                        target["id"],

                        state,

                        telegram_time

                    )


                except Exception:

                    continue


        except Exception:

            pass


        await asyncio.sleep(
            CHECK_INTERVAL
        )
