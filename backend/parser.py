"""Document ingestion: e-invoices (XML / JSON / PDF), bank statements (PDF / CSV) and informal text receipts.

Every parser returns plain dicts; validation and encryption happen in the API layer.
"""
from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

import pymupdf


class ParseError(ValueError):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _to_date(value: str | None) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ParseError(f"Unrecognised date: {value!r}")


def _to_amount(value: str | float | int | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    if cleaned in ("", "-", "."):
        return 0.0
    return float(cleaned)


def detect_kind(filename: str, data: bytes) -> str:
    """Identify the file type by magic bytes first, extension second."""
    head = data[:512].lstrip()
    name = filename.lower()
    if data.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"<"):
        return "xml"
    if head.startswith(b"{") or head.startswith(b"["):
        return "json"
    if name.endswith(".csv"):
        return "csv"
    if name.endswith(".txt"):
        return "text"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError("Unsupported binary file type") from exc
    return "csv" if b"," in data[:200] and b"\n" in data else "text"


# ---------------------------------------------------------------------------
# invoices
# ---------------------------------------------------------------------------
def _normalise_invoice(raw: dict) -> dict:
    required = ["invoice_number", "buyer_name", "buyer_tax_id", "amount", "issue_date", "due_date"]
    missing = [k for k in required if raw.get(k) in (None, "")]
    if missing:
        raise ParseError(f"Invoice missing fields: {', '.join(missing)}")
    issue = _to_date(raw["issue_date"])
    due = _to_date(raw["due_date"])
    paid = _to_date(raw.get("paid_date"))
    amount = _to_amount(raw["amount"])
    if amount <= 0:
        raise ParseError(f"Invoice {raw['invoice_number']}: amount must be positive")
    if due < issue:
        raise ParseError(f"Invoice {raw['invoice_number']}: due date before issue date")
    return {
        "invoice_number": str(raw["invoice_number"]).strip()[:80],
        "buyer_name": str(raw["buyer_name"]).strip()[:200],
        "buyer_tax_id": str(raw["buyer_tax_id"]).strip()[:40],
        "buyer_sector": str(raw.get("buyer_sector") or "general").strip().lower()[:60],
        "amount": round(amount, 2),
        "currency": str(raw.get("currency") or "EGP").upper()[:3],
        "issue_date": issue,
        "due_date": due,
        "paid_date": paid,
    }


def _xml_text(node: ET.Element, path: str) -> str | None:
    found = node.find(path)
    return found.text.strip() if found is not None and found.text else None


def parse_invoice_xml(data: bytes) -> list[dict]:
    text = data.decode("utf-8", errors="strict")
    # Reject DTDs/entities outright: blocks XXE and entity-expansion ("billion laughs") attacks.
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
        raise ParseError("XML with DTD or entity declarations is not accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ParseError(f"Malformed XML: {exc}") from exc
    nodes = [root] if root.tag == "Invoice" else root.findall("Invoice")
    if not nodes:
        raise ParseError("No <Invoice> elements found")
    out = []
    for n in nodes:
        out.append(
            _normalise_invoice(
                {
                    "invoice_number": _xml_text(n, "InvoiceNumber"),
                    "buyer_name": _xml_text(n, "Buyer/Name"),
                    "buyer_tax_id": _xml_text(n, "Buyer/TaxId"),
                    "buyer_sector": _xml_text(n, "Buyer/Sector"),
                    "amount": _xml_text(n, "TotalAmount"),
                    "currency": _xml_text(n, "Currency"),
                    "issue_date": _xml_text(n, "IssueDate"),
                    "due_date": _xml_text(n, "DueDate"),
                    "paid_date": _xml_text(n, "PaidDate"),
                }
            )
        )
    return out


def parse_invoice_json(data: bytes) -> list[dict]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ParseError(f"Malformed JSON: {exc}") from exc
    items = payload if isinstance(payload, list) else payload.get("invoices", [payload])
    return [_normalise_invoice(item) for item in items]


_PDF_INVOICE_FIELDS = {
    "invoice_number": r"Invoice\s*(?:No|Number)\s*[:#]\s*(\S+)",
    "buyer_name": r"Buyer\s*:\s*(.+)",
    "buyer_tax_id": r"Buyer\s*Tax\s*ID\s*:\s*(\S+)",
    "buyer_sector": r"Buyer\s*Sector\s*:\s*(.+)",
    "issue_date": r"Issue\s*Date\s*:\s*(\S+)",
    "due_date": r"Due\s*Date\s*:\s*(\S+)",
    "paid_date": r"Paid\s*Date\s*:\s*(\S+)",
    "amount": r"Total\s*(?:\(EGP\))?\s*:\s*([\d,\.]+)",
}


def parse_invoice_pdf(data: bytes) -> list[dict]:
    text = pdf_text(data)
    raw = {}
    for key, pattern in _PDF_INVOICE_FIELDS.items():
        m = re.search(pattern, text, re.IGNORECASE)
        raw[key] = m.group(1).strip() if m else None
    return [_normalise_invoice(raw)]


def parse_invoices(filename: str, data: bytes) -> list[dict]:
    kind = detect_kind(filename, data)
    if kind == "xml":
        return parse_invoice_xml(data)
    if kind == "json":
        return parse_invoice_json(data)
    if kind == "pdf":
        return parse_invoice_pdf(data)
    raise ParseError(f"Unsupported invoice format: {kind}")


# ---------------------------------------------------------------------------
# bank statements and informal records
# ---------------------------------------------------------------------------
_CATEGORY_RULES = [
    ("ipn_inflow", r"instapay|ipn|meeza|fawry.*in"),
    ("utility", r"electric|water|gas co|natural gas|telecom|internet|vodafone|orange|etisalat|we "),
    ("payroll", r"salar|payroll|wages"),
    ("rent", r"rent"),
    ("tax", r"\btax\b|eta|vat"),
    ("loan", r"loan|installment|instalment"),
    ("supplier", r"supplier|flour|sugar|mill|packaging|purchase"),
    ("customer_payment", r"payment from|transfer from|cheque|deposit"),
]


def categorise(description: str, direction: str) -> str:
    d = description.lower()
    for category, pattern in _CATEGORY_RULES:
        if re.search(pattern, d):
            return category
    return "other_in" if direction == "credit" else "other_out"


def pdf_text(data: bytes) -> str:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # pymupdf raises several internal types
        raise ParseError("Unreadable PDF") from exc
    if doc.page_count > 50:
        raise ParseError("PDF exceeds 50 pages")
    return "\n".join(page.get_text() for page in doc)


_ROW = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s*\|\s*(.+?)\s*\|\s*([\d,\.]*)\s*\|\s*([\d,\.]*)\s*\|\s*([\d,\.\-]+)\s*$"
)


