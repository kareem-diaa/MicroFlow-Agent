from pathlib import Path

import pytest

from backend import ai_engine, parser

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


def test_parse_xml_batch():
    invs = parser.parse_invoices("x.xml", (SAMPLES / "invoices_paid_history.xml").read_bytes())
    assert len(invs) > 50
    assert all(i["paid_date"] for i in invs)


def test_parse_json_and_pdf_invoice():
    invs = parser.parse_invoices("o.json", (SAMPLES / "invoices_open.json").read_bytes())
    assert invs and all(i["paid_date"] is None for i in invs)
    pdf = parser.parse_invoices("p.pdf", (SAMPLES / "invoice_single_open.pdf").read_bytes())
    assert pdf[0]["amount"] == 310000.0
    assert pdf[0]["buyer_sector"] == "hotel"


def test_parse_statements_pdf_and_csv():
    pdf = parser.parse_statement("s.pdf", (SAMPLES / "statement_2_jul-oct_2026.pdf").read_bytes())
    csv = parser.parse_statement("s.csv", (SAMPLES / "statement_1_apr-jul_2026.csv").read_bytes())
    assert len(pdf) > 100 and len(csv) > 100
    cats = {r["category"] for r in pdf + csv}
    assert {"ipn_inflow", "payroll", "utility", "supplier", "customer_payment"} <= cats


def test_informal_arabic_and_english_text():
    rows = parser.parse_statement("r.txt", (SAMPLES / "informal_receipts.txt").read_bytes())
    assert len(rows) == 4
    assert [r["direction"] for r in rows] == ["credit", "debit", "credit", "debit"]
    assert rows[2]["amount"] == 8200


def test_xxe_and_entity_expansion_rejected():
    payload = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><Invoice>&e;</Invoice>'
    with pytest.raises(parser.ParseError):
        parser.parse_invoices("evil.xml", payload)


def test_invalid_invoice_rejected():
    bad = b'[{"invoice_number":"X","buyer_name":"B","buyer_tax_id":"1","amount":-5,"issue_date":"2026-01-01","due_date":"2026-01-10"}]'
    with pytest.raises(parser.ParseError):
        parser.parse_invoices("bad.json", bad)


def test_delay_model_beats_naive_baseline():
    m = ai_engine.model_metrics()
    assert m["test_mae_days"] < m["baseline_mae_days_buyer_average"]
