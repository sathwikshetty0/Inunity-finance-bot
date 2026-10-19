from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import re
import uuid
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from googleapiclient.errors import HttpError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.ai_check import (
    AICheckResult,
    compare_extracted_details,
    extract_bill_details,
)
from app.config import Settings
from app.drive_service import DriveService, safe_filename_part
from app.sheets_service import SheetsService

LOGGER = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_BILLS = 5
MAX_COMPLETED_SUBMISSIONS = 10_000
PROJECT_PAGE_SIZE = 8
INDIA_TIMEZONE = timezone(timedelta(hours=5, minutes=30))


def _today() -> date:
    return datetime.now(INDIA_TIMEZONE).date()


def _sessions(context: ContextTypes.DEFAULT_TYPE) -> dict[int, dict[str, Any]]:
    return context.application.bot_data.setdefault("sessions", {})


def _user_lock(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> asyncio.Lock:
    locks = context.application.bot_data.setdefault("user_locks", {})
    return locks.setdefault(user_id, asyncio.Lock())


def _button_rows(
    items: list[tuple[str, str]], columns: int = 2
) -> InlineKeyboardMarkup:
    rows = []
    for start in range(0, len(items), columns):
        rows.append(
            [
                InlineKeyboardButton(label, callback_data=data)
                for label, data in items[start : start + columns]
            ]
        )
    return InlineKeyboardMarkup(rows)


async def route_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    effective_user = update.effective_user
    if effective_user is None:
        return
    async with _user_lock(context, effective_user.id):
        try:
            if update.callback_query:
                await _handle_callback(update, context)
            elif update.effective_message:
                await _handle_message(update, context)
        except Exception:
            LOGGER.exception("Failed to handle Telegram update from user %s", effective_user.id)
            if update.effective_message:
                await update.effective_message.reply_text(
                    "Sorry, we could not complete that step due to a temporary error. "
                    "Check /mylast before retrying in case your submission was saved, or contact your admin."
                )


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    if chat.type != "private":
        await message.reply_text("Please use this bot in a private chat.")
        return

    text = message.text.strip() if message.text else ""
    if text.startswith("/"):
        command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
        if command == "/start":
            await _handle_start(update, context)
        elif command == "/new":
            await _start_submission(update, context)
        elif command == "/cancel":
            _sessions(context).pop(user.id, None)
            await message.reply_text("Your draft was cancelled. Use /new to start another.")
        elif command == "/mylast":
            await _show_last(update, context)
        else:
            await message.reply_text("Available commands: /start, /new, /cancel, /mylast")
        return

    session = _sessions(context).get(user.id)
    if not session:
        await message.reply_text("Use /new to start an expense submission.")
        return
    if message.photo or message.document:
        await _receive_bill(update, session)
        return
    if not text:
        await message.reply_text("Please send a text response or a bill photo/PDF.")
        return
    await _handle_text(update, context, session, text)


async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message = update.effective_message
    staff = await asyncio.to_thread(context.application.bot_data["sheets"].get_user, user_id)
    display_name = (
        staff["name"]
        if staff
        else " ".join(
            part
            for part in (
                update.effective_user.first_name,
                update.effective_user.last_name,
            )
            if part
        )
        or update.effective_user.username
        or f"there (Telegram ID {user_id})"
    )
    await message.reply_text(
        f"Welcome, {display_name}!\n\n"
        "This bot is open to all Telegram users; you do not need to be added to the Users tab "
        "to submit.\n\n"
        "To submit an expense, tap the button below or send /new. Reimbursement submissions "
        "are available to everyone; admins can also choose Direct Payment Flow. Enter the "
        "month, department, program, project/institution, expense type, bill date, vendor, "
        "and amount, then upload the bill.\n\n"
        "/mylast - view your 5 most recent submissions and their status\n"
        "/cancel - discard your current draft",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Start an expense submission", callback_data="new")]]
        ),
    )


async def _start_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    settings: Settings = context.application.bot_data["settings"]
    if user_id in settings.admin_telegram_ids:
        await update.effective_message.reply_text(
            "Choose an expense flow.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Reimbursement Flow", callback_data="flow:reimbursement")],
                    [InlineKeyboardButton("Direct Payment Flow", callback_data="flow:direct")],
                ]
            ),
        )
        return
    await _create_submission(update, context, "Reimbursement Flow")


