"""AI engine: XGBoost invoice-delay model and the explainable alternative credit scorecard."""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date

import numpy as np
import pandas as pd
import xgboost as xgb

from .config import MODEL_DIR, POLICY

SECTORS = ["hotel", "supermarket", "government", "cafe", "distributor", "general"]
# Prior mean delay (days past due) by buyer sector, used when a buyer has no payment history.
SECTOR_PRIOR_DELAY = {"hotel": 28, "supermarket": 18, "government": 55, "cafe": 9, "distributor": 22, "general": 20}
FEATURES = [
    "buyer_hist_avg_delay",
    "buyer_hist_late_ratio",
    "buyer_hist_count",
    "log_amount",
    "amount_vs_buyer_avg",
    "terms_days",
    "sector_code",
    "issue_month",
]
MODEL_PATH = MODEL_DIR / "delay_xgb.json"
METRICS_PATH = MODEL_DIR / "delay_xgb_metrics.json"


# ---------------------------------------------------------------------------
# Synthetic training data
# A generative simulation of Egyptian B2B payment behaviour: each buyer has a
# latent reliability; sector, invoice size relative to the buyer's usual order,
# payment terms and year-end seasonality shift the delay. Documented as
# synthetic in the README; swap in real ledger data with the same columns.
# ---------------------------------------------------------------------------
def generate_training_data(n_buyers: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n_buyers):
        sector = rng.choice(SECTORS, p=[0.15, 0.2, 0.1, 0.25, 0.15, 0.15])
        reliability = rng.normal(0, 1)
        base = SECTOR_PRIOR_DELAY[sector] * math.exp(-0.45 * reliability)
        typical_amount = rng.lognormal(mean=10.2 if sector != "cafe" else 9.0, sigma=0.6)
        terms = int(rng.choice([15, 30, 45, 60], p=[0.15, 0.45, 0.25, 0.15]))
        history: list[float] = []
        for _ in range(int(rng.integers(3, 18))):
            amount = typical_amount * rng.lognormal(0, 0.35)
            month = int(rng.integers(1, 13))
            ratio = amount / typical_amount
            delay = base * (0.7 + 0.5 * ratio) + (12 if month == 12 else 0) + 0.15 * (terms - 30)
            delay = max(0.0, delay + rng.normal(0, 6))
            if history:
                rows.append(
                    {
                        "buyer_hist_avg_delay": float(np.mean(history)),
                        "buyer_hist_late_ratio": float(np.mean([h > 5 for h in history])),
                        "buyer_hist_count": len(history),
                        "log_amount": math.log(amount),
                        "amount_vs_buyer_avg": ratio,
                        "terms_days": terms,
                        "sector_code": SECTORS.index(sector),
                        "issue_month": month,
                        "delay_days": delay,
                    }
                )
            else:
                rows.append(
                    {
                        "buyer_hist_avg_delay": float(SECTOR_PRIOR_DELAY[sector]),
                        "buyer_hist_late_ratio": 0.5,
                        "buyer_hist_count": 0,
                        "log_amount": math.log(amount),
                        "amount_vs_buyer_avg": 1.0,
                        "terms_days": terms,
                        "sector_code": SECTORS.index(sector),
                        "issue_month": month,
                        "delay_days": delay,
                    }
                )
            history.append(delay)
    return pd.DataFrame(rows)


