from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

LOGGER = logging.getLogger(__name__)
NVIDIA_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"


@dataclass
class AICheckResult:
    result: str
    notes: str


@dataclass
class BillExtractionResult:
    result: str
    bill_date: str | None = None
    vendor: str | None = None
    amount: str | None = None
    bill_number: str | None = None
    notes: str = ""


async def check_bill(
    api_key: str | None,
    model: str,
    image: bytes | None,
    mime_type: str,
    details: dict[str, str],
) -> AICheckResult:
    if not api_key:
        return AICheckResult("Skipped", "AI check is enabled but NVIDIA_API_KEY is not configured.")
    if not image or not mime_type.startswith("image/"):
        return AICheckResult("Skipped", "AI check supports image bills; this bill was not an image.")

    instruction = (
        "Inspect the expense bill image and compare it with the entered details. "
        "Return only strict JSON with exactly these keys: extracted_amount (number or null), "
        "extracted_date (string or null), extracted_vendor (string or null), "
        "legible (boolean), matches_amount (boolean), matches_date (boolean), "
        "matches_vendor (boolean), notes (short string). Use false for a match only when "
        "the bill clearly disagrees; use null for a match field if the value cannot be read. "
        "Entered details: "
        + json.dumps(details, ensure_ascii=False)
    )
    data_url = f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}"
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 400,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    try:
        async with asyncio.timeout(60):
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                response = await client.post(
                    NVIDIA_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("AI response did not contain text")
        result = _parse_json_object(content)
        if not isinstance(result.get("legible"), bool):
            raise ValueError("AI response omitted the bill legibility result")
        if not result["legible"]:
            return AICheckResult("Unreadable", _short_note(result, "Bill image is not legible."))

        match_fields = ("matches_amount", "matches_date", "matches_vendor")
        amount_match = amount_equal(result.get("extracted_amount"), details["amount"])
        date_match = _date_equal(result.get("extracted_date"), details["date"])
        if (
            any(result.get(key) is False for key in match_fields)
            or amount_match is False
            or date_match is False
        ):
            return AICheckResult("Mismatch", _mismatch_note(result, details))
        if any(result.get(key) not in (True, None) for key in match_fields):
            raise ValueError("AI response omitted a bill comparison result")
        return AICheckResult("OK", _short_note(result, "Bill appears to match the entered details."))
    except (
        httpx.HTTPError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        LOGGER.warning("AI bill check skipped (%s)", type(exc).__name__)
        return AICheckResult("Skipped", "AI check unavailable; submission was not blocked.")


async def extract_bill_details(
    api_key: str | None, model: str, image: bytes | None, mime_type: str
) -> BillExtractionResult:
    if not api_key or not image or not mime_type.startswith("image/"):
        return BillExtractionResult(
            "Skipped",
            notes="Automatic reading requires a bill photo and a configured NVIDIA API key.",
        )

    instruction = (
        "Read the expense bill image. Do not guess, infer, or invent details. Only transcribe "
        "values clearly visible on the bill. For a blank, non-bill, or blurry image, set legible "
        "to false and return null for every field. Return the final total paid/grand total, not "
        "a subtotal, tax, or individual line item. Return the bill/invoice/receipt number "
        "exactly as printed (letters and digits, no spaces). Return a JSON object with exactly "
        "these keys: bill_date (string in YYYY-MM-DD format or null), vendor (string or null), "
        "amount (number or null), bill_number (string or null), legible (boolean). "
        "Use null for any value that cannot be read."
    )
    data_url = f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}"
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 500,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    try:
        async with asyncio.timeout(60):
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                response = await client.post(
                    NVIDIA_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("AI response did not contain text")
        result = _parse_json_object(content)
        if not isinstance(result.get("legible"), bool):
            raise ValueError("AI response omitted a valid bill legibility result")
        if not result["legible"]:
            return BillExtractionResult(
                "Unreadable", notes="The AI could not clearly read this bill photo."
            )

        bill_date = result.get("bill_date")
        if isinstance(bill_date, str):
            try:
                parsed_date = datetime.strptime(bill_date.strip(), "%Y-%m-%d").date()
                bill_date = parsed_date.isoformat() if parsed_date.isoformat() == bill_date.strip() else None
            except ValueError:
                bill_date = None
        else:
            bill_date = None

        vendor = result.get("vendor")
        vendor = vendor.strip()[:200] if isinstance(vendor, str) and vendor.strip() else None
        bill_number = result.get("bill_number")
        bill_number = (
            bill_number.strip()[:50]
            if isinstance(bill_number, str) and bill_number.strip()
            else None
        )
        extracted_amount = result.get("amount")
        amount = None
        if extracted_amount is not None and not isinstance(extracted_amount, bool):
            try:
                parsed_amount = Decimal(str(extracted_amount))
                if parsed_amount.is_finite() and parsed_amount > 0:
                    formatted_amount = f"{parsed_amount:.2f}"
                    if Decimal(formatted_amount) > 0:
                        amount = formatted_amount
            except (InvalidOperation, ValueError):
                pass
        return BillExtractionResult("OK", bill_date, vendor, amount, bill_number)
    except (
        httpx.HTTPError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        LOGGER.warning("AI bill detail extraction skipped (%s)", type(exc).__name__)
        return BillExtractionResult(
            "Skipped", notes="Automatic reading did not complete. Please enter the details manually."
        )


def compare_extracted_details(
    extraction: BillExtractionResult | None, entered: dict[str, str]
) -> AICheckResult:
    if extraction is None:
        return AICheckResult("Skipped", "Bill details were not extracted by AI.")
    if extraction.result != "OK":
        return AICheckResult(
            extraction.result,
            extraction.notes or "AI extraction was unavailable; details were entered manually.",
        )

    extracted = {
        "extracted_date": extraction.bill_date,
        "extracted_vendor": extraction.vendor,
        "extracted_amount": extraction.amount,
    }
    comparisons = (
        ("date", extraction.bill_date, _date_equal(extraction.bill_date, entered["date"])),
        ("vendor", extraction.vendor, None),
        ("amount", extraction.amount, amount_equal(extraction.amount, entered["amount"])),
    )
    mismatches = []
    for field, value, matches in comparisons:
        if value is None:
            continue
        if field == "vendor":
            matches = value.strip().casefold() == entered["vendor"].strip().casefold()
        if matches is False:
            mismatches.append(field)
    if mismatches:
        extracted.update(
            matches_date=(
                None
                if extraction.bill_date is None
                else _date_equal(extraction.bill_date, entered["date"])
            ),
            matches_vendor=(
                None
                if extraction.vendor is None
                else extraction.vendor.strip().casefold() == entered["vendor"].strip().casefold()
            ),
            matches_amount=(
                None
                if extraction.amount is None
                else amount_equal(extraction.amount, entered["amount"])
            ),
        )
        return AICheckResult("Mismatch", _mismatch_note(extracted, entered))
    if not any(value is not None for value in extracted.values()):
        return AICheckResult(
            "Skipped", "AI could not extract bill details to compare with the confirmed values."
        )
    return AICheckResult("OK", "Confirmed values match the details read from the bill.")


def _parse_json_object(content: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character == "{":
            try:
                result, _ = decoder.raw_decode(cleaned[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(result, dict):
                return result
    raise ValueError("AI response did not contain a JSON object")


def _mismatch_note(result: dict, entered: dict[str, str]) -> str:
    amount = result.get("extracted_amount")
    date = result.get("extracted_date")
    vendor = result.get("extracted_vendor")
    details = []
    if (
        result.get("matches_amount") is False
        or amount_equal(amount, entered["amount"]) is False
    ) and amount is not None:
        details.append(f"bill shows Rs {amount}, entered Rs {entered['amount']}")
    if result.get("matches_date") is False or _date_equal(date, entered["date"]) is False:
        if date:
            details.append(f"bill date appears to be {date}, entered {entered['date']}")
    if result.get("matches_vendor") is False and vendor:
        details.append(f"bill vendor appears to be {vendor}, entered {entered['vendor']}")
    return "Possible mismatch: " + "; ".join(details) + "." if details else "Bill details may not match."


def _short_note(result: dict, fallback: str) -> str:
    note = result.get("notes")
    if isinstance(note, str) and note.strip():
        return note.strip()[:500]
    return fallback[:500]


def amount_equal(extracted: object, entered: str) -> bool | None:
    if extracted is None:
        return None
    try:
        return Decimal(str(extracted)) == Decimal(entered)
    except (InvalidOperation, ValueError):
        return None


def _date_equal(extracted: object, entered: str) -> bool | None:
    if not isinstance(extracted, str) or not extracted.strip():
        return None
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(extracted.strip(), date_format).date().isoformat() == entered
        except ValueError:
            continue
    return None