async def _create_submission(
    update: Update, context: ContextTypes.DEFAULT_TYPE, flow_type: str
) -> None:
    user_id = update.effective_user.id
    settings: Settings = context.application.bot_data["settings"]
    if flow_type == "Direct Payment Flow" and user_id not in settings.admin_telegram_ids:
        await update.effective_message.reply_text("Direct Payment Flow is available to admins only.")
        return

    staff = await asyncio.to_thread(context.application.bot_data["sheets"].get_user, user_id)
    if not staff:
        telegram_user = update.effective_user
        display_name = " ".join(
            part for part in (telegram_user.first_name, telegram_user.last_name) if part
        ) or telegram_user.username or f"Telegram user {user_id}"
        staff = {
            "telegram_id": str(user_id),
            "name": display_name,
            "email": "",
            "default_department": "",
        }
    session = {
        "id": str(uuid.uuid4()),
        "staff": staff,
        "flow_type": flow_type,
        "files": [],
        "ai_result": AICheckResult("Skipped", "AI validation is disabled."),
    }
    _sessions(context)[user_id] = session
    await _prompt_month(update.effective_message, session, _today())


async def _prompt_month(message: Any, session: dict[str, Any], today: date) -> None:
    options = []
    for offset in (-2, -1, 0, 1, 2):
        year = today.year
        month = today.month + offset
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        value = f"{year:04d}-{month:02d}"
        label = datetime(year, month, 1).strftime("%b %Y")
        if offset == 0:
            label += " (default)"
        options.append((label, f"month:{value}"))
    options.append(("Other", "month:other"))
    session["step"] = "month"
    month_name = "reimbursement month" if session["flow_type"] == "Reimbursement Flow" else "expense month"
    await message.reply_text(
        f"Choose the {month_name}. The current month is marked as the default.",
        reply_markup=_button_rows(options, columns=2),
    )


async def _prompt_department(
    message: Any, session: dict[str, Any], context: ContextTypes.DEFAULT_TYPE
) -> None:
    values = (await asyncio.to_thread(context.application.bot_data["sheets"].get_config))[
        "departments"
    ]
    default = session["staff"]["default_department"]
    if default and default not in values:
        values = [default, *values]
    session["step"] = "department"
    buttons = [
        (f"{value} (default)" if value == default else value, f"department:{i}")
        for i, value in enumerate(values)
    ]
    session["department_options"] = values
    await message.reply_text(
        "Choose the department.",
        reply_markup=_button_rows(buttons, columns=1),
    )


async def _prompt_program(
    message: Any, session: dict[str, Any], context: ContextTypes.DEFAULT_TYPE
) -> None:
    config = await asyncio.to_thread(context.application.bot_data["sheets"].get_config)
    values = config["programs"].get(session["department"], [])
    if not values:
        session["step"] = "program_text"
        await message.reply_text("Enter the program or product name.")
        return
    session["step"] = "program"
    session["program_options"] = values
    buttons = [(value, f"program:{i}") for i, value in enumerate(values)]
    buttons.append(("Other (enter manually)", "program:other"))
    await message.reply_text(
        "Choose the program or product.",
        reply_markup=_button_rows(buttons, columns=1),
    )


async def _prompt_expense_type(
    message: Any, session: dict[str, Any], context: ContextTypes.DEFAULT_TYPE
) -> None:
    values = (await asyncio.to_thread(context.application.bot_data["sheets"].get_config))[
        "expense_types"
    ]
    session["step"] = "expense_type"
    session["expense_options"] = values
    buttons = [(value, f"expense:{i}") for i, value in enumerate(values)]
    buttons.append(("Other (enter manually)", "expense:other"))
    await message.reply_text(
        "Choose the expense type.",
        reply_markup=_button_rows(buttons, columns=1),
    )


async def _continue_after_expense_type(message: Any, session: dict[str, Any]) -> None:
    if session.pop("editing", False):
        await _show_summary(message, session)
    else:
        await _prompt_text_field(message, session, "bill_date")


