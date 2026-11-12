"""Local Telegram comment picker. Credentials and sessions stay outside the repository."""
import asyncio
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, SecretStr
from telethon import TelegramClient, errors, types
import uvicorn

os.umask(0o077)
DATA = Path(os.environ.get("ZNWR_PICKER_DATA_DIR", Path.home() / "Codex/context/projects/znwr/telegram-picker-private"))
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
CONFIG = DATA / "credentials.json"
ROOT = Path(__file__).resolve().parent
client = None
credentials = None
phone = None
phone_code_hash = None
cached_url = None
comments = []
progress = {"request_id": None, "phase": "idle", "count": 0}
lock = asyncio.Lock()


def make_client(settings):
    return TelegramClient(str(DATA / "telegram"), settings["api_id"], settings["api_hash"],
                          receive_updates=False, flood_sleep_threshold=0,
                          device_model="ZNWR Comment Picker", request_retries=2)


def save_credentials():
    temporary = CONFIG.with_suffix(".tmp")
    temporary.write_text(json.dumps(credentials))
    temporary.replace(CONFIG)


@asynccontextmanager
async def lifespan(app):
    global client, credentials
    if CONFIG.exists():
        credentials = json.loads(CONFIG.read_text())
        client = make_client(credentials)
    yield
    if client and client.is_connected():
        await client.disconnect()


