"""Copilot: cash-flow forecast with alerts, financing matcher, the agent loop and natural-language Q&A."""
from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from .config import POLICY

MONTHLY_CATEGORIES = {"payroll", "rent", "utility", "loan", "tax"}
WEEKLY_OUT_CATEGORIES = {"supplier", "other_out"}
WEEKLY_IN_CATEGORIES = {"ipn_inflow", "other_in"}


# ---------------------------------------------------------------------------
# Cash-flow forecast
# ---------------------------------------------------------------------------
def _recurring_profile(txns: list[dict]) -> dict:
    months = sorted({t["txn_date"].strftime("%Y-%m") for t in txns})
    n_months = max(1, len(months))
    span_weeks = max(1.0, ((max(t["txn_date"] for t in txns) - min(t["txn_date"] for t in txns)).days + 1) / 7)

    monthly: dict[str, dict] = {}
    by_cat_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_cat_days: dict[str, list[int]] = defaultdict(list)
    weekly_out = 0.0
    weekly_in = 0.0
    collections = 0.0
    for t in txns:
        if t["direction"] == "debit" and t["category"] in MONTHLY_CATEGORIES:
            by_cat_month[t["category"]][t["txn_date"].strftime("%Y-%m")] += t["amount"]
            by_cat_days[t["category"]].append(t["txn_date"].day)
        elif t["direction"] == "debit":
            weekly_out += t["amount"]
        elif t["category"] in WEEKLY_IN_CATEGORIES:
            weekly_in += t["amount"]
        else:
            collections += t["amount"]
    for cat, per_month in by_cat_month.items():
        values = [per_month.get(m, 0.0) for m in months]
        monthly[cat] = {"amount": median(values), "day": int(median(by_cat_days[cat]))}
    return {
        "monthly": monthly,
        "weekly_out": weekly_out / span_weeks,
        "weekly_in": weekly_in / span_weeks,
        "weekly_collections": collections / span_weeks,
        "n_months": n_months,
    }


def forecast_cashflow(
    txns: list[dict],
    open_with_preds: list[dict],
    start_balance: float,
    as_of: date,
    historical_dso: float,
    weeks: int | None = None,
) -> dict:
    weeks = weeks or POLICY["forecast_weeks"]
    prof = _recurring_profile(txns)
    scenarios = {
        "base": {"extra_delay": 0, "collect_mult": 1.0, "sales_mult": 1.0},
        "stress": {"extra_delay": 21, "collect_mult": 0.85, "sales_mult": 0.9},
    }
    # Collections from sales not yet invoiced start after the typical DSO.
    new_sales_start = as_of + timedelta(days=int(historical_dso))
    results = {}
    for name, sc in scenarios.items():
        balance = start_balance
        rows = []
        for w in range(weeks):
            ws = as_of + timedelta(days=7 * w)
            we = ws + timedelta(days=6)
            inflow_invoices = 0.0
            for inv in open_with_preds:
                # Predicted dates already in the past mean "overdue": assume the soonest plausible week.
                exp = max(inv["expected_collection_date"], as_of + timedelta(days=7)) + timedelta(days=sc["extra_delay"])
                if ws <= exp <= we:
                    inflow_invoices += inv["amount"] * inv["collection_probability"] * sc["collect_mult"]
            inflow_other = prof["weekly_in"] * sc["sales_mult"]
            inflow_new_sales = prof["weekly_collections"] * sc["sales_mult"] if we >= new_sales_start else 0.0
            outflow_fixed = 0.0
            d = ws
            while d <= we:
                for cat, info in prof["monthly"].items():
                    if d.day == min(info["day"], 28):
                        outflow_fixed += info["amount"]
                d += timedelta(days=1)
            outflow_var = prof["weekly_out"]
            inflow = inflow_invoices + inflow_other + inflow_new_sales
            outflow = outflow_fixed + outflow_var
            balance += inflow - outflow
            rows.append(
                {
                    "week": w + 1,
                    "week_start": ws.isoformat(),
                    "inflow_open_invoices": round(inflow_invoices, 2),
                    "inflow_instant_payments": round(inflow_other, 2),
                    "inflow_future_sales": round(inflow_new_sales, 2),
                    "outflow_fixed": round(outflow_fixed, 2),
                    "outflow_variable": round(outflow_var, 2),
                    "net": round(inflow - outflow, 2),
                    "closing_balance": round(balance, 2),
                }
            )
        results[name] = rows

    daily_out = (prof["weekly_out"] + sum(v["amount"] for v in prof["monthly"].values()) * 12 / 52) / 7
    buffer = daily_out * POLICY["low_cash_buffer_days"]
    alerts = build_alerts(results, open_with_preds, buffer, as_of)
    base_min = min(r["closing_balance"] for r in results["base"])
    gap = max(0.0, buffer - base_min)
    return {
        "as_of": as_of.isoformat(),
        "start_balance": round(start_balance, 2),
        "weeks": weeks,
        "scenarios": results,
        "cash_buffer_target": round(buffer, 2),
        "funding_gap_base": round(gap, 2),
        "min_balance_base": round(base_min, 2),
        "min_balance_stress": round(min(r["closing_balance"] for r in results["stress"]), 2),
        "alerts": alerts,
        "assumptions": [
            "Open invoices collected at XGBoost-predicted date x collection probability",
            "Fixed costs (payroll, rent, utilities, loans, tax) repeat monthly on their usual day",
            "Variable purchases and instant-payment inflows continue at historical weekly averages",
            f"Collections from new invoices begin after historical DSO ({historical_dso:.0f} days)",
            "Stress: +21 days delay, 85% collection, 90% sales",
        ],
    }