async def _prompt_project(
    message: Any,
    session: dict[str, Any],
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
) -> None:
    config = await asyncio.to_thread(context.application.bot_data["sheets"].get_config)
    values = config["projects"].get(session["program"], [])
    if not values:
        session["step"] = "project_text"
        await message.reply_text("Enter the project or institution name.")
        return
    session["project_options"] = values
    session["project_page"] = page
    session["step"] = "project"
    start = page * PROJECT_PAGE_SIZE
    items = [
        (value, f"project:{i}")
        for i, value in enumerate(values[start : start + PROJECT_PAGE_SIZE], start=start)
    ]
    if start + PROJECT_PAGE_SIZE >= len(values):
        items.append(("Other (enter manually)", "project:other"))
    controls = []
    if page > 0:
        controls.append(("Previous", f"project_page:{page - 1}"))
    if start + PROJECT_PAGE_SIZE < len(values):
        controls.append(("Next", f"project_page:{page + 1}"))
    items.extend(controls)
    await message.reply_text(
        "Choose the project.",
        reply_markup=_button_rows(items, columns=2),
    )


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    try:
        await query.answer()
    except BadRequest as exc:
        if "query is too old" in str(exc).casefold() or "query id is invalid" in str(exc).casefold():
            LOGGER.info("Ignored expired callback query for user %s", user_id)
            return
        raise
    session = _sessions(context).get(user_id)
    if query.data == "new":
        await _start_submission(update, context)
        return
    if query.data in {"flow:reimbursement", "flow:direct"}:
        flow_type = (
            "Reimbursement Flow"
            if query.data == "flow:reimbursement"
            else "Direct Payment Flow"
        )
        await _create_submission(update, context, flow_type)
        return
    if not session:
        completed_id = context.application.bot_data.get("completed_submissions", {}).get(user_id)
        if query.data in {"submit", "warning:continue"} and completed_id:
            await query.message.reply_text(
                "This expense was already submitted successfully. "
                f"Submission ID: {completed_id}\nStatus: Pending."
            )
            return
        await query.message.reply_text("This draft is no longer active. Use /new to start again.")
        return

    data = query.data or ""
    if data.startswith("month:"):
        value = data.split(":", 1)[1]
        if value == "other":
            session["step"] = "month_text"
            await query.message.reply_text("Enter the month as YYYY-MM (for example, 2026-10).")
        else:
            session["month"] = value
            if session.pop("editing", False):
                await _show_summary(query.message, session)
            else:
                await _prompt_department(query.message, session, context)
    elif data.startswith("department:"):
        try:
            value = session["department_options"][int(data.split(":", 1)[1])]
        except (ValueError, IndexError):
            await query.message.reply_text("That department selection has expired. Please retry.")
            return
        session["department"] = value
        session.pop("program", None)
        session.pop("project", None)
        session.pop("expense_type", None)
        await _prompt_program(query.message, session, context)
    elif data.startswith("program:"):
        token = data.split(":", 1)[1]
        if token == "other":
            session["step"] = "program_text"
            await query.message.reply_text("Enter the program or product name.")
            return
        try:
            value = session["program_options"][int(token)]
        except (KeyError, ValueError, IndexError):
            await query.message.reply_text("That program selection has expired. Please retry.")
            return
        session["program"] = value
        session.pop("project", None)
        session.pop("expense_type", None)
        await _prompt_project(query.message, session, context)
    elif data.startswith("expense:"):
        token = data.split(":", 1)[1]
        if token == "other":
            session["step"] = "expense_other"
            await query.message.reply_text("Enter the expense type.")
            return
        try:
            value = session["expense_options"][int(token)]
        except (ValueError, IndexError):
            await query.message.reply_text("That expense type selection has expired. Please retry.")
            return
        if value.casefold() == "other":
            session["step"] = "expense_other"
            await query.message.reply_text("Enter the expense type.")
            return
        session["expense_type"] = value
        await _continue_after_expense_type(query.message, session)
    elif data.startswith("project_page:"):
        await _prompt_project(query.message, session, context, int(data.split(":", 1)[1]))
    elif data.startswith("project:"):
        token = data.split(":", 1)[1]
        if token == "other":
            session["step"] = "project_text"
            await query.message.reply_text("Enter the project or institution name.")
            return
        try:
            value = session["project_options"][int(token)]
        except (ValueError, IndexError):
            await query.message.reply_text("That project selection has expired. Please retry.")
            return
        if value.casefold() == "other":
            session["step"] = "project_text"
            await query.message.reply_text("Enter the project or institution name.")
            return
        session["project"] = value
        session.pop("expense_type", None)
        await _prompt_expense_type(query.message, session, context)
    elif data == "bills:done":
        if not session["files"]:
            await query.message.reply_text("Upload at least one bill photo or PDF before continuing.")
        else:
            await _finish_bill_upload(query.message, context, session)
    elif data == "submit":
        progress = await query.message.reply_text("Submitting your expense. Please wait.")
        await _validate_and_submit(progress, context, user_id, session)
    elif data == "warning:continue":
        progress = await query.message.reply_text("Submitting your expense. Please wait.")
        await _save_submission(progress, context, user_id, session)
    elif data == "warning:fix":
        await _show_edit_fields(query.message)
    elif data.startswith("edit:"):
        await _begin_edit(query.message, context, session, data.split(":", 1)[1])
    elif data == "cancel":
        _sessions(context).pop(user_id, None)
        await query.message.reply_text("Your draft was cancelled. Use /new to start another.")


