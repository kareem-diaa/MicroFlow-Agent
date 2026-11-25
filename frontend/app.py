"""MicroFlow AI dashboard (Streamlit). Talks to the FastAPI backend only through its REST API."""
from __future__ import annotations

import os

import altair as alt
import pandas as pd
import requests
import streamlit as st

API = os.getenv("MICROFLOW_API", "http://localhost:8000")
DEMO_USERS = {
    "SME owner - Al-Nour Bakery Supplies": "owner@alnour.example",
    "Advisor (assigned to Al-Nour)": "advisor@microflow.example",
    "Bank auditor (read-only)": "auditor@bank.example",
    "SME owner - Delta Textiles (other tenant)": "owner@delta.example",
}

st.set_page_config(page_title="MicroFlow AI", page_icon="💧", layout="wide")
st.markdown(
    """
    <style>
      .block-container {padding-top: 1.5rem;}
      .mf-kpi {border:1px solid rgba(128,128,128,.25); border-radius:10px; padding:14px 16px;}
      .mf-kpi h4 {margin:0; font-size:.8rem; opacity:.7; font-weight:500;}
      .mf-kpi p {margin:2px 0 0; font-size:1.5rem; font-weight:650;}
      .sev-critical {border-left:5px solid #d9363e; padding:6px 10px; margin:4px 0; background:rgba(217,54,62,.08);}
      .sev-warning  {border-left:5px solid #e8a33d; padding:6px 10px; margin:4px 0; background:rgba(232,163,61,.08);}
      .sev-info     {border-left:5px solid #3d7be8; padding:6px 10px; margin:4px 0; background:rgba(61,123,232,.06);}
    </style>
    """,
    unsafe_allow_html=True,
)


