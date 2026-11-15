"""Small, shared presentation and money helpers for CashVibe."""
from __future__ import annotations

import calendar
import html
import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

CATEGORIES = ["Food & drinks", "Transport", "Rent & utilities", "Education", "Shopping", "Entertainment", "Health", "Other"]
PALETTE = ["#39d9ab", "#8676ff", "#ffc56d", "#69b5ff", "#ef8dae", "#91df82", "#d6a8ff", "#8aa5ad"]
SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$"}
CURRENCY = os.getenv("CASHVIBE_CURRENCY", "INR").upper()
SYMBOL = SYMBOLS.get(CURRENCY, CURRENCY + " ")


def today() -> date:
    return datetime.now(ZoneInfo(os.getenv("CASHVIBE_TIMEZONE", "Asia/Kolkata"))).date()


def money(cents: int, signed: bool = False) -> str:
    sign = "−" if cents < 0 else "+" if signed and cents > 0 else ""
    return f"{sign}{SYMBOL}{abs(cents) / 100:,.2f}"


def to_cents(value) -> int:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("10000000"):
            raise ValueError("Enter an amount between 0.01 and 10,000,000.")
        if amount != amount.quantize(Decimal("0.01")):
            raise ValueError("Use at most two decimal places.")
        return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError("Enter a valid amount.") from exc


def equal_split(amount_cents: int, participants: list[str]) -> dict[str, int]:
    if not participants:
        raise ValueError("Choose at least one participant.")
    base, remainder = divmod(amount_cents, len(participants))
    return {uid: base + (i < remainder) for i, uid in enumerate(participants)}


def percentage_split(amount_cents: int, percentages: dict[str, float]) -> dict[str, int]:
    values = {k: Decimal(str(v)) for k, v in percentages.items()}
    if not values or any(not v.is_finite() or v < 0 for v in values.values()) or sum(values.values()) != Decimal("100"):
        raise ValueError("Participant percentages must add up to exactly 100%.")
    raw = {k: Decimal(amount_cents) * v / 100 for k, v in values.items()}
    result = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - result[k], reverse=True)[:amount_cents - sum(result.values())]:
        result[k] += 1
    return result


def ledger(store, uid: str, month: str | None = None) -> list[dict]:
    personal = [dict(row, source="Personal") for row in store.list_transactions(uid, month)]
    shared = [dict(row, source=row.get("group_name", "Shared"), kind="expense") for row in store.list_shared_shares(uid, month)]
    return sorted(personal + shared, key=lambda row: (row["occurred_on"], row.get("id", "")), reverse=True)


def totals(rows: list[dict]) -> dict:
    income = sum(row["amount_cents"] for row in rows if row["kind"] == "income")
    spending = sum(row["amount_cents"] for row in rows if row["kind"] == "expense")
    categories = {cat: sum(row["amount_cents"] for row in rows if row["kind"] == "expense" and row["category"] == cat) for cat in CATEGORIES}
    return {"income": income, "spending": spending, "balance": income - spending, "categories": categories}


def month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def days_in_month(month: str) -> int:
    year, mon = map(int, month.split("-"))
    return calendar.monthrange(year, mon)[1]


def metric(label: str, value: str, detail: str, color: str = "mint"):
    st.markdown(f'<div class="cv-metric {color}"><div class="cv-metric-label">{html.escape(label)}</div><div class="cv-metric-value">{html.escape(value)}</div><div class="cv-metric-detail">{html.escape(detail)}</div></div>', unsafe_allow_html=True)


def section(kicker: str, title: str, subtitle: str = ""):
    st.markdown(f'<div class="cv-kicker">{html.escape(kicker)}</div><h1>{html.escape(title)}</h1>', unsafe_allow_html=True)
    if subtitle:
        st.markdown(f'<p class="cv-subtitle">{html.escape(subtitle)}</p>', unsafe_allow_html=True)


def empty(title: str, body: str):
    st.markdown(f'<div class="cv-empty"><div class="cv-empty-symbol">✦</div><h3>{html.escape(title)}</h3><p>{html.escape(body)}</p></div>', unsafe_allow_html=True)


