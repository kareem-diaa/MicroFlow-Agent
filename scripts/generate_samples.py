"""Generate the synthetic demo dataset for 'Al-Nour Bakery Supplies' (fictional Cairo wholesaler).

Outputs to data/samples/:
  statement_1_apr-jul_2026.csv     bank statement (CSV format)
  statement_2_jul-oct_2026.pdf     bank statement (PDF format)
  invoices_paid_history.xml        paid e-invoices (XML batch)
  invoices_open.json               open e-invoices (JSON)
  invoice_single_open.pdf          one open invoice as a PDF
  informal_receipts.txt            informal Arabic/English records (fallback-parser demo only)

All names are invented. The dataset is deterministic (fixed seed, fixed as-of date 2026-10-19).
"""
from __future__ import annotations

import csv
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "samples"
AS_OF = date(2026, 10, 19)
START = date(2026, 4, 19)
SPLIT = date(2026, 7, 19)
rng = random.Random(2026)

BUYERS = [
    # name, tax id, sector, typical invoice EGP, terms days, mean delay days
    ("Nile View Hotel", "301-552-118", "hotel", 165_000, 45, 34),
    ("GreenBasket Supermarkets", "402-118-930", "supermarket", 120_000, 30, 14),
    ("Downtown Public Hospital Canteen", "100-220-417", "government", 90_000, 60, 58),
    ("Bean & Crumb Cafes", "588-301-662", "cafe", 38_000, 15, 4),
    ("Giza Food Distributors", "219-774-503", "distributor", 105_000, 30, 20),
    ("Corniche Patisserie", "640-115-298", "cafe", 30_000, 15, 6),
    ("Heliopolis Hotel Group", "377-905-214", "hotel", 140_000, 45, 27),
]


def build():
    invoices = []
    payments = []  # (date, description, amount)
    n = 1
    for name, tax, sector, typical, terms, mean_delay in BUYERS:
        d = START - timedelta(days=terms + mean_delay) + timedelta(days=rng.randint(0, 10))
        step = 14 if sector in ("cafe", "supermarket") else 21
        while d < AS_OF - timedelta(days=5):
            amount = round(typical * rng.uniform(0.75, 1.3), -2)
            due = d + timedelta(days=terms)
            delay = max(0, int(rng.gauss(mean_delay, mean_delay * 0.25 + 2)))
            paid = due + timedelta(days=delay)
            number = f"ANB-2026-{n:04d}"
            n += 1
            if paid < AS_OF:
                invoices.append(dict(invoice_number=number, buyer_name=name, buyer_tax_id=tax, buyer_sector=sector,
                                     amount=amount, issue_date=d, due_date=due, paid_date=paid))
                if paid >= START:
                    payments.append((paid, f"PAYMENT FROM {name.upper()} REF {number}", amount))
            else:
                invoices.append(dict(invoice_number=number, buyer_name=name, buyer_tax_id=tax, buyer_sector=sector,
                                     amount=amount, issue_date=d, due_date=due, paid_date=None))
            d += timedelta(days=step + rng.randint(-3, 3))
    # A large seasonal hotel order issued recently: the cash-flow pressure point.
    invoices.append(dict(invoice_number=f"ANB-2026-{n:04d}", buyer_name="Nile View Hotel", buyer_tax_id="301-552-118",
                         buyer_sector="hotel", amount=310_000.0, issue_date=AS_OF - timedelta(days=12),
                         due_date=AS_OF + timedelta(days=33), paid_date=None))
    return invoices, payments


def build_transactions(payments):
    txns = []
    d = START
    while d < AS_OF:
        # retail walk-in and small shop orders via InstaPay
        if d.weekday() != 4:
            txns.append((d, "INSTAPAY IN RETAIL SALES BATCH", 0.0, round(rng.uniform(7_000, 12_500), -1)))
        if d.weekday() in (0, 3):
            txns.append((d, "SUPPLIER PAYMENT NORTH DELTA FLOUR MILLS", round(rng.uniform(95_000, 115_000), -2), 0.0))
        if d.weekday() == 2:
            txns.append((d, "PURCHASE PACKAGING MATERIALS", round(rng.uniform(9_000, 16_000), -2), 0.0))
        if d.day == 1:
            txns.append((d, "WAREHOUSE RENT NASR CITY", 48_000.0, 0.0))
        if d.day == 5:
            txns.append((d, "EQUIPMENT LOAN INSTALLMENT", 36_500.0, 0.0))
        if d.day == 10:
            txns.append((d, "NORTH CAIRO ELECTRICITY CO", round(rng.uniform(16_000, 21_000), -1), 0.0))
            txns.append((d, "WE INTERNET AND TELECOM", 2_450.0, 0.0))
        if d.day == 12:
            txns.append((d, "CAIRO WATER COMPANY", round(rng.uniform(2_400, 3_300), -1), 0.0))
        if d.day == 15:
            txns.append((d, "ETA VAT PAYMENT", round(rng.uniform(26_000, 34_000), -2), 0.0))
        if d.day == 25:
            txns.append((d, "PAYROLL SALARIES 22 STAFF", 248_000.0, 0.0))
        d += timedelta(days=1)
    for pd, desc, amt in payments:
        txns.append((pd, desc, 0.0, amt))
    txns.sort(key=lambda t: (t[0], t[3] == 0))
    balance = 400_000.0
    rows = []
    for d, desc, debit, credit in txns:
        balance += credit - debit
        rows.append((d, desc, debit, credit, round(balance, 2)))
    return rows


