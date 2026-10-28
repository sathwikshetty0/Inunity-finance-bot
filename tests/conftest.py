import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings


class FakeMessage:
    """Stands in for telegram.Message; records every reply."""

    def __init__(self):
        self.replies = []          # list of (text, reply_markup)
        self.edits = []            # texts sent via edit_text
        self.markup_removed = 0
        self.text = None
        self.photo = None
        self.document = None

    async def reply_text(self, text, reply_markup=None, **kwargs):
        # Return self so progress/reply messages all record in one transcript.
        self.replies.append((text, reply_markup))
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append(text)
        self.replies.append((text, reply_markup))
        return self

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        if reply_markup is None:
            self.markup_removed += 1
        return self


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = 0

    async def answer(self, **kwargs):
        self.answered += 1


class FakeChat:
    type = "private"


class FakeUser:
    def __init__(self, user_id=111, first_name="Test", last_name="User", username="tuser"):
        self.id = user_id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


class FakeUpdate:
    def __init__(self, user=None, data=None, message=None):
        self.effective_user = user or FakeUser()
        self.effective_message = message or FakeMessage()
        self.effective_chat = FakeChat()
        self.callback_query = FakeQuery(data, self.effective_message) if data else None


class FakeSheets:
    def __init__(self):
        self.config = {
            "departments": ["Education", "HR"],
            "expense_types": ["Travel", "Supplies"],
            "programs": {"Education": ["Mindset Program"], "HR": ["Onboarding"]},
            "projects": {"Mindset Program": ["VVCE", "SKIT"]},
        }
        self.appended = []
        self.duplicate = False

    def get_user(self, telegram_id):
        return {
            "telegram_id": str(telegram_id),
            "name": "Test User",
            "email": "test@example.com",
            "default_department": "",
        }

    def get_config(self):
        return self.config

    def has_duplicate(self, *args):
        return self.duplicate

    def append_submission(self, row):
        self.appended.append(row)

    def get_last_submissions(self, telegram_id, limit=5):
        return []


class FakeDrive:
    def __init__(self):
        self.uploads = []

    def upload_bill(self, content, name, month, filename, mime_type, flow_type):
        self.uploads.append((name, month, filename, mime_type, flow_type))
        return f"https://drive.test/{filename}"


class FakeBot:
    async def get_file(self, file_id):
        async def dl(out):
            out.write(b"\xff\xd8fake-image")
        return SimpleNamespace(download_to_memory=dl)

    async def send_message(self, chat_id, text):
        return None


def make_settings(**overrides):
    defaults = dict(
        telegram_bot_token="t",
        webhook_secret="s",
        google_service_account_info={},
        sheet_id="sheet",
        drive_folder_id="folder",
        admin_telegram_ids=(999,),
        nvidia_api_key=None,
        nvidia_model="m",
        ai_check_enabled=False,
        cron_secret="c",
        notify_admins=False,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_context(settings=None, sheets=None, drive=None):
    settings = settings or make_settings()
    sheets = sheets or FakeSheets()
    drive = drive or FakeDrive()
    application = SimpleNamespace(
        bot_data={"settings": settings, "sheets": sheets, "drive": drive}
    )
    context = SimpleNamespace(application=application, bot=FakeBot())
    return context


def run(coro):
    """Only for sync-session tests; async tests just await directly."""
    return asyncio.get_event_loop().run_until_complete(coro)


def session_of(context, user_id=111):
    return context.application.bot_data["sessions"][user_id]


def reply_texts(message):
    return [t for t, _ in message.replies]


def button_callbacks(reply):
    """Flatten a reply's InlineKeyboardMarkup into callback_data list."""
    markup = reply[1]
    if markup is None:
        return []
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def button_labels(reply):
    markup = reply[1]
    if markup is None:
        return []
    return [button.text for row in markup.inline_keyboard for button in row]


def photo_update(user=None, file_id="photo1", file_size=1024):
    """Update carrying a photo attachment."""
    update = FakeUpdate(user=user)
    update.effective_message.photo = [
        SimpleNamespace(file_id=file_id, file_size=file_size)
    ]
    update.effective_message.document = None
    return update
