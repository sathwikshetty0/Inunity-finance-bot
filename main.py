from __future__ import annotations

import asyncio
import hmac
import logging
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
from telegram.ext import Application, TypeHandler

from app.bot_handlers import route_update
from app.config import Settings, load_settings
from app.drive_service import DriveService
from app.sheets_service import SheetsService

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)
MAX_REMEMBERED_UPDATES = 10_000
INDIA_TIMEZONE = timezone(timedelta(hours=5, minutes=30))


async def _error_handler(update: object, context: Any) -> None:
    error = context.error
    if error:
        LOGGER.error(
            "Unhandled Telegram update error",
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        LOGGER.error("Unhandled Telegram update error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = load_settings()
    sheets = SheetsService(settings.google_service_account_info, settings.sheet_id)
    drive = DriveService(settings.google_service_account_info, settings.drive_folder_id)
    bot_app = Application.builder().token(settings.telegram_bot_token).build()
    bot_app.bot_data.update(settings=settings, sheets=sheets, drive=drive)
    bot_app.add_handler(TypeHandler(Update, route_update))
    bot_app.add_error_handler(_error_handler)
    await bot_app.initialize()
    await bot_app.start()

    external_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if external_url:
        await bot_app.bot.set_webhook(
            url=f"{external_url}/telegram/webhook",
            secret_token=settings.webhook_secret,
            allowed_updates=Update.ALL_TYPES,
        )
        LOGGER.info("Telegram webhook configured for %s/telegram/webhook", external_url)
    else:
        LOGGER.warning(
            "RENDER_EXTERNAL_URL is not set; configure the Telegram webhook manually before use"
        )

    app.state.bot_app = bot_app
    app.state.settings = settings
    app.state.processed_updates = OrderedDict()
    app.state.update_lock = asyncio.Lock()
    try:
        yield
    finally:
        await bot_app.stop()
        await bot_app.shutdown()


app = FastAPI(title="Staff Expense Reimbursement Bot", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict[str, bool]:
    settings: Settings = request.app.state.settings
    if not secret_token or not hmac.compare_digest(secret_token, settings.webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise TypeError("Telegram update must be a JSON object")
        update = Update.de_json(payload, request.app.state.bot_app.bot)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Telegram update") from exc

    if update.update_id is None:
        raise HTTPException(status_code=400, detail="Telegram update is missing update_id")
    processed: OrderedDict = request.app.state.processed_updates
    lock: asyncio.Lock = request.app.state.update_lock
    async with lock:
        if update.update_id in processed:
            return {"ok": True}
        processed[update.update_id] = None
        while len(processed) > MAX_REMEMBERED_UPDATES:
            processed.popitem(last=False)
    try:
        await request.app.state.bot_app.process_update(update)
    except Exception:
        async with lock:
            processed.pop(update.update_id, None)
        LOGGER.exception("Failed to process Telegram update_id=%s", update.update_id)
        raise HTTPException(status_code=500, detail="Update processing failed")
    return {"ok": True}


@app.post("/cron")
async def weekly_reminder(
    request: Request,
    cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict[str, int]:
    settings: Settings = request.app.state.settings
    if not cron_secret or not hmac.compare_digest(cron_secret, settings.cron_secret):
        raise HTTPException(status_code=403, detail="Invalid cron secret")
    bot_app: Application = request.app.state.bot_app
    sheets: SheetsService = bot_app.bot_data["sheets"]
    month = datetime.now(INDIA_TIMEZONE).strftime("%Y-%m")
    users, submitted = await asyncio.gather(
        asyncio.to_thread(sheets.get_users),
        asyncio.to_thread(sheets.get_telegram_ids_submitted_for_month, month),
    )
    sent = 0
    failed = 0
    for user in users:
        if user["telegram_id"] in submitted:
            continue
        try:
            await bot_app.bot.send_message(
                chat_id=int(user["telegram_id"]),
                text=(
                    f"Friendly reminder: you have not submitted a reimbursement for {month} yet. "
                    "If you have expenses to claim, use /new."
                ),
            )
            sent += 1
        except Exception:
            LOGGER.exception("Could not send monthly reminder to a staff member")
            failed += 1
    LOGGER.info("Monthly reminder run for %s: sent=%d failed=%d", month, sent, failed)
    return {"sent": sent, "failed": failed}
