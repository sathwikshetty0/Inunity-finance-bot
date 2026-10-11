from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    webhook_secret: str
    google_service_account_info: dict
    sheet_id: str
    drive_folder_id: str
    admin_telegram_ids: tuple[int, ...]
    nvidia_api_key: str | None
    nvidia_model: str
    ai_check_enabled: bool
    cron_secret: str
    notify_admins: bool


def load_settings() -> Settings:
    required = (
        "TELEGRAM_BOT_TOKEN",
        "WEBHOOK_SECRET",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "SHEET_ID",
        "DRIVE_FOLDER_ID",
        "CRON_SECRET",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    try:
        service_account_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON must contain valid JSON") from exc
    if not isinstance(service_account_info, dict):
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON must be a JSON object")
    webhook_secret = os.environ["WEBHOOK_SECRET"]
    if not 1 <= len(webhook_secret) <= 256 or not all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in webhook_secret
    ):
        raise RuntimeError(
            "WEBHOOK_SECRET must be 1-256 characters containing only letters, digits, '_' or '-'"
        )

    try:
        admin_ids = tuple(
            int(value.strip())
            for value in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
            if value.strip()
        )
    except ValueError as exc:
        raise RuntimeError("ADMIN_TELEGRAM_IDS must be a comma-separated list of integers") from exc

    return Settings(
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        webhook_secret=webhook_secret,
        google_service_account_info=service_account_info,
        sheet_id=os.environ["SHEET_ID"],
        drive_folder_id=os.environ["DRIVE_FOLDER_ID"],
        admin_telegram_ids=admin_ids,
        nvidia_api_key=os.getenv("NVIDIA_API_KEY") or None,
        nvidia_model=os.getenv("NVIDIA_MODEL", "meta/llama-3.2-11b-vision-instruct"),
        ai_check_enabled=os.getenv("AI_CHECK_ENABLED", "false").lower() == "true",
        cron_secret=os.environ["CRON_SECRET"],
        notify_admins=os.getenv("NOTIFY_ADMINS", "true").lower() == "true",
    )
