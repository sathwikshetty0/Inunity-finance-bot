from __future__ import annotations

import logging
import threading
import time
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

LOGGER = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CONFIG_CACHE_TTL_SECONDS = 60

DATA_HEADERS = [
    "Timestamp",
    "Submission ID",
    "Email",
    "Name",
    "Telegram ID",
    "Month",
    "Department",
    "Expense Type",
    "Description",
    "Bill Date",
    "Vendor",
    "Amount",
    "Project",
    "Bill Links",
    "AI Check Result",
    "AI Notes",
    "Status",
    "Admin Remarks",
    "Flow Type",
    "Program Name",
]

DEFAULTS = {
    "departments": [
        "Education",
        "Community Engagement",
        "Startup Studio",
        "Tech Platform",
        "Marketing",
        "Sales",
        "HR",
        "Finance",
        "Admin",
    ],
    "expense_types": [
        "Travelling Charges",
        "Accommodation Charges",
        "Purchases",
        "Subscription Charges(Tech)",
        "Subscription Charges(Office)",
        "Subscription Charges(Marketing)",
        "Employee Benefit Expenses",
        "Employee Engagement Expenses",
        "Hiring Charges",
        "L&D Charges",
        "Printing Charges",
        "Stationery Charges",
        "Office Expenses",
    ],
}

DEFAULT_PROGRAMS = {
    "Education": [
        "Mindset Program",
        "Innovation Week",
        "Workshop",
        "Tech Courses",
        "Bridge Course",
        "FDP",
        "Short term Programs",
        "Others",
    ],
    "Community Engagement": ["Problem Statement Scouting", "Events"],
    "Startup Studio": ["Pre Incubation", "Incubation"],
    "Tech Platform": [
        "INPULSE ERP",
        "INPULSE CONTEST",
        "INPULSE LMS",
        "INPULSE RECRUIT",
    ],
    "Marketing": [
        "Marketing Collaterals",
        "Marketing Travel",
        "Marketing Subscriptions",
        "Marketing Campaigns/Events",
    ],
}

DEFAULT_PROJECTS = {
    "Mindset Program": ["VVCE", "SKIT", "SSSE", "KSIT", "SSIT", "Acharya Institute", "GMIT"],
    "Events": ["TITAN Makeathons"],
}