def chart_style(fig, height=300):
    fig.update_layout(template="plotly_dark", height=height, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(family="Inter, sans-serif", color="#c8d4dd", size=12), margin=dict(l=8, r=8, t=12, b=8), legend=dict(orientation="h", y=-0.2), hoverlabel=dict(bgcolor="#182a30"))
    fig.update_xaxes(gridcolor="#24343b", zeroline=False)
    fig.update_yaxes(gridcolor="#24343b", zeroline=False, tickprefix=SYMBOL)
    return fig


def category_chart(categories: dict):
    nonzero = {k: v for k, v in categories.items() if v > 0}
    if not nonzero:
        empty("Your spending story starts here", "Add an expense to see where your money goes.")
        return
    fig = go.Figure(go.Pie(labels=list(nonzero), values=[v / 100 for v in nonzero.values()], hole=.73, marker=dict(colors=PALETTE), textinfo="none", hovertemplate=f"%{{label}}<br>{SYMBOL}%{{value:,.2f}} · %{{percent}}<extra></extra>"))
    fig.update_layout(annotations=[dict(text="SPENDING", x=.5, y=.56, font=dict(size=11, color="#91a5ae"), showarrow=False), dict(text=money(sum(nonzero.values())), x=.5, y=.43, font=dict(size=21, color="#f0faf7"), showarrow=False)])
    st.plotly_chart(chart_style(fig, 320), width="stretch", config={"displayModeBar": False})


def transaction_frame(rows: list[dict]) -> pd.DataFrame:
    columns = ["Date", "Description", "Category", "Type", f"Amount ({CURRENCY})", "Source"]
    return pd.DataFrame([{"Date": r["occurred_on"], "Description": r["description"], "Category": r["category"], "Type": r["kind"].title(), f"Amount ({CURRENCY})": r["amount_cents"] / 100, "Source": r.get("source", "Personal")} for r in rows], columns=columns)


def export_csv(rows: list[dict]) -> bytes:
    frame = transaction_frame(rows)
    # Preserve numeric amounts; treat user-entered text as text in spreadsheets.
    def safe(value):
        if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))):
            return "'" + value
        return value
    for column in ("Description", "Category", "Source"):
        frame[column] = frame[column].map(safe)
    return frame.to_csv(index=False).encode("utf-8-sig")


def debt_plan(balances: list[dict]) -> list[dict]:
    debtors = [[r["user_id"], r["name"], -r["balance_cents"]] for r in balances if r["balance_cents"] < 0]
    creditors = [[r["user_id"], r["name"], r["balance_cents"]] for r in balances if r["balance_cents"] > 0]
    plan = []
    for debtor in debtors:
        for creditor in creditors:
            amount = min(debtor[2], creditor[2])
            if amount:
                plan.append({"from_id": debtor[0], "from": debtor[1], "to_id": creditor[0], "to": creditor[1], "amount_cents": amount})
                debtor[2] -= amount
                creditor[2] -= amount
    return plan