def write_csv(rows, path):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "description", "debit", "credit", "balance"])
        for d, desc, debit, credit, bal in rows:
            w.writerow([d.isoformat(), desc, f"{debit:.2f}" if debit else "", f"{credit:.2f}" if credit else "", f"{bal:.2f}"])


def write_statement_pdf(rows, path):
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4

    def header():
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, height - 40, "SANDBOX BANK - Business Current Account Statement")
        c.setFont("Helvetica", 9)
        c.drawString(40, height - 55, "Account holder: Al-Nour Bakery Supplies   Currency: EGP   (synthetic demo data)")
        c.drawString(40, height - 70, "date | description | debit | credit | balance")
        return height - 90

    y = header()
    c.setFont("Courier", 8)
    for d, desc, debit, credit, bal in rows:
        if y < 40:
            c.showPage()
            y = header()
            c.setFont("Courier", 8)
        line = f"{d.isoformat()} | {desc} | {debit:.2f} | {credit:.2f} | {bal:.2f}".replace("| 0.00 |", "|  |")
        c.drawString(40, y, line)
        y -= 11
    c.save()


def write_invoices_xml(invoices, path):
    parts = ["<?xml version='1.0' encoding='UTF-8'?>", "<Invoices>"]
    for inv in invoices:
        parts.append(
            "  <Invoice>"
            f"<InvoiceNumber>{inv['invoice_number']}</InvoiceNumber>"
            f"<IssueDate>{inv['issue_date'].isoformat()}</IssueDate>"
            f"<DueDate>{inv['due_date'].isoformat()}</DueDate>"
            + (f"<PaidDate>{inv['paid_date'].isoformat()}</PaidDate>" if inv["paid_date"] else "")
            + "<Seller><Name>Al-Nour Bakery Supplies</Name><TaxId>512-884-301</TaxId></Seller>"
            f"<Buyer><Name>{escape(inv['buyer_name'])}</Name><TaxId>{inv['buyer_tax_id']}</TaxId><Sector>{inv['buyer_sector']}</Sector></Buyer>"
            f"<Currency>EGP</Currency><TotalAmount>{inv['amount']:.2f}</TotalAmount>"
            "</Invoice>"
        )
    parts.append("</Invoices>")
    path.write_text("\n".join(parts))


def write_invoice_pdf(inv, path):
    c = canvas.Canvas(str(path), pagesize=A4)
    _, h = A4
    lines = [
        "TAX INVOICE (synthetic demo)",
        "Seller: Al-Nour Bakery Supplies   Seller Tax ID: 512-884-301",
        f"Invoice No: {inv['invoice_number']}",
        f"Buyer: {inv['buyer_name']}",
        f"Buyer Tax ID: {inv['buyer_tax_id']}",
        f"Buyer Sector: {inv['buyer_sector']}",
        f"Issue Date: {inv['issue_date'].isoformat()}",
        f"Due Date: {inv['due_date'].isoformat()}",
        f"Total (EGP): {inv['amount']:.2f}",
    ]
    y = h - 60
    for i, line in enumerate(lines):
        c.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 12 if i == 0 else 10)
        c.drawString(50, y, line)
        y -= 20
    c.save()


INFORMAL = """\
14/10/2026 received from Corniche Patisserie 12,500 EGP cash
15/10/2026 paid delivery driver 1,800
16/10/2026 استلمت من مقهى الزمالك 8,200 جنيه
17/10/2026 دفعت صيانة الفرن 4,500 جنيه
"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    invoices, payments = build()
    rows = build_transactions(payments)
    write_csv([r for r in rows if r[0] < SPLIT], OUT / "statement_1_apr-jul_2026.csv")
    write_statement_pdf([r for r in rows if r[0] >= SPLIT], OUT / "statement_2_jul-oct_2026.pdf")
    paid = [i for i in invoices if i["paid_date"]]
    open_ = [i for i in invoices if not i["paid_date"]]
    single = open_.pop()  # the large hotel order is delivered as a PDF invoice
    write_invoices_xml(paid, OUT / "invoices_paid_history.xml")
    (OUT / "invoices_open.json").write_text(json.dumps(open_, default=str, indent=2))
    write_invoice_pdf(single, OUT / "invoice_single_open.pdf")
    (OUT / "informal_receipts.txt").write_text(INFORMAL, encoding="utf-8")
    print(f"paid invoices={len(paid)} open invoices={len(open_) + 1} transactions={len(rows)} "
          f"closing balance={rows[-1][4]:,.0f}", file=sys.stderr)


if __name__ == "__main__":
    main()