app = FastAPI(lifespan=lifespan)
PAGES_ORIGIN = "https://grigand96-hash.github.io"
LOCAL_ORIGINS = {"http://127.0.0.1:8894", "http://localhost:8894"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Only the published picker may use the loopback service across origins.
    if request.headers.get("host") not in {"127.0.0.1:8894", "localhost:8894"}:
        return JSONResponse({"detail": "Откройте сервис через localhost."}, status_code=403)
    origin = request.headers.get("origin")
    pages_request = origin == PAGES_ORIGIN and request.url.path in {"/api/status", "/api/draw", "/api/progress"}
    if request.method != "GET" and origin not in LOCAL_ORIGINS and not pages_request:
        return JSONResponse({"detail": "Запрос с другого сайта запрещён."}, status_code=403)
    if request.method == "OPTIONS" and pages_request:
        return JSONResponse({}, headers={
            "Access-Control-Allow-Origin": PAGES_ORIGIN,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Private-Network": "true",
            "Vary": "Origin",
        })
    try:
        response = await call_next(request)
    except errors.FloodWaitError as error:
        response = JSONResponse({"detail": f"Telegram просит подождать {error.seconds} сек."}, status_code=429)
    except errors.RPCError:
        response = JSONResponse({"detail": "Telegram отклонил запрос. Проверьте данные входа и доступ к обсуждению."}, status_code=400)
    except (ConnectionError, OSError, asyncio.TimeoutError):
        response = JSONResponse({"detail": "Нет связи с Telegram. Попробуйте ещё раз."}, status_code=503)
    response.headers["Cache-Control"] = "no-store"
    if pages_request:
        response.headers["Access-Control-Allow-Origin"] = PAGES_ORIGIN
        response.headers["Vary"] = "Origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


async def connected():
    if client is None:
        raise HTTPException(401, "Подключите Telegram.")
    if not client.is_connected():
        await client.connect()
    return client


async def authorized():
    telegram = await connected()
    if not await telegram.is_user_authorized():
        raise HTTPException(401, "Войдите в Telegram.")
    return telegram


@app.get("/")
async def index():
    return FileResponse(ROOT / "static/index.html")


@app.get("/style.css")
async def stylesheet():
    return FileResponse(ROOT / "static/style.css")


@app.get("/app.js")
async def javascript():
    return FileResponse(ROOT / "static/app.js")


@app.get("/api/status")
async def status():
    async with lock:
        if client is None:
            return {"authorized": False}
        telegram = await connected()
        return {"authorized": await telegram.is_user_authorized()}


class Login(BaseModel):
    api_id: int = Field(gt=0)
    api_hash: SecretStr
    phone: str = Field(min_length=5, max_length=30)


@app.post("/api/login")
async def login(data: Login):
    global client, credentials, phone, phone_code_hash
    api_hash = data.api_hash.get_secret_value().strip()
    if not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
        raise HTTPException(400, "API hash должен содержать 32 символа.")
    async with lock:
        if client and client.is_connected():
            await client.disconnect()
        credentials = {"api_id": data.api_id, "api_hash": api_hash}
        client = make_client(credentials)
        telegram = await connected()
        if await telegram.is_user_authorized():
            save_credentials()
            return {"step": "ready"}
        phone = data.phone.strip()
        sent = await telegram.send_code_request(phone)
        phone_code_hash = sent.phone_code_hash
        return {"step": "code"}


class Verify(BaseModel):
    code: SecretStr = SecretStr("")
    password: SecretStr = SecretStr("")


@app.post("/api/verify")
async def verify(data: Verify):
    global phone_code_hash, phone
    async with lock:
        telegram = await connected()
        try:
            if data.password.get_secret_value():
                await telegram.sign_in(password=data.password.get_secret_value())
            else:
                if not phone_code_hash:
                    raise HTTPException(400, "Сначала запросите код входа.")
                await telegram.sign_in(phone=phone, code=data.code.get_secret_value().replace(" ", ""),
                                       phone_code_hash=phone_code_hash)
        except errors.SessionPasswordNeededError:
            return {"step": "password"}
        save_credentials()
        phone = phone_code_hash = None
        return {"step": "ready"}


def parse_post(value):
    url = urlparse(value.strip())
    match = re.fullmatch(r"/([A-Za-z][A-Za-z0-9_]{0,31})/([1-9][0-9]*)/?", url.path)
    if url.scheme != "https" or url.netloc != "t.me" or not match:
        raise HTTPException(400, "Вставьте ссылку вида https://t.me/channel/123.")
    channel, post = match.groups()
    return channel, int(post), f"https://t.me/{channel}/{post}"


async def load_comments(telegram, channel, post):
    # Complete the traversal before making a draw. Partial results never enter the cache.
    loaded = []
    async for message in telegram.iter_messages(channel, reply_to=post, limit=None):
        if isinstance(message, types.Message):
            loaded.append(message)
            progress["count"] = len(loaded)
    return loaded


class Draw(BaseModel):
    url: str = Field(max_length=2048)
    request_id: str = Field(default="", max_length=64)


@app.get("/api/progress")
async def draw_progress(request_id: str):
    if request_id != progress["request_id"]:
        return {"phase": "waiting", "count": 0}
    return {"phase": progress["phase"], "count": progress["count"]}


@app.post("/api/draw")
async def draw(data: Draw):
    global cached_url, comments
    channel, post, url = parse_post(data.url)
    async with lock:
        progress.update(request_id=data.request_id, phase="connecting", count=0)
        telegram = await authorized()
        if cached_url != url:
            progress["phase"] = "collecting"
            try:
                loaded = await load_comments(telegram, channel, post)
            except (ValueError, errors.MsgIdInvalidError):
                raise HTTPException(400, "Пост или его обсуждение недоступны этому аккаунту.")
            if not loaded:
                raise HTTPException(400, "Под этим постом нет доступных комментариев.")
            comments = loaded
            cached_url = url
        progress.update(phase="selecting", count=len(comments))
        selected = secrets.choice(comments)
        sender = await selected.get_sender()
        author = "Автор комментария"
        if sender:
            author = getattr(sender, "title", None) or " ".join(filter(None, [
                getattr(sender, "first_name", None), getattr(sender, "last_name", None)
            ])) or author
            if getattr(sender, "username", None):
                author += " · @" + sender.username
        caption = selected.message or ("Фото" if selected.photo else "Вложение" if selected.media else "Комментарий")
        progress["phase"] = "done"
        return {"author": author, "text": caption[:1500], "count": len(comments),
                "url": f"{url}?comment={selected.id}"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8894, access_log=False)