def inject_style():
    st.markdown('''<style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap');
    :root {--mint:#45e3b2;--muted:#8fa5b0;--panel:#122129;}
    html,body,.stApp,.stApp input,.stApp textarea,.stApp p {font-family:'DM Sans',sans-serif;}
    .stApp {background:radial-gradient(ellipse at 88% 0%,#12362e66,transparent 40%),#0b141b;color:#e8f3f0;}
    .block-container {max-width:1420px;padding-top:2.2rem;padding-bottom:4rem;}
    h1,h2,h3,.cv-metric-value {font-family:'Space Grotesk',sans-serif!important;letter-spacing:-.04em;}
    h1 {font-size:2.5rem!important;line-height:1.12!important;margin:.2rem 0 .55rem!important;}
    h2 {font-size:1.5rem!important;}h3{font-size:1.15rem!important;}
    .cv-kicker {font-size:.7rem;letter-spacing:.19em;color:#58e4b8;font-weight:700;text-transform:uppercase;}
    .cv-subtitle {color:#95aab5;font-size:.97rem;max-width:800px;margin-bottom:1.9rem;}
    .cv-brand {font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:1.9rem;letter-spacing:-.06em;}
    .cv-brand span {color:#45e3b2;} .cv-mark{display:inline-flex;align-items:center;justify-content:center;background:#45e3b2;color:#09201b;border-radius:13px;width:42px;height:42px;margin-right:9px;font-size:1.4rem;vertical-align:middle;}
    .cv-brand .cv-mark {color:#09201b;}
    .cv-tag {color:#8fa5b0;font-size:.7rem;letter-spacing:.16em;margin:8px 0 26px;text-transform:uppercase;}
    section[data-testid="stSidebar"] {background:#0d1920;border-right:1px solid #203039;}
    section[data-testid="stSidebar"] .block-container {padding-top:2rem;}
    section[data-testid="stSidebar"] [data-testid="stRadio"] label {padding:6px 0;}
    section[data-testid="stSidebar"] [data-testid="stRadio"] p {font-size:.95rem;}
    .cv-metric {background:linear-gradient(140deg,#16262e,#101e26);border:1px solid #2b3b43;border-radius:18px;padding:23px 20px;min-height:148px;margin-bottom:12px;}
    .cv-metric-label {color:#a5b6be;font-size:.78rem;margin-bottom:15px;}
    .cv-metric-value {font-size:1.9rem;font-weight:600;line-height:1.1;overflow-wrap:anywhere;}
    .cv-metric-detail {font-size:.72rem;color:#8fa5b0;margin-top:13px;}
    .cv-metric.mint {border-color:#2b5d50;background:linear-gradient(135deg,#17372f,#13262a);}.mint .cv-metric-value{color:#65edc0;}
    .cv-metric.violet .cv-metric-value{color:#b1a6ff;}.cv-metric.amber .cv-metric-value{color:#ffd08b;}
    .cv-empty {padding:30px 22px;text-align:center;border:1px dashed #34505a;border-radius:17px;margin:10px 0;color:#93abb6;}
    .cv-empty-symbol {font-size:1.5rem;color:#45e3b2;margin-bottom:12px;}.cv-empty h3 {color:#e5f1ed;}.cv-empty p {font-size:.88rem;line-height:1.7;}
    .cv-pill {display:inline-block;padding:6px 11px;background:#16372e;color:#77ebc1;border:1px solid #285445;border-radius:20px;font-size:.7rem;letter-spacing:.05em;}
    .cv-hero {padding:32px 48px 32px 0;}.cv-hero h1 {font-size:3.8rem!important;line-height:1.05!important;margin:27px 0!important;max-width:590px;}.cv-hero h1 span {color:#54e4b4;}.cv-hero p {font-size:1.03rem;color:#9bb0b9;line-height:1.8;max-width:530px;}
    .cv-feature {border-top:1px solid #2b3b43;padding:15px 0;font-size:.88rem;color:#b8c9cf;}.cv-feature b {color:#66e9bd;margin-right:10px;}
    .cv-notice {padding:17px 20px;border-radius:14px;background:#172932;border:1px solid #2c414c;color:#b1c4cd;font-size:.86rem;line-height:1.7;}
    .cv-sync {font-size:.72rem;color:#80bdad;margin:14px 0;}
    [data-testid="stVerticalBlockBorderWrapper"]>div {border-radius:17px!important;}
    [data-testid="stForm"] {border-color:#2d444e!important;background:#10202966;border-radius:17px!important;}
    .stButton>button,.stDownloadButton>button,.stLinkButton>a {border-radius:10px;font-weight:600;min-height:2.55rem;}
    .stButton>button[kind="primary"],.stFormSubmitButton>button[kind="primary"] {color:#07271e;background:#48e3b1;border:0;}
    [data-testid="stTabs"] button {font-size:.9rem;}.stCaption {color:#8fa5b0;}
    .stChatMessage {border:1px solid #27404a;background:#10222b;border-radius:15px;}
    footer {visibility:hidden;}#MainMenu {visibility:hidden;}[data-testid="stAppDeployButton"] {display:none;}
    @media(max-width:750px){.block-container{padding:1.2rem 1rem 3rem}.cv-hero{padding:10px 0}.cv-hero h1{font-size:2.7rem!important}h1{font-size:2rem!important}.cv-metric{padding:18px 16px;min-height:130px}.cv-metric-value{font-size:1.5rem}}
    </style>''', unsafe_allow_html=True)