async def _handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: dict[str, Any],
    text: str,
) -> None:
    message = update.effective_message
    step = session.get("step")
    if step == "month_text":
        try:
            parsed = datetime.strptime(text, "%Y-%m")
            if parsed.strftime("%Y-%m") != text:
                raise ValueError
        except ValueError:
            await message.reply_text("Enter a valid month as YYYY-MM, for example 2026-10.")
            return
        session["month"] = text
        if session.pop("editing", False):
            await _show_summary(message, session)
        else:
            await _prompt_department(message, session, context)
    elif step == "program_text":
        if not text:
            await message.reply_text("Please enter the program or product name.")
            return
        session["program"] = text[:200]
        session.pop("project", None)
        session.pop("expense_type", None)
        await _prompt_project(message, session, context)
    elif step == "project_text":
        if not text:
            await message.reply_text("Please enter the project or institution name.")
            return
        session["project"] = text[:200]
        session.pop("expense_type", None)
        await _prompt_expense_type(message, session, context)
    elif step == "expense_other":
        session["expense_type"] = text[:200]
        await _continue_after_expense_type(message, session)
    elif step == "bill_date":
        parsed = _parse_bill_date(text)
        if not parsed:
            await message.reply_text(
                'Enter the bill date as DD/MM/YYYY, or type "today". Future dates are not accepted.'
            )
            return
        if parsed > _today():
            await message.reply_text("Bill date cannot be in the future. Please enter it again.")
            return
        session["bill_date"] = parsed.isoformat()
        if session.pop("editing", False):
            await _show_summary(message, session)
        else:
            await _prompt_text_field(message, session, "vendor")
    elif step == "vendor":
        if not text:
            await message.reply_text("Please enter the vendor or party name.")
            return
        session["vendor"] = text[:200]
        if session.pop("editing", False):
            await _show_summary(message, session)
        else:
            await _prompt_text_field(message, session, "amount")
    elif step == "amount":
        amount = _parse_amount(text)
        if amount is None or amount <= 0:
            await message.reply_text("Enter an amount greater than zero, such as 1250 or Rs 1,250.")
            return
        session["amount"] = f"{amount:.2f}"
        if session.pop("editing", False):
            await _show_summary(message, session)
        else:
            await _prompt_upload(message, session)
    elif step == "upload":
        if text.casefold() == "done":
            if session["files"]:
                await _finish_bill_upload(message, context, session)
            else:
                await message.reply_text("Upload at least one bill photo or PDF before continuing.")
        else:
            await message.reply_text("Upload a bill photo/PDF, or tap Done when all bills are attached.")
    elif step == "edit_fields":
        await message.reply_text("Choose a field to edit using the buttons above.")
    else:
        await message.reply_text("Please follow the current question or use /cancel.")


async def _prompt_text_field(message: Any, session: dict[str, Any], field: str) -> None:
    prompts = {
        "bill_date": ("bill_date", 'Enter the bill date as DD/MM/YYYY, or type "today".'),
        "vendor": ("vendor", "Enter the vendor or party name."),
        "amount": ("amount", "Enter the amount in Rs (for example, 1,250 or Rs 1,250.50)."),
    }
    session["step"] = field
    await message.reply_text(prompts[field][1])