def build_alerts(results: dict, open_with_preds: list[dict], buffer: float, as_of: date) -> list[dict]:
    alerts = []
    for scenario, severity in (("base", "critical"), ("stress", "warning")):
        neg = next((r for r in results[scenario] if r["closing_balance"] < 0), None)
        if neg:
            alerts.append(
                {
                    "severity": severity,
                    "type": "insolvency_risk",
                    "scenario": scenario,
                    "message": f"[{scenario}] Balance turns negative in week {neg['week']} (from {neg['week_start']}): "
                    f"EGP {neg['closing_balance']:,.0f}",
                }
            )
    low = next((r for r in results["base"] if 0 <= r["closing_balance"] < buffer), None)
    if low:
        alerts.append(
            {
                "severity": "warning",
                "type": "low_cash_buffer",
                "scenario": "base",
                "message": f"Balance drops below the {POLICY['low_cash_buffer_days']}-day buffer "
                f"(EGP {buffer:,.0f}) in week {low['week']}",
            }
        )
    total_open = sum(i["amount"] for i in open_with_preds)
    by_buyer: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}
    for inv in open_with_preds:
        by_buyer[inv["buyer_ref"]] += inv["amount"]
        names[inv["buyer_ref"]] = inv["buyer_name"]
    if total_open:
        ref, amt = max(by_buyer.items(), key=lambda kv: kv[1])
        if amt / total_open > 0.35:
            alerts.append(
                {
                    "severity": "warning",
                    "type": "buyer_concentration",
                    "scenario": "base",
                    "message": f"{names[ref]} holds {amt / total_open:.0%} of open receivables",
                }
            )
    for inv in open_with_preds:
        if inv["due_date"] < as_of:
            alerts.append(
                {
                    "severity": "info",
                    "type": "overdue_invoice",
                    "scenario": "base",
                    "message": f"Invoice {inv['invoice_number']} ({inv['buyer_name']}) is "
                    f"{(as_of - inv['due_date']).days} days overdue; predicted to arrive {inv['expected_collection_date'].isoformat()}",
                }
            )
    return alerts


# ---------------------------------------------------------------------------
# Financing offers (sandbox providers; names are fictional)
# ---------------------------------------------------------------------------
OFFERS = [
    {
        "offer_id": "NF-FACT-01",
        "provider": "NileFactor (sandbox NBFI)",
        "product": "invoice_factoring",
        "min_score": 45,
        "advance_rate": 0.85,
        "fee_per_30d": 0.022,
        "max_predicted_delay_days": 75,
        "min_invoice": 20_000,
        "recourse": "with recourse",
    },
    {
        "offer_id": "DF-DISC-02",
        "provider": "Delta Receivables Finance (sandbox NBFI)",
        "product": "invoice_discounting",
        "min_score": 60,
        "advance_rate": 0.90,
        "fee_per_30d": 0.018,
        "max_predicted_delay_days": 45,
        "min_invoice": 50_000,
        "recourse": "with recourse",
    },
    {
        "offer_id": "CM-WCL-03",
        "provider": "Cairo Micro Lending (sandbox)",
        "product": "working_capital_loan",
        "min_score": 50,
        "annual_rate": 0.32,
        "tenor_months": 6,
        "max_amount_per_score_point": 12_000,
    },
]


