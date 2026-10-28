import threading

import pytest

from app.sheets_service import SheetsService


class _Values:
    def __init__(self, fake):
        self.fake = fake

    def get(self, spreadsheetId, range):
        return self

    def execute(self):
        if self.fake.fail:
            raise RuntimeError("Sheets unavailable")
        return {"values": self.fake.rows}


class _Spreadsheets:
    def __init__(self, fake):
        self.fake = fake

    def values(self):
        return _Values(self.fake)


class _Service:
    def __init__(self, fake):
        self.fake = fake

    def spreadsheets(self):
        return _Spreadsheets(self.fake)


def make_service(rows, fail=False):
    fake = type("Fake", (), {"rows": rows, "fail": fail})
    service = SheetsService.__new__(SheetsService)
    service.sheet_id = "sheet"
    service._config_cache = None
    service._config_lock = threading.Lock()
    service._submission_ids = None
    service._service = _Service(fake)
    return service, fake


def test_config_loads_hierarchy_and_dedupes():
    service, _ = make_service(
        [
            ["Department", "Education", ""],
            ["department", "Education", ""],              # duplicate
            ["Department", "  Sales  ", ""],              # whitespace trimmed
            ["", "", ""],                                  # blank row
            ["Program", "Mindset", "Education"],
            ["Program", "Mindset", "Education"],          # duplicate
            ["Program", "NoParent", ""],                  # missing parent
            ["Program", "Orphan", "Ghost Dept"],          # parent not a department
            ["Project", "VVCE", "Mindset"],
            ["Project", "VVCE", "Mindset"],               # duplicate
            ["ExpenseType", "Travel", ""],
            ["Bogus", "X", ""],                           # unknown category
        ]
    )
    config = service.get_config()
    assert config["departments"] == ["Education", "Sales"]
    assert config["programs"] == {"Education": ["Mindset"], "Ghost Dept": ["Orphan"]}
    assert config["projects"] == {"Mindset": ["VVCE"]}
    assert config["expense_types"] == ["Travel"]


def test_no_hardcoded_fallback_when_sheet_empty():
    service, _ = make_service([])
    config = service.get_config()
    assert config["departments"] == []
    assert config["expense_types"] == []
    assert config["programs"] == {}
    assert config["projects"] == {}


def test_cache_refresh_picks_up_changes():
    service, fake = make_service([["Department", "Education", ""]])
    assert service.get_config()["departments"] == ["Education"]

    fake.rows = [["Department", "New Dept", ""]]
    assert service.get_config()["departments"] == ["Education"]  # cached

    service._config_cache = (service._config_cache[0] - 61, service._config_cache[1])
    assert service.get_config()["departments"] == ["New Dept"]


def test_failed_refresh_keeps_last_valid_config():
    service, fake = make_service([["Department", "Education", ""]])
    assert service.get_config()["departments"] == ["Education"]

    service._config_cache = (service._config_cache[0] - 61, service._config_cache[1])
    fake.fail = True
    assert service.get_config()["departments"] == ["Education"]


def test_first_load_failure_raises():
    service, _ = make_service([], fail=True)
    with pytest.raises(RuntimeError):
        service.get_config()