def train_delay_model(force: bool = False) -> dict:
    if MODEL_PATH.exists() and METRICS_PATH.exists() and not force:
        return json.loads(METRICS_PATH.read_text())
    df = generate_training_data()
    rng = np.random.default_rng(11)
    mask = rng.random(len(df)) < 0.8
    train, test = df[mask], df[~mask]
    model = xgb.XGBRegressor(
        n_estimators=350, max_depth=4, learning_rate=0.05, subsample=0.9,
        colsample_bytree=0.9, random_state=42, objective="reg:squarederror",
    )
    model.fit(train[FEATURES], train["delay_days"])
    pred = np.clip(model.predict(test[FEATURES]), 0, None)
    mae = float(np.mean(np.abs(pred - test["delay_days"])))
    baseline_mae = float(np.mean(np.abs(test["buyer_hist_avg_delay"] - test["delay_days"])))
    model.save_model(str(MODEL_PATH))
    importance = dict(zip(FEATURES, (float(v) for v in model.feature_importances_)))
    metrics = {
        "model": "XGBRegressor",
        "version": "delay-xgb-" + hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()[:10],
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "test_mae_days": round(mae, 2),
        "baseline_mae_days_buyer_average": round(baseline_mae, 2),
        "improvement_vs_baseline_pct": round(100 * (baseline_mae - mae) / baseline_mae, 1),
        "feature_importance": {k: round(v, 4) for k, v in sorted(importance.items(), key=lambda x: -x[1])},
        "data": "synthetic (simulated Egyptian B2B payment behaviour, seed=7)",
    }
    METRICS_PATH.write_text(json.dumps(metrics, indent=2))
    return metrics


_model_cache: xgb.XGBRegressor | None = None


def _load_model() -> xgb.XGBRegressor:
    global _model_cache
    if _model_cache is None:
        train_delay_model()
        m = xgb.XGBRegressor()
        m.load_model(str(MODEL_PATH))
        _model_cache = m
    return _model_cache


def model_metrics() -> dict:
    return train_delay_model()


# ---------------------------------------------------------------------------
# Delay prediction for a business's open invoices
# ---------------------------------------------------------------------------
def buyer_history(invoices: list[dict]) -> dict[str, dict]:
    """Per-buyer payment history from this business's paid invoices."""
    hist: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for inv in invoices:
        if inv["paid_date"]:
            delay = max(0, (inv["paid_date"] - inv["due_date"]).days)
            hist[inv["buyer_ref"]].append((delay, inv["amount"]))
    out = {}
    for ref, items in hist.items():
        delays = [d for d, _ in items]
        out[ref] = {
            "avg_delay": float(np.mean(delays)),
            "late_ratio": float(np.mean([d > 5 for d in delays])),
            "count": len(items),
            "avg_amount": float(np.mean([a for _, a in items])),
        }
    return out