def match_financing(
    open_with_preds: list[dict], score: float, need: float, avg_monthly_inflow: float, as_of: date
) -> dict:
    options = []
    for offer in OFFERS:
        if score < offer["min_score"]:
            options.append({**_offer_public(offer), "eligible": False, "reason": f"Requires score >= {offer['min_score']}"})
            continue
        if offer["product"] in ("invoice_factoring", "invoice_discounting"):
            candidates = []
            for inv in open_with_preds:
                if inv.get("status") != "open" or inv["amount"] < offer["min_invoice"]:
                    continue
                if inv["predicted_delay_days"] > offer["max_predicted_delay_days"]:
                    continue
                days = max(15, (inv["expected_collection_date"] - as_of).days)
                advance = inv["amount"] * offer["advance_rate"]
                fee = inv["amount"] * offer["fee_per_30d"] * math.ceil(days / 30)
                candidates.append(
                    {
                        "invoice_id": inv["id"],
                        "invoice_number": inv["invoice_number"],
                        "buyer_name": inv["buyer_name"],
                        "amount": inv["amount"],
                        "advance": round(advance, 2),
                        "fee": round(fee, 2),
                        "days_to_collection": days,
                        "effective_apr": round(fee / advance * 365 / days, 4),
                    }
                )
            candidates.sort(key=lambda c: c["effective_apr"])
            chosen, covered, cost = [], 0.0, 0.0
            for c in candidates:
                if covered >= need and chosen:
                    break
                chosen.append(c)
                covered += c["advance"]
                cost += c["fee"]
            if not chosen:
                options.append({**_offer_public(offer), "eligible": False, "reason": "No invoices meet this provider's criteria"})
                continue
            options.append(
                {
                    **_offer_public(offer),
                    "eligible": True,
                    "invoices": chosen,
                    "funds_unlocked": round(covered, 2),
                    "total_cost": round(cost, 2),
                    "covers_need": covered >= need,
                    "cost_per_1000_egp": round(1000 * cost / covered, 2),
                }
            )
        else:
            cap_score = score * offer["max_amount_per_score_point"]
            affordable_monthly = avg_monthly_inflow * POLICY["max_repayment_to_inflow_ratio"]
            r = offer["annual_rate"] / 12
            n = offer["tenor_months"]
            cap_afford = affordable_monthly * (1 - (1 + r) ** -n) / r
            amount = min(max(need, 0), cap_score, cap_afford)
            if amount <= 0:
                options.append({**_offer_public(offer), "eligible": False, "reason": "No funding need or affordability headroom"})
                continue
            payment = amount * r / (1 - (1 + r) ** -n)
            cost = payment * n - amount
            options.append(
                {
                    **_offer_public(offer),
                    "eligible": True,
                    "funds_unlocked": round(amount, 2),
                    "monthly_payment": round(payment, 2),
                    "total_cost": round(cost, 2),
                    "covers_need": amount >= need,
                    "cost_per_1000_egp": round(1000 * cost / amount, 2),
                    "affordability_cap": round(cap_afford, 2),
                }
            )
    eligible = [o for o in options if o["eligible"]]
    eligible.sort(key=lambda o: (not o["covers_need"], o["cost_per_1000_egp"]))
    return {"need": round(need, 2), "options": options, "recommended": eligible[0] if eligible else None}


def _offer_public(offer: dict) -> dict:
    return {k: offer[k] for k in ("offer_id", "provider", "product") if k in offer}


def sandbox_provider_decision(application: dict, score: float) -> dict:
    """Deterministic simulated provider response used for execution evidence."""
    offer = next(o for o in OFFERS if o["offer_id"] == application["offer_id"])
    approved = score >= offer["min_score"]
    ref = "SBX-" + hashlib.sha256(f"{application['offer_id']}{sorted(i['invoice_id'] for i in application.get('invoices', []))}{score}".encode()).hexdigest()[:10].upper()
    return {
        "provider": offer["provider"],
        "reference": ref,
        "decision": "approved" if approved else "declined",
        "amount": application["funds_unlocked"] if approved else 0,
        "disbursement": "T+2 business days (sandbox)" if approved else None,
        "note": "Simulated response from the MicroFlow sandbox; no real funds move.",
    }


