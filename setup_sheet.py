from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from app.sheets_service import DATA_HEADERS, DEFAULTS

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TAB_NAMES = ["Data", "Users", "Config", "Dashboard"]


def _get_service():
    sheet_id = os.getenv("SHEET_ID")
    raw_credentials = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sheet_id or not raw_credentials:
        raise RuntimeError("Set SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON before running setup.")
    credentials = Credentials.from_service_account_info(
        json.loads(raw_credentials), scopes=SCOPES
    )
    return sheet_id, build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _ensure_tabs(service, sheet_id: str) -> dict[str, int]:
    metadata = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet_ids = {sheet["properties"]["title"]: sheet["properties"]["sheetId"] for sheet in metadata["sheets"]}
    requests = [
        {"addSheet": {"properties": {"title": name}}}
        for name in TAB_NAMES
        if name not in sheet_ids
    ]
    if requests:
        result = service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": requests}
        ).execute()
        for reply in result.get("replies", []):
            properties = reply.get("addSheet", {}).get("properties", {})
            if properties:
                sheet_ids[properties["title"]] = properties["sheetId"]
    return sheet_ids


def _update_values(service, sheet_id: str, cell_range: str, values: list[list[object]]) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=cell_range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def _dashboard_values() -> list[list[object]]:
    rows: list[list[object]] = [
        ["Staff Reimbursement Dashboard"],
        [
            "Month filter (YYYY-MM)",
            datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m"),
        ],
        [],
        ["Total this month", "Pending amount", "Approved amount", "Paid amount", "Submissions"],
        [
            '=IFERROR(SUMIF(Data!F2:F,$B$2,Data!L2:L),0)',
            '=IFERROR(SUMIFS(Data!L2:L,Data!F2:F,$B$2,Data!Q2:Q,"Pending"),0)',
            '=IFERROR(SUMIFS(Data!L2:L,Data!F2:F,$B$2,Data!Q2:Q,"Approved"),0)',
            '=IFERROR(SUMIFS(Data!L2:L,Data!F2:F,$B$2,Data!Q2:Q,"Paid"),0)',
            '=COUNTIF(Data!F2:F,$B$2)',
        ],
        [],
        [],
        ["Spend by Department"],
        [
            '=IFERROR(QUERY(Data!A2:R,"select G,sum(L) where F = \'"&$B$2&"\' and G is not null group by G label G \'Department\',sum(L) \'Spend\'",0),"")'
        ],
        [],
        [],
        ["", "", "", "Spend by Project"],
        ["", "", "",
            '=IFERROR(QUERY(Data!A2:R,"select M,sum(L) where F = \'"&$B$2&"\' and M is not null group by M label M \'Project\',sum(L) \'Spend\'",0),"")'
        ],
        [],
        [],
        ["", "", "", "", "", "", "Spend by Expense Type"],
        ["", "", "", "", "", "",
            '=IFERROR(QUERY(Data!A2:R,"select H,sum(L) where F = \'"&$B$2&"\' and H is not null group by H label H \'Expense Type\',sum(L) \'Spend\'",0),"")'
        ],
        [],
        [],
        ["", "", "", "", "", "", "", "", "", "Spend by Month"],
        ["", "", "", "", "", "", "", "", "",
            '=IFERROR(QUERY(Data!A2:R,"select F,sum(L) where F is not null group by F order by F label F \'Month\',sum(L) \'Spend\'",0),"")'
        ],
    ]
    while len(rows) < 24:
        rows.append([])
    rows.extend(
        [
            ["Top 10 Vendors"],
            [
                '=IFERROR(QUERY(Data!A2:R,"select K,sum(L) where F = \'"&$B$2&"\' and K is not null group by K order by sum(L) desc limit 10 label K \'Vendor\',sum(L) \'Spend\'",0),"")'
            ],
            [],
            [],
            ["", "", "", "Spend per Person"],
            ["", "", "",
                '=IFERROR(QUERY(Data!A2:R,"select D,sum(L) where F = \'"&$B$2&"\' and D is not null group by D order by sum(L) desc label D \'Name\',sum(L) \'Spend\'",0),"")'
            ],
            [],
            [],
            ["", "", "", "", "", "", "Pending Approvals (oldest first)"],
            ["", "", "", "", "", "",
                '=IFERROR(QUERY(Data!A2:R,"select A,D,F,G,K,L,Q where Q = \'Pending\' order by A asc label A \'Timestamp\',D \'Name\',F \'Month\',G \'Department\',K \'Vendor\',L \'Amount\',Q \'Status\'",0),"")'
            ],
        ]
    )
    return rows