def _txn(d: date, desc: str, debit: float, credit: float, balance: float | None) -> dict:
    direction = "credit" if credit > 0 else "debit"
    amount = credit if credit > 0 else debit
    return {
        "txn_date": d,
        "description": desc.strip()[:200],
        "direction": direction,
        "amount": round(amount, 2),
        "balance": None if balance is None else round(balance, 2),
        "category": categorise(desc, direction),
    }


def parse_statement_pdf(data: bytes) -> list[dict]:
    rows = []
    for line in pdf_text(data).splitlines():
        m = _ROW.match(line.strip())
        if m:
            d, desc, debit, credit, bal = m.groups()
            rows.append(_txn(_to_date(d), desc, _to_amount(debit), _to_amount(credit), _to_amount(bal)))
    if not rows:
        raise ParseError("No statement rows recognised (expected 'date | description | debit | credit | balance')")
    return rows


def parse_statement_csv(data: bytes) -> list[dict]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    needed = {"date", "description", "debit", "credit", "balance"}
    if not reader.fieldnames or not needed.issubset({f.strip().lower() for f in reader.fieldnames}):
        raise ParseError(f"CSV must have columns: {', '.join(sorted(needed))}")
    rows = []
    for r in reader:
        r = {k.strip().lower(): v for k, v in r.items()}
        rows.append(
            _txn(
                _to_date(r["date"]),
                r["description"],
                _to_amount(r["debit"]),
                _to_amount(r["credit"]),
                _to_amount(r["balance"]) if r.get("balance") else None,
            )
        )
    return rows


_INFORMAL = re.compile(
    r"(?P<date>\d{1,2}/\d{1,2}(?:/\d{2,4})?)\D+?(?P<desc>[^\d]*?)\s*(?P<amount>\d[\d,\.]*)\s*(?:egp|le|ج\.?م|جنيه)?",
    re.IGNORECASE,
)
_CREDIT_WORDS = re.compile(r"received|paid me|collected|استلمت|تحصيل|دفع لي", re.IGNORECASE)


def parse_informal_text(data: bytes, year: int | None = None) -> list[dict]:
    """Fallback for informal records (exported chat logs, typed receipts), English or Arabic."""
    year = year or date.today().year
    rows = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        m = _INFORMAL.search(line)
        if not m:
            continue
        parts = m.group("date").split("/")
        day, month = int(parts[0]), int(parts[1])
        yr = int(parts[2]) if len(parts) == 3 else year
        if yr < 100:
            yr += 2000
        try:
            d = date(yr, month, day)
        except ValueError:
            continue
        amount = _to_amount(m.group("amount"))
        if amount <= 0:
            continue
        credit = amount if _CREDIT_WORDS.search(line) else 0.0
        debit = 0.0 if credit else amount
        rows.append(_txn(d, line.strip(), debit, credit, None))
    if not rows:
        raise ParseError("No dated amounts found in text")
    return rows


def parse_statement(filename: str, data: bytes) -> list[dict]:
    kind = detect_kind(filename, data)
    if kind == "pdf":
        return parse_statement_pdf(data)
    if kind == "csv":
        return parse_statement_csv(data)
    if kind == "text":
        return parse_informal_text(data)
    raise ParseError(f"Unsupported statement format: {kind}")