class SheetsService:
    def __init__(self, service_account_info: dict[str, Any], sheet_id: str) -> None:
        credentials = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
        self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self.sheet_id = sheet_id
        self._config_cache: tuple[float, dict[str, Any]] | None = None
        self._config_lock = threading.Lock()
        self._submission_ids: set[str] | None = None

    def get_user(self, telegram_id: int) -> dict[str, str] | None:
        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range="'Users'!A2:E")
            .execute()
        )
        for row in response.get("values", []):
            if len(row) < 5:
                continue
            if row[0].strip() == str(telegram_id) and row[4].strip().upper() == "TRUE":
                return {
                    "telegram_id": row[0].strip(),
                    "name": row[1].strip(),
                    "email": row[2].strip(),
                    "default_department": row[3].strip(),
                }
        return None

    def get_config(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._config_cache and now - self._config_cache[0] < CONFIG_CACHE_TTL_SECONDS:
            return self._config_cache[1]
        # Serialize refreshes so concurrent users trigger at most one Sheets read.
        with self._config_lock:
            now = time.monotonic()
            if self._config_cache and now - self._config_cache[0] < CONFIG_CACHE_TTL_SECONDS:
                return self._config_cache[1]
            try:
                values = self._load_config()
            except Exception:
                # Keep serving the last valid configuration; retry on the next call.
                if self._config_cache is not None:
                    LOGGER.exception(
                        "Config refresh failed; using the last cached configuration"
                    )
                    return self._config_cache[1]
                raise
            self._config_cache = (now, values)
            return values

    def _load_config(self) -> dict[str, Any]:
        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range="'Config'!A2:C")
            .execute()
        )
        values: dict[str, Any] = {
            "departments": [],
            "expense_types": [],
            "programs": {},
            "projects": {},
        }
        seen: dict[str, set] = {
            "department": set(),
            "expense_type": set(),
            "program": set(),
            "project": set(),
        }
        for row_number, row in enumerate(response.get("values", []), start=2):
            cells = [str(cell).strip() for cell in row]
            if not any(cells):
                continue
            category = cells[0].lower() if cells else ""
            value = cells[1] if len(cells) > 1 else ""
            parent = cells[2] if len(cells) > 2 else ""
            if not value:
                # A category or parent with no Value is a leftover stub row;
                # only warn when a Parent is set, which is genuinely malformed.
                if parent:
                    LOGGER.warning(
                        "Config row %d ignored: Parent %r set but Value is blank",
                        row_number,
                        parent,
                    )
                continue
            if category in {"department", "departments"}:
                key = value.casefold()
                if key in seen["department"]:
                    LOGGER.warning(
                        "Config row %d ignored: duplicate department %r", row_number, value
                    )
                    continue
                seen["department"].add(key)
                values["departments"].append(value)
            elif category in {"expense_type", "expense_types", "expensetype", "expensetypes"}:
                key = value.casefold()
                if key in seen["expense_type"]:
                    LOGGER.warning(
                        "Config row %d ignored: duplicate expense type %r", row_number, value
                    )
                    continue
                seen["expense_type"].add(key)
                values["expense_types"].append(value)
            elif category in {"program", "programs"}:
                if not parent:
                    LOGGER.warning(
                        "Config row %d ignored: program %r has no Parent department",
                        row_number,
                        value,
                    )
                    continue
                if parent.casefold() == value.casefold():
                    LOGGER.warning(
                        "Config row %d ignored: program %r cannot be its own parent",
                        row_number,
                        value,
                    )
                    continue
                key = (parent.casefold(), value.casefold())
                if key in seen["program"]:
                    LOGGER.warning(
                        "Config row %d ignored: duplicate program %r under %r",
                        row_number,
                        value,
                        parent,
                    )
                    continue
                seen["program"].add(key)
                values["programs"].setdefault(parent, []).append(value)
            elif category in {"project", "projects", "institution", "institutions"}:
                if not parent:
                    LOGGER.warning(
                        "Config row %d ignored: project %r has no Parent program",
                        row_number,
                        value,
                    )
                    continue
                if parent.casefold() == value.casefold():
                    LOGGER.warning(
                        "Config row %d ignored: project %r cannot be its own parent",
                        row_number,
                        value,
                    )
                    continue
                key = (parent.casefold(), value.casefold())
                if key in seen["project"]:
                    LOGGER.warning(
                        "Config row %d ignored: duplicate project %r under %r",
                        row_number,
                        value,
                        parent,
                    )
                    continue
                seen["project"].add(key)
                values["projects"].setdefault(parent, []).append(value)
            else:
                LOGGER.warning(
                    "Config row %d ignored: unknown Category %r", row_number, cells[0]
                )
        self._validate_config(values)
        return values

    @staticmethod
    def _validate_config(values: dict[str, Any]) -> None:
        departments = {d.casefold() for d in values["departments"]}
        program_names = {
            program.casefold()
            for programs in values["programs"].values()
            for program in programs
        }
        for parent in values["programs"]:
            if parent.casefold() not in departments:
                LOGGER.warning(
                    "Config: program parent %r is not a configured department; "
                    "programs under it are unreachable",
                    parent,
                )
        for parent in values["projects"]:
            if parent.casefold() not in program_names:
                LOGGER.warning(
                    "Config: project parent %r is not a configured program; "
                    "projects under it are unreachable",
                    parent,
                )

    def append_submission(self, row: list[Any]) -> None:
        if len(row) != len(DATA_HEADERS):
            raise ValueError(f"Expected {len(DATA_HEADERS)} submission values, got {len(row)}")
        submission_id = str(row[1])
        if self._submission_ids is None:
            existing = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self.sheet_id, range="'Data'!B2:B")
                .execute()
            )
            self._submission_ids = {
                values[0] for values in existing.get("values", []) if values and values[0]
            }
        if submission_id in self._submission_ids:
            return
        (
            self._service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.sheet_id,
                range="'Data'!A:T",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            )
            .execute()
        )
        self._submission_ids.add(submission_id)

    def has_duplicate(
        self, telegram_id: int, vendor: str, amount: float, bill_date: str
    ) -> bool:
        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range="'Data'!A2:T")
            .execute()
        )
        rows = response.get("values", [])
        if self._submission_ids is None:
            self._submission_ids = {
                row[1] for row in rows if len(row) > 1 and row[1]
            }
        for row in rows:
            if len(row) < 12:
                continue
            try:
                same_amount = float(row[11].replace(",", "")) == amount
            except ValueError:
                same_amount = False
            if (
                row[4].strip() == str(telegram_id)
                and row[10].strip().casefold() == vendor.strip().casefold()
                and same_amount
                and row[9].strip() == bill_date
            ):
                return True
        return False

    def get_last_submissions(self, telegram_id: int, limit: int = 5) -> list[list[str]]:
        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range="'Data'!A2:T")
            .execute()
        )
        rows = [
            row
            for row in response.get("values", [])
            if len(row) >= 17 and row[4].strip() == str(telegram_id)
        ]
        return list(reversed(rows[-limit:]))

    def get_submissions_for_month(self, telegram_id: int, month: str) -> list[list[str]]:
        """Return the user's reimbursement submissions for a YYYY-MM month."""
        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range="'Data'!A2:T")
            .execute()
        )
        return [
            row
            for row in response.get("values", [])
            if len(row) >= 12
            and row[4].strip() == str(telegram_id)
            and row[5].strip() == month
            and (len(row) <= 18 or row[18].strip() != "Direct Payment Flow")
        ]

    def get_users(self) -> list[dict[str, str]]:
        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range="'Users'!A2:E")
            .execute()
        )
        users = []
        for row in response.get("values", []):
            if len(row) >= 5 and row[4].strip().upper() == "TRUE":
                users.append(
                    {
                        "telegram_id": row[0].strip(),
                        "name": row[1].strip(),
                        "email": row[2].strip(),
                        "default_department": row[3].strip(),
                    }
                )
        return users

    def get_telegram_ids_submitted_for_month(self, month: str) -> set[str]:
        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range="'Data'!A2:S")
            .execute()
        )
        return {
            row[4].strip()
            for row in response.get("values", [])
            if len(row) >= 6
            and row[5].strip() == month
            and (len(row) <= 18 or row[18].strip() != "Direct Payment Flow")
        }