async def _prompt_upload(message: Any, session: dict[str, Any]) -> None:
    session["step"] = "upload"
    await message.reply_text(
        f"Upload 1 to {MAX_BILLS} bill photos or PDFs (up to 20 MB each). "
        f"{len(session['files'])} attached. Tap Done when finished.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Done", callback_data="bills:done")]]
        ),
    )


async def _finish_bill_upload(
    message: Any, context: ContextTypes.DEFAULT_TYPE, session: dict[str, Any]
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    first_image = next(
        (
            descriptor
            for descriptor in session["files"]
            if descriptor["mime_type"].startswith("image/")
        ),
        None,
    )
    if settings.ai_check_enabled and first_image:
        try:
            image = await asyncio.wait_for(
                _download_bill(context, first_image["file_id"]), timeout=10
            )
        except Exception as exc:
            LOGGER.warning("Could not download bill for detail extraction: %s", exc)
            image = None
        if image:
            first_image["content"] = image
        extraction = await extract_bill_details(
            settings.nvidia_api_key,
            settings.nvidia_model,
            image,
            first_image["mime_type"],
        )
        session["ai_extraction"] = extraction
        if extraction.result != "OK":
            await message.reply_text(
                (extraction.notes or "Automatic reading did not complete.")
                + " The details you entered will be kept for submission."
            )
    elif settings.ai_check_enabled:
        await message.reply_text(
            "Automatic bill checking works with photos, not PDFs. The details you entered will be used."
        )

    await _show_summary(message, session)


async def _receive_bill(
    update: Update, session: dict[str, Any]
) -> None:
    message = update.effective_message
    if session.get("step") != "upload":
        await message.reply_text("Bills can be attached at the bill upload step.")
        return
    if len(session["files"]) >= MAX_BILLS:
        await message.reply_text(f"You can attach no more than {MAX_BILLS} bills.")
        return

    if message.photo:
        telegram_file = message.photo[-1]
        descriptor = {
            "file_id": telegram_file.file_id,
            "size": telegram_file.file_size or 0,
            "mime_type": "image/jpeg",
            "extension": ".jpg",
        }
    else:
        document = message.document
        mime_type = document.mime_type or mimetypes.guess_type(document.file_name or "")[0] or ""
        if mime_type != "application/pdf" and not mime_type.startswith("image/"):
            await message.reply_text("Only bill photos and PDF files are accepted.")
            return
        extension = (
            ".pdf"
            if mime_type == "application/pdf"
            else mimetypes.guess_extension(mime_type) or ".jpg"
        )
        descriptor = {
            "file_id": document.file_id,
            "size": document.file_size or 0,
            "mime_type": mime_type,
            "extension": extension,
        }
    if descriptor["size"] > MAX_UPLOAD_BYTES:
        await message.reply_text("That bill is larger than 20 MB. Please send a smaller file.")
        return
    session["files"].append(descriptor)
    await _prompt_upload(message, session)


async def _show_summary(message: Any, session: dict[str, Any]) -> None:
    session["step"] = "summary"
    session.pop("editing", None)
    staff = session["staff"]
    flow_name = session["flow_type"]
    fields = []
    if flow_name == "Reimbursement Flow":
        fields.append(f"Name: {staff['name']}")
    fields.extend(
        [
            f"Month: {session['month']}",
            f"Department: {session['department']}",
            f"Program name: {session['program']}",
            f"Project/Institution: {session['project']}",
            f"Expense type: {session['expense_type']}",
            f"Bill date: {session['bill_date']}",
            f"Vendor: {session['vendor']}",
            f"Amount: Rs {session['amount']}",
            f"Bill attachments: {len(session['files'])}",
        ]
    )
    summary = f"Please review your {flow_name.lower()}:\n" + "\n".join(fields)
    await message.reply_text(
        summary,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Submit", callback_data="submit")],
                [InlineKeyboardButton("Edit a field", callback_data="warning:fix")],
                [InlineKeyboardButton("Cancel", callback_data="cancel")],
            ]
        ),
    )


async def _show_edit_fields(message: Any) -> None:
    fields = [
        ("Month", "month"),
        ("Department", "department"),
        ("Program name", "program"),
        ("Project/Institution", "project"),
        ("Expense type", "expense_type"),
        ("Bill date", "bill_date"),
        ("Vendor", "vendor"),
        ("Amount", "amount"),
        ("Bill attachments", "bills"),
    ]
    await message.reply_text(
        "Which field would you like to edit?",
        reply_markup=_button_rows([(label, f"edit:{field}") for label, field in fields]),
    )