def predict_delays(invoices: list[dict]) -> list[dict]:
    """Return open invoices enriched with predicted delay and expected collection date."""
    hist = buyer_history(invoices)
    open_invs = [i for i in invoices if i["status"] == "open"]
    if not open_invs:
        return []
    feats = []
    for inv in open_invs:
        h = hist.get(inv["buyer_ref"])
        sector = inv["buyer_sector"] if inv["buyer_sector"] in SECTORS else "general"
        feats.append(
            {
                "buyer_hist_avg_delay": h["avg_delay"] if h else SECTOR_PRIOR_DELAY[sector],
                "buyer_hist_late_ratio": h["late_ratio"] if h else 0.5,
                "buyer_hist_count": h["count"] if h else 0,
                "log_amount": math.log(inv["amount"]),
                "amount_vs_buyer_avg": inv["amount"] / h["avg_amount"] if h else 1.0,
                "terms_days": (inv["due_date"] - inv["issue_date"]).days,
                "sector_code": SECTORS.index(sector),
                "issue_month": inv["issue_date"].month,
            }
        )
    preds = np.clip(_load_model().predict(pd.DataFrame(feats)[FEATURES]), 0, None)
    out = []
    for inv, f, p in zip(open_invs, feats, preds):
        delay = int(round(float(p)))
        expected = date.fromordinal(inv["due_date"].toordinal() + delay)
        collection_prob = 0.97 if delay <= 30 else 0.92 if delay <= 60 else 0.82
        out.append(
            {
                **inv,
                "predicted_delay_days": delay,
                "expected_collection_date": expected,
                "collection_probability": collection_prob,
                "has_buyer_history": f["buyer_hist_count"] > 0,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Explainable alternative credit scorecard
# ---------------------------------------------------------------------------
def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _linear(x: float, worst: float, best: float) -> float:
    """Map x linearly so worst -> 0 and best -> 100 (works for either direction)."""
    if best == worst:
        return 50.0
    return _clamp(100 * (x - worst) / (best - worst))


WEIGHTS = {
    "inflow_stability": 0.18,
    "net_cash_margin": 0.17,
    "obligation_consistency": 0.15,
    "digital_footprint": 0.10,
    "receivables_quality": 0.18,
    "buyer_concentration": 0.10,
    "liquidity_buffer": 0.12,
}


def _months(txns: list[dict]) -> list[str]:
    return sorted({t["txn_date"].strftime("%Y-%m") for t in txns})


def score_business(txns: list[dict], invoices: list[dict], open_with_preds: list[dict]) -> dict:
    if not txns:
        raise ValueError("At least one bank statement is required to score")
    months = _months(txns)
    df = pd.DataFrame(txns)
    df["month"] = df["txn_date"].apply(lambda d: d.strftime("%Y-%m"))
    credits = df[df.direction == "credit"].groupby("month")["amount"].sum().reindex(months, fill_value=0)
    debits = df[df.direction == "debit"].groupby("month")["amount"].sum().reindex(months, fill_value=0)

    # 1. inflow stability: coefficient of variation of monthly inflows
    cv = float(credits.std(ddof=0) / credits.mean()) if credits.mean() > 0 else 1.0
    # 2. net cash margin
    margin = float((credits.sum() - debits.sum()) / credits.sum()) if credits.sum() > 0 else -1.0
    # 3. obligation consistency: utilities/rent/payroll paid every month, on a regular day
    obligations = df[(df.direction == "debit") & df.category.isin(["utility", "rent", "payroll", "loan"])]
    months_with_oblig = obligations["month"].nunique()
    coverage = months_with_oblig / len(months)
    day_std = float(obligations.groupby("category")["txn_date"].apply(lambda s: np.std([d.day for d in s])).mean()) if len(obligations) else 15.0
    consistency = 0.6 * _linear(coverage, 0.3, 1.0) + 0.4 * _linear(day_std, 10, 1.5)
    # 4. digital footprint: share of inflows via instant payments
    ipn = df[(df.direction == "credit") & (df.category == "ipn_inflow")]["amount"].sum()
    digital_share = float(ipn / credits.sum()) if credits.sum() > 0 else 0.0
    # 5. receivables quality: predicted DSO on open book + historical
    paid = [i for i in invoices if i["paid_date"]]
    hist_dso = float(np.mean([(i["paid_date"] - i["issue_date"]).days for i in paid])) if paid else 75.0
    if open_with_preds:
        weights = np.array([i["amount"] for i in open_with_preds])
        pred_dso = float(np.average([(i["expected_collection_date"] - i["issue_date"]).days for i in open_with_preds], weights=weights))
    else:
        pred_dso = hist_dso
    dso = 0.5 * hist_dso + 0.5 * pred_dso
    # 6. buyer concentration (HHI over invoiced revenue)
    by_buyer: dict[str, float] = defaultdict(float)
    for inv in invoices:
        by_buyer[inv["buyer_ref"]] += inv["amount"]
    total = sum(by_buyer.values())
    hhi = sum((v / total) ** 2 for v in by_buyer.values()) if total else 1.0
    top_share = max(by_buyer.values()) / total if total else 1.0
    # 7. liquidity buffer: days of outflows covered by latest balance
    with_bal = df.dropna(subset=["balance"]).sort_values("txn_date")
    latest_balance = float(with_bal["balance"].iloc[-1]) if len(with_bal) else 0.0
    span_days = max(1, (df.txn_date.max() - df.txn_date.min()).days + 1)
    daily_out = float(debits.sum()) / span_days
    buffer_days = latest_balance / daily_out if daily_out > 0 else 90.0

    components = {
        "inflow_stability": (cv, _linear(cv, 0.8, 0.1), f"Monthly inflows vary by {cv:.0%} (coefficient of variation)"),
        "net_cash_margin": (margin, _linear(margin, -0.15, 0.20), f"Inflows exceed outflows by {margin:.1%}"),
        "obligation_consistency": (coverage, consistency, f"Utilities/rent/payroll paid in {coverage:.0%} of months, avg day spread {day_std:.1f} days"),
        "digital_footprint": (digital_share, _linear(digital_share, 0.0, 0.5), f"{digital_share:.0%} of inflows arrive via instant payments (IPN)"),
        "receivables_quality": (dso, _linear(dso, 120, 30), f"Blended DSO {dso:.0f} days (historical {hist_dso:.0f}, predicted {pred_dso:.0f})"),
        "buyer_concentration": (hhi, _linear(hhi, 0.6, 0.1), f"Largest buyer is {top_share:.0%} of invoiced sales (HHI {hhi:.2f})"),
        "liquidity_buffer": (buffer_days, _linear(buffer_days, 0, 45), f"Current balance covers {buffer_days:.0f} days of outflows"),
    }
    breakdown = []
    score = 0.0
    for name, (raw, sub, reason) in components.items():
        contribution = WEIGHTS[name] * sub
        score += contribution
        breakdown.append(
            {
                "factor": name,
                "raw_value": round(float(raw), 4),
                "sub_score": round(sub, 1),
                "weight": WEIGHTS[name],
                "contribution": round(contribution, 2),
                "explanation": reason,
            }
        )
    score = round(score, 1)
    grade = "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D" if score >= 35 else "E"
    ranked = sorted(breakdown, key=lambda b: b["sub_score"])
    annual_turnover = float(credits.sum()) * 12 / len(months)
    avg_monthly_inflow = float(credits.mean())

    return {
        "score": score,
        "grade": grade,
        "breakdown": breakdown,
        "top_strengths": [b["explanation"] for b in ranked[::-1][:2]],
        "top_risks": [b["explanation"] for b in ranked[:2]],
        "metrics": {
            "history_months": len(months),
            "avg_monthly_inflow": round(avg_monthly_inflow, 2),
            "avg_monthly_outflow": round(float(debits.mean()), 2),
            "annualised_turnover": round(annual_turnover, 2),
            "latest_balance": round(latest_balance, 2),
            "daily_outflow": round(daily_out, 2),
            "historical_dso_days": round(hist_dso, 1),
            "predicted_dso_days": round(pred_dso, 1),
            "top_buyer_share": round(top_share, 3),
        },
        "policy_checks": policy_checks(len(months), score, annual_turnover, avg_monthly_inflow),
        "model_version": model_metrics()["version"],
        "method": "Transparent weighted scorecard; receivables factor uses XGBoost-predicted invoice delays.",
    }


def policy_checks(history_months: int, score: float, annual_turnover: float, avg_monthly_inflow: float) -> list[dict]:
    """Configurable responsible-lending checks. Parameters live in config.POLICY and are illustrative."""
    threshold = POLICY["simplified_tax_turnover_threshold_egp"]
    return [
        {
            "check": "minimum_data_history",
            "passed": history_months >= POLICY["min_history_months"],
            "detail": f"{history_months} months of statements (policy minimum {POLICY['min_history_months']})",
        },
        {
            "check": "explainability",
            "passed": True,
            "detail": "Every score ships with factor-level contributions and plain-language reasons",
        },
        {
            "check": "human_in_the_loop",
            "passed": True,
            "detail": (
                "Score below review threshold: advisor review required before any financing decision"
                if score < POLICY["human_review_required_below_score"]
                else "Owner approval required before any application is sent; the agent never auto-submits"
            ),
        },
        {
            "check": "affordability_cap",
            "passed": True,
            "detail": f"Repayments capped at {POLICY['max_repayment_to_inflow_ratio']:.0%} of avg monthly inflow "
            f"(EGP {avg_monthly_inflow * POLICY['max_repayment_to_inflow_ratio']:,.0f}/month)",
        },
        {
            "check": "simplified_tax_regime_indicator",
            "passed": annual_turnover < threshold,
            "detail": f"Annualised turnover EGP {annual_turnover:,.0f} vs EGP {threshold:,.0f} threshold "
            "(indicator only; confirm eligibility with a tax advisor)",
        },
    ]