def call(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    if st.session_state.get("token"):
        headers["Authorization"] = f"Bearer {st.session_state.token}"
    try:
        r = requests.request(method, f"{API}{path}", headers=headers, timeout=60, **kwargs)
    except requests.ConnectionError:
        st.error(f"Cannot reach the API at {API}. Start it with: uvicorn backend.main:app --port 8000")
        st.stop()
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        return None, f"{r.status_code}: {detail}"
    return r.json(), None


def kpi(col, label, value):
    col.markdown(f"<div class='mf-kpi'><h4>{label}</h4><p>{value}</p></div>", unsafe_allow_html=True)


def egp(x) -> str:
    return f"EGP {x:,.0f}" if isinstance(x, (int, float)) else str(x)


# ---------------------------------------------------------------------------
# Sidebar: login
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("💧 MicroFlow AI")
    st.caption("SME Finance Copilot · sandbox demo")
    if "token" not in st.session_state:
        who = st.selectbox("Sign in as", list(DEMO_USERS))
        pw = st.text_input("Password", type="password", value="Demo@2026!")
        if st.button("Sign in", type="primary", width="stretch"):
            data, err = call("POST", "/api/auth/login", json={"email": DEMO_USERS[who], "password": pw})
            if err:
                st.error(err)
            else:
                st.session_state.token = data["access_token"]
                st.rerun()
        st.stop()
    me, err = call("GET", "/api/me")
    if err:
        st.session_state.pop("token", None)
        st.rerun()
    st.success(f"{me['name']}\n\nRole: **{me['role']}**")
    biz_options = {b["name"]: b["id"] for b in me["businesses"]}
    if not biz_options:
        st.warning("No businesses in scope")
        st.stop()
    biz_name = st.selectbox("Business", list(biz_options))
    BID = biz_options[biz_name]
    with st.expander("My permissions"):
        st.write(", ".join(me["permissions"]))
    if st.button("Sign out", width="stretch"):
        st.session_state.clear()
        st.rerun()

perms = set(me["permissions"])
P = {"business_id": BID}
st.header(biz_name)

tabs = st.tabs(["1 · Upload & Ingestion", "2 · Credit Dossier", "3 · Cash-Flow Runway", "4 · Copilot Agent", "5 · Security Audit Vault"])

# ---------------------------------------------------------------------------
# 1. Ingestion
# ---------------------------------------------------------------------------
with tabs[0]:
    if "data:upload" not in perms:
        st.info("Your role cannot upload data (read-only).")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Bank statements")
            st.caption("PDF statement, CSV export, or informal text/chat receipts (English or Arabic)")
            f = st.file_uploader("Statement", type=["pdf", "csv", "txt"], key="stmt")
            if f and st.button("Ingest statement"):
                data, err = call("POST", "/api/upload-statement", params=P, files={"file": (f.name, f.getvalue())})
                st.error(err) if err else st.success(data)
        with c2:
            st.subheader("E-invoices")
            st.caption("XML batch, JSON, or PDF invoice")
            f2 = st.file_uploader("Invoice file", type=["xml", "json", "pdf"], key="inv")
            if f2 and st.button("Ingest invoices"):
                data, err = call("POST", "/api/upload-invoice", params=P, files={"file": (f2.name, f2.getvalue())})
                st.error(err) if err else st.success(data)
        st.divider()
        if st.button("Load bundled demo dataset (synthetic)", type="primary"):
            data, err = call("POST", "/api/demo/load-samples")
            if err:
                st.error(err)
            else:
                st.success(f"Loaded {len(data['files'])} files")
                st.dataframe(pd.DataFrame(data["files"]), width="stretch")
    if "data:read" in perms:
        invs, err = call("GET", "/api/invoices", params=P)
        if invs:
            st.subheader("Invoices (decrypted for your role)")
            df = pd.DataFrame(invs)
            st.dataframe(
                df[["invoice_number", "buyer_name", "buyer_sector", "amount", "issue_date", "due_date", "paid_date", "status",
                    "predicted_delay_days", "expected_collection_date"]],
                width="stretch", hide_index=True,
            )

# ---------------------------------------------------------------------------
# 2. Credit dossier
# ---------------------------------------------------------------------------
with tabs[1]:
    if "credit:score" in perms and st.button("Generate / refresh credit dossier", type="primary"):
        _, err = call("POST", "/api/score-credit", params=P)
        if err:
            st.error(err)
    dossier, err = call("GET", "/api/credit-dossier", params=P)
    if err:
        st.info("No dossier yet. " + ("Click the button above." if "credit:score" in perms else "Ask the owner or advisor to generate one."))
    else:
        c = st.columns(4)
        kpi(c[0], "Alternative credit score", f"{dossier['score']} / 100")
        kpi(c[1], "Grade", dossier["grade"])
        kpi(c[2], "Months of data", dossier["metrics"]["history_months"])
        kpi(c[3], "Annualised turnover", egp(dossier["metrics"]["annualised_turnover"]))
        st.markdown("#### Why this score")
        bd = pd.DataFrame(dossier["breakdown"])
        chart = alt.Chart(bd).mark_bar().encode(
            x=alt.X("contribution:Q", title="Points contributed"),
            y=alt.Y("factor:N", sort="-x", title=None),
            color=alt.Color("sub_score:Q", scale=alt.Scale(scheme="redyellowgreen", domain=[0, 100]), title="Sub-score"),
            tooltip=["factor", "sub_score", "weight", "contribution", "explanation"],
        ).properties(height=260)
        st.altair_chart(chart, width="stretch")
        st.dataframe(bd[["factor", "sub_score", "weight", "contribution", "explanation"]], hide_index=True, width="stretch")
        s1, s2 = st.columns(2)
        s1.markdown("**Strengths**\n\n" + "\n".join(f"- {x}" for x in dossier["top_strengths"]))
        s2.markdown("**Risks**\n\n" + "\n".join(f"- {x}" for x in dossier["top_risks"]))
        st.markdown("#### Policy checks")
        for chk in dossier["policy_checks"]:
            st.markdown(f"{'✅' if chk['passed'] else '⚠️'} **{chk['check']}** — {chk['detail']}")
        st.caption(f"{dossier['method']} Model: {dossier['model_version']}. Generated {dossier['generated_at']}.")

# ---------------------------------------------------------------------------
# 3. Cash-flow runway
# ---------------------------------------------------------------------------
with tabs[2]:
    if "forecast:run" not in perms:
        st.info("Your role cannot run forecasts.")
    else:
        fc, err = call("POST", "/api/forecast-cashflow", params=P)
        if err:
            st.warning(err)
        else:
            c = st.columns(4)
            kpi(c[0], "Starting balance", egp(fc["start_balance"]))
            kpi(c[1], "Lowest point (base)", egp(fc["min_balance_base"]))
            kpi(c[2], "Lowest point (stress)", egp(fc["min_balance_stress"]))
            kpi(c[3], "Funding gap to buffer", egp(fc["funding_gap_base"]))
            rows = []
            for scen, data in fc["scenarios"].items():
                for r in data:
                    rows.append({"week_start": r["week_start"], "scenario": scen, "closing_balance": r["closing_balance"]})
            df = pd.DataFrame(rows)
            line = alt.Chart(df).mark_line(point=True).encode(
                x=alt.X("week_start:T", title="Week"),
                y=alt.Y("closing_balance:Q", title="Closing balance (EGP)"),
                color=alt.Color("scenario:N", scale=alt.Scale(domain=["base", "stress"], range=["#2f6fdf", "#d9363e"])),
                tooltip=["week_start", "scenario", "closing_balance"],
            )
            zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="gray", strokeDash=[4, 4]).encode(y="y")
            buf = alt.Chart(pd.DataFrame({"y": [fc["cash_buffer_target"]]})).mark_rule(color="#e8a33d", strokeDash=[2, 2]).encode(y="y")
            st.altair_chart((line + zero + buf).properties(height=340), width="stretch")
            st.caption("Grey dashed: zero. Orange dotted: cash buffer target.")
            st.markdown("#### Alerts")
            for a in fc["alerts"]:
                st.markdown(f"<div class='sev-{a['severity']}'><b>{a['type']}</b> · {a['message']}</div>", unsafe_allow_html=True)
            with st.expander("Weekly detail (base scenario)"):
                st.dataframe(pd.DataFrame(fc["scenarios"]["base"]), hide_index=True, width="stretch")
            with st.expander("Assumptions"):
                st.markdown("\n".join(f"- {a}" for a in fc["assumptions"]))