async def _begin_edit(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    session: dict[str, Any],
    field: str,
) -> None:
    session.pop("ai_result", None)
    session["editing"] = True
    if field == "month":
        await _prompt_month(message, session, _today())
    elif field == "department":
        session.pop("program", None)
        session.pop("project", None)
        session.pop("expense_type", None)
        await _prompt_department(message, session, context)
    elif field == "program":
        session.pop("project", None)
        session.pop("expense_type", None)
        await _prompt_program(message, session, context)
    elif field == "project":
        session.pop("expense_type", None)
        await _prompt_project(message, session, context)
    elif field == "expense_type":
        await _prompt_expense_type(message, session, context)
    elif field in {"bill_date", "vendor", "amount"}:
        await _prompt_text_field(message, session, field)
    elif field == "bills":
        session["files"] = []
        await _prompt_upload(message, session)


async def _validate_and_submit(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session: dict[str, Any],
) -> None:
    if not all(
        key in session
        for key in (
            "flow_type",
            "month",
            "department",
            "program",
            "expense_type",
            "bill_date",
            "vendor",
            "amount",
            "project",
        )
    ) or not session["files"]:
        await message.reply_text("The draft is incomplete. Please use /new to start again.")
        return

    settings: Settings = context.application.bot_data["settings"]
    if session["flow_type"] == "Direct Payment Flow" and user_id not in settings.admin_telegram_ids:
        await message.reply_text("Direct Payment Flow is available to admins only.")
        return
    if session["flow_type"] not in {"Reimbursement Flow", "Direct Payment Flow"}:
        await message.reply_text("That expense flow is invalid. Please use /new to start again.")
        return

    sheets: SheetsService = context.application.bot_data["sheets"]
    duplicate = await asyncio.to_thread(
        sheets.has_duplicate,
        user_id,
        session["vendor"],
        float(session["amount"]),
        session["bill_date"],
    )
    warnings = []
    bill_date = date.fromisoformat(session["bill_date"])
    if bill_date < _today() - timedelta(days=90):
        warnings.append("This bill date is more than 90 days old.")
    if duplicate:
        warnings.append("A possible duplicate (same user, vendor, amount, and bill date) was found.")

    if settings.ai_check_enabled:
        result = compare_extracted_details(
            session.get("ai_extraction"),
            {
                "amount": session["amount"],
                "date": session["bill_date"],
                "vendor": session["vendor"],
            },
        )
    else:
        result = AICheckResult("Skipped", "AI validation is disabled.")
    session["ai_result"] = result
    if result.result in {"Mismatch", "Unreadable"}:
        warnings.append(result.notes)

    if warnings:
        session["step"] = "warning"
        await message.edit_text(
            "Please review before submitting:\n"
            + "\n".join(f"- {warning}" for warning in warnings)
            + "\n\nYou can fix a field or continue. The AI check is advisory only.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Fix a field", callback_data="warning:fix")],
                    [InlineKeyboardButton("Continue anyway", callback_data="warning:continue")],
                ]
            ),
        )
    else:
        await _save_submission(message, context, user_id, session)


