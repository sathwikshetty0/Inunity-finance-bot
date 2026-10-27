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
        if self._config_cache and now - self._config_cache[0] < 60:
            return self._config_cache[1]

        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range="'Config'!A2:C")
            .execute()
        )
        values: dict[str, Any] = {
            "departments": list(DEFAULTS["departments"]),
            "expense_types": list(DEFAULTS["expense_types"]),
            "programs": {
                department: list(programs)
                for department, programs in DEFAULT_PROGRAMS.items()
            },
            "projects": {
                program: list(projects)
                for program, projects in DEFAULT_PROJECTS.items()
            },
        }
        found_departments: list[str] = []
        found_expense_types: list[str] = []
        found_programs: list[tuple[str, str]] = []
        found_projects: list[tuple[str, str]] = []
        for row in response.get("values", []):
            if len(row) >= 2:
                category, value = row[0].strip().lower(), row[1].strip()
                parent = row[2].strip() if len(row) >= 3 else ""
                if category in {"department", "departments"} and value:
                    found_departments.append(value)
                elif category in {"expense_type", "expense_types"} and value:
                    found_expense_types.append(value)
                elif category in {"program", "programs"} and value and parent:
                    found_programs.append((parent, value))
                elif category in {"project", "projects"} and value and parent:
                    found_projects.append((parent, value))

        if found_departments:
            values["departments"] = found_departments
        if found_expense_types:
            values["expense_types"] = found_expense_types
        if found_programs:
            values["programs"] = {}
            for department, program in found_programs:
                values["programs"].setdefault(department, []).append(program)
        if found_projects:
            values["projects"] = {}
            for program, project in found_projects:
                values["projects"].setdefault(program, []).append(project)
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