def _formatting_requests(sheet_ids: dict[str, int]) -> list[dict]:
    data_id = sheet_ids["Data"]
    dashboard_id = sheet_ids["Dashboard"]
    users_id = sheet_ids["Users"]
    requests = [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": data_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "updateCells": {
                "range": {
                    "sheetId": data_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(DATA_HEADERS),
                },
                "rows": [{"values": [{"userEnteredFormat": {"textFormat": {"bold": True}}} for _ in DATA_HEADERS]}],
                "fields": "userEnteredFormat.textFormat.bold",
            }
        },
        {
            "setDataValidation": {
                "range": {
                    "sheetId": data_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 16,
                    "endColumnIndex": 17,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [
                            {"userEnteredValue": value}
                            for value in ("Pending", "Approved", "Paid", "Rejected")
                        ],
                    },
                    "showCustomUi": True,
                    "strict": True,
                },
            }
        },
    ]
    colors = {
        "Pending": {"red": 1.0, "green": 0.95, "blue": 0.75},
        "Approved": {"red": 0.75, "green": 0.9, "blue": 0.75},
        "Paid": {"red": 0.7, "green": 0.85, "blue": 1.0},
        "Rejected": {"red": 1.0, "green": 0.78, "blue": 0.78},
    }
    for status, color in colors.items():
        requests.append(
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": data_id,
                                "startRowIndex": 1,
                                "startColumnIndex": 16,
                                "endColumnIndex": 17,
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": status}],
                            },
                            "format": {"backgroundColor": color},
                        },
                    },
                    "index": 0,
                }
            }
        )

    for sheet_id in (dashboard_id, users_id):
        requests.append(
            {
                "addProtectedRange": {
                    "protectedRange": {
                        "range": {"sheetId": sheet_id},
                        "description": "Warning protection to prevent accidental edits",
                        "warningOnly": True,
                    }
                }
            }
        )

    chart_ranges = [
        (8, 0, 2, 40, 0, "Spend by Department", "COLUMN"),
        (12, 3, 5, 40, 7, "Spend by Project", "BAR"),
        (16, 6, 8, 58, 0, "Spend by Expense Type", "BAR"),
        (20, 9, 11, 58, 7, "Spend by Month", "LINE"),
        (25, 0, 2, 76, 0, "Top 10 Vendors", "BAR"),
        (29, 3, 5, 76, 7, "Spend per Person", "BAR"),
    ]
    for header_row, start_col, end_col, anchor_row, anchor_col, title, chart_type in chart_ranges:
        requests.append(
            {
                "addChart": {
                    "chart": {
                        "spec": {
                            "title": title,
                            "basicChart": {
                                "chartType": chart_type,
                                "legendPosition": "NO_LEGEND",
                                "headerCount": 1,
                                "domains": [
                                    {
                                        "domain": {
                                            "sourceRange": {
                                                "sources": [
                                                    {
                                                        "sheetId": dashboard_id,
                                                        "startRowIndex": header_row,
                                                        "endRowIndex": header_row + 100,
                                                        "startColumnIndex": start_col,
                                                        "endColumnIndex": start_col + 1,
                                                    }
                                                ]
                                            }
                                        }
                                    }
                                ],
                                "series": [
                                    {
                                        "series": {
                                            "sourceRange": {
                                                "sources": [
                                                    {
                                                        "sheetId": dashboard_id,
                                                        "startRowIndex": header_row,
                                                        "endRowIndex": header_row + 100,
                                                        "startColumnIndex": start_col + 1,
                                                        "endColumnIndex": end_col,
                                                    }
                                                ]
                                            }
                                        },
                                        "targetAxis": "LEFT_AXIS",
                                    }
                                ],
                            },
                        },
                        "position": {
                            "overlayPosition": {
                                "anchorCell": {
                                    "sheetId": dashboard_id,
                                    "rowIndex": anchor_row,
                                    "columnIndex": anchor_col,
                                },
                                "widthPixels": 640,
                                "heightPixels": 360,
                            }
                        },
                    }
                }
            }
        )
    return requests


def main() -> None:
    sheet_id, service = _get_service()
    sheet_ids = _ensure_tabs(service, sheet_id)

    _update_values(service, sheet_id, "'Data'!A1", [DATA_HEADERS])
    _update_values(service, sheet_id, "'Users'!A1", [["telegram_id", "name", "email", "default_department", "active"]])
    config_rows = [["Category", "Value"]]
    for category, values in DEFAULTS.items():
        config_rows.extend([[category, value] for value in values])
    _update_values(service, sheet_id, "'Config'!A1", config_rows)
    _update_values(service, sheet_id, "'Dashboard'!A1", _dashboard_values())
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": _formatting_requests(sheet_ids)},
    ).execute()
    print("Sheet setup complete: Data, Users, Config, and Dashboard are ready.")
    print("Add staff in Users: telegram_id, name, email, default_department, active (TRUE/FALSE).")


if __name__ == "__main__":
    main()
