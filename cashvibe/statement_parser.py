"""Bank-independent statement extraction. PDFs stay local; every result is reviewed."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation

from cashvibe.agent import CATEGORIES, suggest_category

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
APPS = ["PhonePe", "Google Pay", "Paytm", "BHIM", "Amazon Pay", "WhatsApp Pay", "CRED", "Navi", "PayZapp", "UPI (app unknown)", "Bank / other"]
PLATFORMS = ["Amazon", "Flipkart", "Zomato", "Swiggy", "Blinkit", "Zepto", "Uber", "Ola", "Rapido", "IRCTC", "Myntra", "Ajio", "Netflix", "Spotify", "Other / unknown"]
_GATE = threading.BoundedSemaphore(1)
DATE = r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/. ](?:\d{1,2}|[A-Za-z]{3,9})[-/. ]\d{2,4})"
DATE_START = re.compile(r"^\s*(" + DATE + r")\b")
MONEY = re.compile(r"(?<![\w@/])(?:₹|Rs\.?|INR)?\s*([(-]?\d[\d,]*\.\d{2}\)?)(?:\s*(CR|DR))?(?!\d)", re.I)


def cents(value, *, allow_negative=False):
    value = str(value).strip().replace(",", "").replace("₹", "")
    value = re.sub(r"(?i)\b(?:INR|Rs\.?|CR|DR)\b", "", value).strip()
    negative = value.startswith("(") and value.endswith(")")
    value = value.strip("()")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount != amount.quantize(Decimal(".01")):
            raise ValueError
        result = int(amount * 100) * (-1 if negative else 1)
        if abs(result) > 100_000_000_000 or (result < 0 and not allow_negative):
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise ValueError("Amounts must be valid numbers with at most two decimal places.") from None


def statement_date(value, day_first=True):
    value = str(value).strip()
    value = re.split(r"\s+\d{1,2}:\d{2}", value)[0]
    formats = ["%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y",
               "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y"]
    if not day_first:
        formats = ["%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y"] + formats
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt).date()
            if 1900 <= parsed.year <= 2100:
                return parsed.isoformat()
        except ValueError:
            continue
    raise ValueError("A transaction date could not be read. Use YYYY-MM-DD in the review table.")


def identify_app(text):
    text = text.casefold()
    rules = [("PhonePe", r"\bphone\s*pe\b"), ("Google Pay", r"\b(?:google\s*pay|gpay)\b"),
             ("Paytm", r"\bpaytm\b"), ("BHIM", r"\bbhim\b"), ("Amazon Pay", r"\bamazon\s*pay\b"),
             ("WhatsApp Pay", r"\bwhatsapp\s*pay\b"), ("CRED", r"\bcred\b"),
             ("Navi", r"\bnavi\b"), ("PayZapp", r"\bpayzapp\b")]
    for name, pattern in rules:
        if re.search(pattern, text):
            return name
    # A PSP handle such as @ybl or @okaxis does not prove which app was used.
    return "UPI (app unknown)" if re.search(r"\bupi\b|@[a-z0-9]+", text) else "Bank / other"


def identify_platform(text):
    text = re.sub(r"(?i)amazon\s*pay", "", text)
    for platform in PLATFORMS[:-1]:
        if re.search(r"\b" + re.escape(platform) + r"\b", text, re.I):
            return platform
    return "Other / unknown"


def reference_from(text):
    match = re.search(r"(?i)\b(?:UTR|RRN|REF(?:ERENCE)?(?:\s*NO)?)[\s:/-]+([A-Z0-9]{10,24})\b", text)
    if match:
        return match.group(1).upper()
    match = re.search(r"(?i)\bUPI[/:-](\d{12})(?:[/:\s-]|$)", text)
    return match.group(1) if match else ""


def enrich(row):
    row = dict(row)
    description = " ".join(str(row.get("description", "")).split())[:200]
    row.update(description=description, category=row.get("category") or suggest_category(description),
               payment_app=row.get("payment_app") or identify_app(description),
               platform=row.get("platform") or identify_platform(description),
               reference=row.get("reference") or reference_from(description))
    row.setdefault("balance_cents", None)
    row.setdefault("review_note", "")
    return row


def row_key(row, occurrence=0):
    # References are only extracted from explicit UTR/RRN/reference/UPI fields.
    data = [row["occurred_on"], row["amount_cents"], row["kind"], row.get("reference", "").upper()]
    if not data[-1]:
        data += [re.sub(r"\s+", " ", row["description"].casefold()).strip(), row.get("balance_cents"), occurrence]
    return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()


def assign_keys(rows):
    counts = {}
    for row in rows:
        base = row_key(row)
        occurrence = counts.get(base, 0)
        counts[base] = occurrence + 1
        row["dedupe_key"] = row_key(row, occurrence)
    return rows


def _header_map(line):
    fields = {
        "date": r"\b(?:transaction\s*date|txn\s*date|posting\s*date|date)\b",
        "description": r"\b(?:narration|description|particulars|transaction\s*details|details|remarks)\b",
        "debit": r"\b(?:withdrawal(?:s)?(?:\s*amt)?|debit(?:\s*amount)?|paid\s*out|money\s*out)\b",
        "credit": r"\b(?:deposit(?:s)?(?:\s*amt)?|credit(?:\s*amount)?|paid\s*in|money\s*in)\b",
        "amount": r"\b(?:transaction\s*amount|amount)\b",
        "balance": r"\b(?:closing\s*balance|running\s*balance|balance)\b",
        "direction": r"\b(?:dr\s*/\s*cr|debit\s*/\s*credit|type)\b",
    }
    result = {}
    for field, pattern in fields.items():
        match = re.search(pattern, line, re.I)
        if match:
            result[field] = match.start()
    if "debit" in result or "credit" in result:
        result.pop("amount", None)
    return result


def parse_text(text):
    """Use aligned statement columns; flag ambiguous rows instead of guessing."""
    rows, warnings, headers = [], [], {}
    pending = []
    previous_balance = None

    def flush(lines):
        nonlocal previous_balance
        if not lines:
            return
        line = lines[0]
        match = DATE_START.match(line)
        if not match:
            return
        try:
            occurred = statement_date(match.group(1))
        except ValueError:
            warnings.append("One transaction had an unreadable date.")
            return
        # Ignore a second value-date column.
        rest_start = match.end()
        second = re.match(r"\s+(" + DATE + r")\b", line[rest_start:])
        if second:
            rest_start += second.end()
        monies = list(MONEY.finditer(line, rest_start))
        if not monies:
            warnings.append(f"{occurred}: a transaction line had no readable amount.")
            return
        found = {}
        numeric_fields = {k: v for k, v in headers.items() if k in ("debit", "credit", "amount", "balance")}
        for item in monies:
            if numeric_fields:
                center = (item.start(1) + item.end(1)) / 2
                field = min(numeric_fields, key=lambda k: abs(center - numeric_fields[k]))
                if field not in found:
                    found[field] = item
        balance = None
        if "balance" in found:
            balance = cents(found["balance"].group(1), allow_negative=True)
            if found["balance"].group(2) and found["balance"].group(2).upper() == "DR":
                balance = -abs(balance)
        amount, kind, note = None, None, ""
        debit = cents(found["debit"].group(1)) if "debit" in found else 0
        credit = cents(found["credit"].group(1)) if "credit" in found else 0
        if bool(debit) != bool(credit):
            amount, kind = (debit, "expense") if debit else (credit, "income")
        else:
            amount_items = [m for m in monies if m is not found.get("balance")]
            if amount_items:
                item = found.get("amount") or amount_items[0]
                amount = abs(cents(item.group(1), allow_negative=True))
                suffix = (item.group(2) or "").upper()
                if suffix:
                    kind = "expense" if suffix == "DR" else "income"
                elif re.search(r"(?i)\b(?:DEBIT|PAID|SENT|WITHDRAWAL)\b", line):
                    kind = "expense"
                elif re.search(r"(?i)\b(?:CREDIT|RECEIVED|DEPOSIT)\b", line):
                    kind = "income"
                elif balance is not None and previous_balance is not None and abs(balance - previous_balance) == amount:
                    kind = "income" if balance > previous_balance else "expense"
                else:
                    kind = "Review required"
                    note = "Direction is uncertain. Select Money out or Money in before saving."
        if debit and credit:
            kind = "Review required"
            note = "Both debit and credit were detected. Check the amount and direction."
        if amount and balance is not None and previous_balance is not None and kind in ("expense", "income"):
            expected = previous_balance + (amount if kind == "income" else -amount)
            if balance != expected:
                note = "Running balance does not reconcile; check amount, direction and statement order."
                warnings.append(f"{occurred}: running balance needs review (reverse-date statements can also cause this).")
        description = line[rest_start:monies[0].start()].strip(" |:-")
        continuation = " ".join(v.strip() for v in lines[1:] if not _header_map(v).get("description"))
        description = (description + " " + continuation).strip()
        if amount and description and not re.search(r"(?i)^(opening|closing|brought forward|carried forward)\b", description):
            rows.append(enrich(dict(occurred_on=occurred, description=description, amount_cents=amount,
                                   kind=kind, balance_cents=balance, review_note=note)))
        if balance is not None:
            previous_balance = balance

    for line in text.splitlines():
        if len(line) > 3000:
            continue
        mapping = _header_map(line)
        if "date" in mapping and ("debit" in mapping or "credit" in mapping or "amount" in mapping):
            flush(pending); pending = []; headers = mapping
            continue
        if re.search(r"(?i)\b(?:opening balance|balance brought forward)\b", line):
            nums = list(MONEY.finditer(line))
            if nums:
                previous_balance = cents(nums[-1].group(1), allow_negative=True)
            continue
        if DATE_START.match(line):
            flush(pending);pending = [line]
        elif pending and line.strip() and not re.search(r"(?i)^\s*(page\s+\d|closing balance|total\b|statement\b|account\b|generated\b)", line):
            pending.append(line)
        if len(rows) > MAX_ROWS:
            raise ValueError("A statement can contain at most 2,000 transactions. Upload a shorter period.")
    flush(pending)
    if len(rows) > MAX_ROWS:
        raise ValueError("A statement can contain at most 2,000 transactions. Upload a shorter period.")
    return assign_keys(rows), warnings


def csv_rows(data, mapping=None, day_first=True):
    if len(data)>MAX_BYTES:
        raise ValueError("Upload a CSV no larger than 5 MB.")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Save the CSV as UTF-8 and upload it again.") from None
    try:
        dialect = csv.Sniffer().sniff(content[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    headers = reader.fieldnames or []
    aliases = {"date": ["date", "transaction date", "txn date", "occurred_on", "posting date"],
               "description": ["description", "narration", "particulars", "remarks", "details"],
               "debit": ["debit", "withdrawal", "withdrawals", "money out", "paid out", "debit amount"],
               "credit": ["credit", "deposit", "deposits", "money in", "paid in", "credit amount"],
               "amount": ["amount", "amount_cents", "transaction amount"],
               "direction": ["direction", "type", "kind", "dr/cr"],
               "balance": ["balance", "running balance", "closing balance"]}
    if mapping is None:
        mapping = {key: next((h for h in headers if h.strip().casefold() in names), "") for key, names in aliases.items()}
    if not mapping.get("date") or not mapping.get("description"):
        raise ValueError("Choose the CSV date and description columns.")
    rows, warnings = [], []
    for number, record in enumerate(reader, 2):
        if number > MAX_ROWS + 1:
            raise ValueError("A statement can contain at most 2,000 rows.")
        get = lambda key: (record.get(mapping.get(key, ""), "") or "").strip()
        if not any(v for v in record.values()):
            continue
        try:
            debit, credit = cents(get("debit")) if get("debit") else 0, cents(get("credit")) if get("credit") else 0
            kind = "Review required"
            if debit and credit:
                raise ValueError("Both debit and credit contain an amount.")
            if debit or credit:
                amount, kind = (debit, "expense") if debit else (credit, "income")
            else:
                raw = get("amount")
                amount = abs(cents(raw, allow_negative=True))
                if mapping.get("amount") == "amount_cents":
                    amount = abs(int(raw))
                direction = get("direction").casefold()
                if direction in ("expense", "debit", "dr", "money out", "paid", "sent") or raw.startswith("-"):
                    kind = "expense"
                elif direction in ("income", "credit", "cr", "money in", "received"):
                    kind = "income"
            if not amount:
                continue
            rows.append(enrich(dict(occurred_on=statement_date(get("date"), day_first), description=get("description"),
                                   amount_cents=amount, kind=kind, balance_cents=cents(get("balance"), allow_negative=True) if get("balance") else None,
                                   review_note="" if kind != "Review required" else "Confirm the transaction direction.")))
        except (ValueError, TypeError):
            warnings.append(f"CSV row {number} could not be converted; correct it before importing.")
    return assign_keys(rows), warnings, headers


def _pdf_worker(data, password):
    import pdfplumber
    all_rows, warnings, used_ocr = [], [], False
    with pdfplumber.open(io.BytesIO(data), password=password or "") as pdf:
        if len(pdf.pages) > 50:
            raise ValueError("Upload at most 50 PDF pages at a time.")
        texts = []
        scanned = 0
        for page in pdf.pages:
            text = page.extract_text(layout=True) or ""
            if len(text.strip()) < 30:
                scanned += 1
                if scanned > 5:
                    raise ValueError("OCR is limited to five scanned pages per upload. Upload a shorter statement.")
                image = page.to_image(resolution=120).original
                buffer = io.BytesIO();image.save(buffer, format="PNG")
                try:
                    result = subprocess.run(["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "6"],
                                            input=buffer.getvalue(), capture_output=True, timeout=15, check=True)
                except (OSError, subprocess.SubprocessError):
                    raise ValueError("The scanned page could not be read. Try a text-based bank PDF or CSV.") from None
                text = result.stdout.decode(errors="replace")
                used_ocr = True
            texts.append(text)
        all_rows, warnings = parse_text("\n".join(texts))
    if used_ocr:
        warnings.insert(0, "OCR was used. Check dates, amounts, and directions carefully before saving.")
    return {"rows": all_rows, "warnings": warnings, "ocr": used_ocr}


def parse_pdf(data, password=""):
    if len(data) > MAX_BYTES or not data.startswith(b"%PDF-"):
        raise ValueError("Upload a valid PDF no larger than 5 MB.")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "SYSTEMROOT")}
    env["PYTHONIOENCODING"] = "utf-8"
    with _GATE:
        try:
            result = subprocess.run([sys.executable, "-m", "cashvibe.statement_parser", "--worker"],
                                    input=json.dumps({"pdf": __import__("base64").b64encode(data).decode(), "password": password}),
                                    capture_output=True, text=True, timeout=75, env=env,
                                    cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
        except subprocess.TimeoutExpired:
            raise ValueError("This statement took too long to read. Upload a shorter period or CSV.") from None
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError("This PDF could not be read within the upload limits. Try a shorter statement or CSV.") from None
    if payload.get("error"):
        raise ValueError(payload["error"])
    if not payload.get("rows"):
        raise ValueError("No transaction rows could be identified. Try CSV column mapping or add corrected rows in the review table.")
    return payload


if __name__ == "__main__" and "--worker" in sys.argv:
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
        except ImportError:
            pass
        payload = json.load(sys.stdin)
        import base64
        result = _pdf_worker(base64.b64decode(payload["pdf"]), payload.get("password", ""))
    except Exception as exc:
        # Never echo source text or PDF passwords in errors.
        if isinstance(exc, ValueError):
            result = {"error": str(exc)}
        elif type(exc).__name__ in ("PDFPasswordIncorrect", "PasswordError"):
            result = {"error": "The PDF password is incorrect. Enter the statement password and try again."}
        else:
            result = {"error": "The PDF could not be read. Check its password, or try a text-based statement/CSV."}
    print(json.dumps(result))