async def _save_submission(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session: dict[str, Any],
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if (
        session.get("flow_type") == "Direct Payment Flow"
        and user_id not in settings.admin_telegram_ids
    ):
        await message.edit_text("Direct Payment Flow is available to admins only.")
        return
    drive: DriveService = context.application.bot_data["drive"]
    staff = session["staff"]
    links = []
    try:
        for index, descriptor in enumerate(session["files"], start=1):
            content = descriptor.get("content")
            if not isinstance(content, bytes):
                content = await _download_bill(context, descriptor["file_id"])
            filename = (
                f"{session['bill_date']}_"
                f"{safe_filename_part(session['vendor'])}_"
                f"{safe_filename_part(session['amount'])}_{index}{descriptor['extension']}"
            )
            link = await asyncio.to_thread(
                drive.upload_bill,
                content,
                staff["name"],
                session["month"],
                filename,
                descriptor["mime_type"],
                session["flow_type"],
            )
            links.append(link)

        result: AICheckResult = session.get(
            "ai_result", AICheckResult("Skipped", "AI validation is disabled.")
        )
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row = [
            timestamp,
            session["id"],
            staff["email"],
            staff["name"],
            str(user_id),
            session["month"],
            session["department"],
            session["expense_type"],
            session.get("description", ""),
            session["bill_date"],
            session["vendor"],
            float(Decimal(session["amount"])),
            session["project"],
            ", ".join(links),
            result.result,
            result.notes,
            "Pending",
            "",
            session["flow_type"],
            session["program"],
        ]
        await asyncio.to_thread(context.application.bot_data["sheets"].append_submission, row)
    except HttpError as exc:
        LOGGER.exception("Failed to save reimbursement submission %s", session["id"])
        if b"storageQuotaExceeded" in exc.content:
            error_text = (
                "Google Drive rejected the bill upload because service accounts do not have "
                "personal Drive storage quota. Your draft is still active. Move DRIVE_FOLDER_ID "
                "to a folder inside a Google Workspace Shared Drive and add the service account "
                "as a member with permission to create files, or configure Workspace domain-wide "
                "delegation to upload as a licensed user. Then tap Submit again."
            )
        elif exc.resp.status == 403:
            error_text = (
                "Google denied access while saving your reimbursement. Your draft is still active. "
                "Ask the Drive folder owner to give the service account email from "
                "GOOGLE_SERVICE_ACCOUNT_JSON Editor access to the folder in DRIVE_FOLDER_ID, "
                "so it can create subfolders and upload bills. Also confirm it has Editor access "
                "to the spreadsheet. Then tap Submit again."
            )
        else:
            error_text = (
                "We could not save your expense right now. Your draft is still active; "
                "please try Submit again in a moment. If this continues, contact your admin."
            )
        await message.edit_text(error_text)
        return
    except Exception:
        LOGGER.exception("Failed to save reimbursement submission %s", session["id"])
        await message.edit_text(
            "We could not save your expense right now. Your draft is still active; "
            "please try Submit again in a moment. If this continues, contact your admin."
        )
        return

    _sessions(context).pop(user_id, None)
    completed = context.application.bot_data.setdefault("completed_submissions", OrderedDict())
    completed[user_id] = session["id"]
    completed.move_to_end(user_id)
    while len(completed) > MAX_COMPLETED_SUBMISSIONS:
        completed.popitem(last=False)
    await message.edit_text(
        f"Your {session['flow_type'].lower()} was submitted successfully. "
        f"Submission ID: {session['id']}\n"
        "Status: Pending."
    )
    if settings.notify_admins:
        summary = (
            f"New {session['flow_type']} submission {session['id']}\n"
            f"{staff['name']} - Rs {session['amount']} - {session['vendor']}\n"
            f"{session['department']} / {session['program']} / {session['project']} "
            f"- {session['month']}"
        )
        for admin_id in settings.admin_telegram_ids:
            try:
                await context.bot.send_message(chat_id=admin_id, text=summary)
            except Exception:
                LOGGER.exception("Failed to notify admin Telegram ID %s", admin_id)


async def _download_bill(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    telegram_file = await context.bot.get_file(file_id)
    buffer = io.BytesIO()
    await telegram_file.download_to_memory(out=buffer)
    return buffer.getvalue()


async def _show_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = await asyncio.to_thread(
        context.application.bot_data["sheets"].get_last_submissions, user_id, 5
    )
    if not rows:
        await update.effective_message.reply_text("You have no reimbursement submissions yet.")
        return
    lines = ["Your last submissions (newest first):"]
    for row in rows:
        month = row[5] if len(row) > 5 else "-"
        vendor = row[10] if len(row) > 10 else "-"
        amount = row[11] if len(row) > 11 else "-"
        status = row[16] if len(row) > 16 else "Unknown"
        submission_id = row[1] if len(row) > 1 else "-"
        lines.append(f"{month} | {vendor} | Rs {amount} | {status} | {submission_id}")
    await update.effective_message.reply_text("\n".join(lines))


def _parse_bill_date(value: str) -> date | None:
    if value.strip().casefold() == "today":
        return _today()
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _parse_amount(value: str) -> Decimal | None:
    normalized = value.strip()
    normalized = re.sub(r"(?i)\brs\.?\s*", "", normalized).replace("₹", "")
    normalized = normalized.replace(",", "").replace(" ", "")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", normalized):
        return None
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None
