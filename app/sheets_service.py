from __future__ import annotations

import time
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

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
]

DEFAULTS = {
    "departments": [
        "Education-Institutions",
        "Marketing & Sales",
        "Community Engagement/Start Up Studio",
        "Tech Platform",
        "HR",
        "Finance",
        "Admin",
    ],
    "expense_types": [
        "Travelling Expense",
        "Accommodation Expense",
        "Printing Expense",
        "Purchases",
        "Courier Charges",
        "Office Expenses",
        "Employee Benefits/HR Cost",
        "Technology Cost",
        "Branding & Marketing",
        "Other",
    ],
    "projects": [
        "VVCE Mysore",
        "SKIT Bangalore",
        "KSIT Bangalore",
        "Chanakya University Bangalore",
        "PES College Mandya",
        "GMIT Mandya",
        "SSSE Tumkur",
        "DIM- TITAN",
        "UDAL",
        "BOLPU",
        "Nagpur Next",
        "Sristi MAHE",
        "Employee Benefits/HR Cost",
        "Inpulse(Tech)",
        "Acharya Institute",
        "Beary's(BIT)",
        "YIT",
        "Sales/Business Development",
        "CIVIC",
        "Short term Program",
        "Community Engagement",
        "Other",
    ],
}


class SheetsService:
    def __init__(self, service_account_info: dict[str, Any], sheet_id: str) -> None:
        credentials = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
        self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self.sheet_id = sheet_id
        self._config_cache: tuple[float, dict[str, list[str]]] | None = None
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

    def get_config(self) -> dict[str, list[str]]:
        now = time.monotonic()
        if self._config_cache and now - self._config_cache[0] < 600:
            return self._config_cache[1]

        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range="'Config'!A2:B")
            .execute()
        )
        values = {key: list(items) for key, items in DEFAULTS.items()}
        found: dict[str, list[str]] = {key: [] for key in DEFAULTS}
        for row in response.get("values", []):
            if len(row) >= 2:
                category, value = row[0].strip().lower(), row[1].strip()
                if category in found and value:
                    found[category].append(value)
        for category, items in found.items():
            if items:
                values[category] = items
        self._config_cache = (now, values)
        return values

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
                range="'Data'!A:R",
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
            .get(spreadsheetId=self.sheet_id, range="'Data'!A2:R")
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
            .get(spreadsheetId=self.sheet_id, range="'Data'!A2:R")
            .execute()
        )
        rows = [
            row
            for row in response.get("values", [])
            if len(row) >= 17 and row[4].strip() == str(telegram_id)
        ]
        return list(reversed(rows[-limit:]))

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
            .get(spreadsheetId=self.sheet_id, range="'Data'!A2:R")
            .execute()
        )
        return {
            row[4].strip()
            for row in response.get("values", [])
            if len(row) >= 6 and row[5].strip() == month
        }