# ---------------------------------------------------------------------------
# Agent loop: observe -> analyse -> decide -> act (with guardrails)
# ---------------------------------------------------------------------------
def agent_plan(forecast: dict, credit: dict, financing: dict) -> dict:
    steps = [
        {
            "step": "observe",
            "summary": f"Balance EGP {forecast['start_balance']:,.0f}; {len(forecast['alerts'])} alerts raised over {forecast['weeks']} weeks",
        },
        {
            "step": "analyse",
            "summary": f"Credit score {credit['score']} ({credit['grade']}); funding gap EGP {forecast['funding_gap_base']:,.0f} "
            f"(min base balance EGP {forecast['min_balance_base']:,.0f}, stress EGP {forecast['min_balance_stress']:,.0f})",
        },
    ]
    actions = []
    rec = financing.get("recommended")
    if forecast["funding_gap_base"] <= 0:
        steps.append({"step": "decide", "summary": "No funding gap: keep monitoring, no financing drafted"})
    elif rec is None:
        steps.append({"step": "decide", "summary": "Gap found but no eligible financing; escalate to advisor"})
        actions.append({"type": "escalate_to_advisor", "reason": "funding gap without eligible offer"})
    elif credit["score"] < POLICY["min_score_for_auto_draft"]:
        steps.append({"step": "decide", "summary": "Score below auto-draft threshold; advisor review required"})
        actions.append({"type": "escalate_to_advisor", "reason": "score below auto-draft threshold"})
    else:
        steps.append(
            {
                "step": "decide",
                "summary": f"Cheapest option covering the gap: {rec['provider']} ({rec['product']}), "
                f"EGP {rec['funds_unlocked']:,.0f} for EGP {rec['total_cost']:,.0f} cost",
            }
        )
        actions.append({"type": "draft_financing_application", "offer": rec})
        if credit["score"] < POLICY["human_review_required_below_score"]:
            actions.append({"type": "request_advisor_review", "reason": "score below review threshold"})
    actions.append({"type": "publish_alerts", "count": len(forecast["alerts"])})
    steps.append({"step": "act", "summary": ", ".join(a["type"] for a in actions)})
    steps.append({"step": "guardrail", "summary": "Drafts wait for owner approval; nothing is submitted automatically"})
    return {"steps": steps, "actions": actions}


# ---------------------------------------------------------------------------
# Natural-language Q&A over the business's own data (English + Arabic keywords)
# ---------------------------------------------------------------------------
_INTENTS = [
    ("runway", r"runway|cash|run out|balance|insolven|سيولة|رصيد|فلوس"),
    ("score", r"score|credit|rating|why|grade|تقييم|ائتمان"),
    ("late", r"late|overdue|delay|who owes|متأخر|تأخير"),
    ("financing", r"financ|factor|loan|fund|borrow|تمويل|قرض"),
    ("tax", r"tax|law 6|simplified|ضريب"),
]


def answer_question(question: str, forecast: dict, credit: dict, financing: dict, open_with_preds: list[dict]) -> dict:
    q = question.lower()
    intent = next((name for name, pat in _INTENTS if re.search(pat, q)), "help")
    if intent == "runway":
        neg = next((r for r in forecast["scenarios"]["base"] if r["closing_balance"] < 0), None)
        text = (
            f"You start at EGP {forecast['start_balance']:,.0f}. "
            + (
                f"In the base case cash runs out in week {neg['week']} (from {neg['week_start']})."
                if neg
                else f"In the base case you stay positive for {forecast['weeks']} weeks; the lowest point is EGP {forecast['min_balance_base']:,.0f}."
            )
            + f" Under stress the lowest point is EGP {forecast['min_balance_stress']:,.0f}."
        )
    elif intent == "score":
        text = (
            f"Your score is {credit['score']}/100 (grade {credit['grade']}). "
            f"Strengths: {'; '.join(credit['top_strengths'])}. "
            f"Biggest drags: {'; '.join(credit['top_risks'])}."
        )
    elif intent == "late":
        late = sorted(open_with_preds, key=lambda i: -i["predicted_delay_days"])[:3]
        text = "Most likely to pay late: " + "; ".join(
            f"{i['buyer_name']} invoice {i['invoice_number']} (EGP {i['amount']:,.0f}), ~{i['predicted_delay_days']} days past due"
            for i in late
        ) if late else "No open invoices."
    elif intent == "financing":
        rec = financing.get("recommended")
        text = (
            f"Best option: {rec['provider']} {rec['product'].replace('_', ' ')}, unlocking EGP {rec['funds_unlocked']:,.0f} "
            f"for EGP {rec['total_cost']:,.0f} (EGP {rec['cost_per_1000_egp']:,.0f} per 1,000). It needs your approval before it is sent."
            if rec
            else "No eligible financing right now; an advisor review is recommended."
        )
    elif intent == "tax":
        chk = next(c for c in credit["policy_checks"] if c["check"] == "simplified_tax_regime_indicator")
        text = chk["detail"] + "."
    else:
        text = "Ask me about your cash runway, credit score, late payers, financing options or the simplified tax threshold."
    return {"intent": intent, "answer": text}