# ---------------------------------------------------------------------------
# 4. Copilot agent
# ---------------------------------------------------------------------------
with tabs[3]:
    if "agent:run" in perms and st.button("Run the copilot agent", type="primary"):
        res, err = call("POST", "/api/agent/run", params=P)
        if err:
            st.error(err)
        else:
            st.session_state.agent = res
    res = st.session_state.get("agent")
    if res:
        st.markdown("#### Agent reasoning trace")
        for s in res["steps"]:
            st.markdown(f"**{s['step'].upper()}** — {s['summary']}")
        st.caption(f"Decision hash (in audit log): `{res['decision_sha256']}`")
    if "financing:read" in perms:
        offers, err = call("GET", "/api/financing/offers", params=P)
        if offers:
            st.markdown("#### Financing options compared")
            st.dataframe(
                pd.DataFrame([{k: o.get(k) for k in ("provider", "product", "eligible", "funds_unlocked", "total_cost", "cost_per_1000_egp", "covers_need", "reason")}
                              for o in offers["options"]]),
                hide_index=True, width="stretch",
            )
        apps, err = call("GET", "/api/financing/applications", params=P)
        if apps:
            st.markdown("#### Financing applications")
            for a in apps:
                with st.container(border=True):
                    st.markdown(f"**#{a['id']} · {a['provider']}** · {a['product']} · status `{a['status']}` · created by `{a['created_by']}`")
                    st.write(f"Funds: {egp(a['funds_unlocked'])} · Cost: {egp(a['total_cost'])} · Invoices: {a['invoice_ids']}")
                    if a.get("provider_response"):
                        st.json(a["provider_response"])
                    if a["status"] == "pending_owner_approval" and "financing:approve" in perms:
                        b1, b2, _ = st.columns([1, 1, 4])
                        if b1.button("Approve & submit", key=f"ap{a['id']}", type="primary"):
                            r, e = call("POST", f"/api/financing/applications/{a['id']}/approve")
                            st.error(e) if e else st.rerun()
                        if b2.button("Reject", key=f"rj{a['id']}"):
                            r, e = call("POST", f"/api/financing/applications/{a['id']}/reject")
                            st.error(e) if e else st.rerun()
                    elif a["status"] == "pending_owner_approval":
                        st.caption("Waiting for the business owner's approval.")
    if "agent:ask" in perms:
        st.markdown("#### Ask the copilot")
        q = st.text_input("e.g. When do I run out of cash? · Why is my score low? · Who will pay late? · متى تنفد السيولة؟")
        if q:
            ans, err = call("POST", "/api/agent/ask", json={"question": q, "business_id": BID})
            st.error(err) if err else st.info(ans["answer"])

# ---------------------------------------------------------------------------
# 5. Audit vault
# ---------------------------------------------------------------------------
with tabs[4]:
    if "audit:verify" in perms and st.button("Verify hash chain integrity", type="primary"):
        res, err = call("GET", "/api/audit-logs/verify")
        if err:
            st.error(err)
        elif res["valid"]:
            st.success(f"Chain intact: {res['entries_checked']} entries. Head hash `{res['head_hash'][:24]}…`")
        else:
            st.error(f"Chain broken at entry {res['broken_at_id']}")
    logs, err = call("GET", "/api/audit-logs", params={"limit": 300})
    if err:
        st.info(err)
    elif logs:
        df = pd.DataFrame(logs)
        df["entry_hash"] = df["entry_hash"].str[:16] + "…"
        df["prev_hash"] = df["prev_hash"].str[:16] + "…"
        st.dataframe(df[["id", "ts", "actor", "role", "action", "business_id", "prev_hash", "entry_hash", "details"]],
                     hide_index=True, width="stretch", height=380)
    st.markdown("#### What the database actually stores")
    sample, err = call("GET", "/api/security/at-rest-sample", params=P)
    if sample:
        st.caption("Invoice fields are AES-256-GCM envelopes: a per-value data key wrapped by the master key, bound to the column by AAD.")
        st.json(sample)
    with st.expander("Role permission matrix"):
        st.markdown(
            "| Permission | SME owner | Advisor | Auditor |\n|---|:-:|:-:|:-:|\n"
            "| Upload data | ✅ | | |\n| Read invoices | ✅ | ✅ | |\n| Score credit / forecast / run agent | ✅ | ✅ | |\n"
            "| Read dossier & applications | ✅ | ✅ | ✅ (amounts masked) |\n| Approve financing | ✅ | | |\n"
            "| Audit log | own business | own business | all + chain verify |"
        )
