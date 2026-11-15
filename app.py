"""CashVibe — a multi-user student expense tracker built with Streamlit."""
from __future__ import annotations

import csv
import html
import io
import logging
import os
import uuid
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

# Streamlit Community Cloud stores secrets separately from environment variables.
for _key in ("DATABASE_URL", "REQUIRE_POSTGRES", "GROQ_API_KEY", "GROQ_MODEL", "OLLAMA_BASE_URL", "OLLAMA_MODEL", "CASHVIBE_CURRENCY", "CASHVIBE_TIMEZONE", "CASHVIBE_SYNC_SECONDS"):
    if not os.getenv(_key):
        try:
            if _key in st.secrets:
                os.environ[_key] = str(st.secrets[_key])
        except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
            pass

from cashvibe.store import Store
from cashvibe.agent import Agent
from cashvibe.ui import (CATEGORIES, CURRENCY, SYMBOL, PALETTE, today, money, to_cents, equal_split,
                        percentage_split, ledger, totals, month_label, days_in_month, metric,
                        section, empty, category_chart, chart_style, transaction_frame, export_csv, debt_plan, inject_style)

st.set_page_config(page_title="CashVibe · Your money, in rhythm", page_icon="💸", layout="wide", initial_sidebar_state="auto")
inject_style()


@st.cache_resource
def get_store():
    if os.getenv("RENDER", "").lower() == "true" and not os.getenv("DATABASE_URL"):
        raise ValueError("Set DATABASE_URL in Render to a persistent PostgreSQL database before opening CashVibe.")
    return Store()


def error_message(exc):
    if isinstance(exc, (ValueError, PermissionError)):
        st.error(str(exc))
    else:
        logging.getLogger("cashvibe").error("Operation failed (%s)", type(exc).__name__)
        st.error("We couldn't save that change. Your records are safe; please try again.")


try:
    store = get_store()
except Exception as exc:
    section("CASHVIBE", "Let's connect your database", "CashVibe needs durable storage to keep every participant in sync.")
    error_message(exc)
    st.info("For hosted deployment, set DATABASE_URL in your hosting dashboard. For local Docker use, keep the data volume mounted. See README.md for the setup steps.")
    st.stop()


def flash(message):
    st.session_state["flash"] = message


def logout():
    for key in list(st.session_state):
        del st.session_state[key]
    st.rerun()


def show_recovery():
    if st.session_state.get("recovery_code"):
        st.success("Your recovery key is ready. Save it somewhere private before continuing.")
        st.caption("This key can reset your password. It is shown once; CashVibe cannot retrieve it later.")
        st.code(st.session_state["recovery_code"], language=None)
        st.download_button("Download recovery key", f"CashVibe recovery key\nAccount: {st.session_state.get('recovery_email', '')}\nKey: {st.session_state['recovery_code']}\nKeep private. A successful recovery replaces this key.\n", file_name="cashvibe-recovery-key.txt", mime="text/plain", key="download_recovery")
        if st.checkbox("I saved my recovery key", key="saved_recovery") and st.button("Continue to CashVibe", type="primary", key="continue_recovery"):
            st.session_state.pop("recovery_code", None)
            st.session_state.pop("recovery_email", None)
            st.rerun()
        return True
    return False


def authentication():
    left, right = st.columns([1.12, 1], gap="large")
    with left:
        st.markdown('<div class="cv-brand"><span class="cv-mark"><svg width="23" height="23" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 19L19 5M5 5h14v14" stroke="#09201b" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/></svg></span>Cash<span>Vibe</span></div>', unsafe_allow_html=True)
        st.markdown('''<div class="cv-hero"><span class="cv-pill">BUILT FOR STUDENT LIFE</span><h1>Less money stress.<br>More <span>good vibes.</span></h1><p>Your allowance, hostel bills, late-night food runs, and next big goal. One clear picture, with a little help from SpendGenie.</p><div class="cv-feature"><b>01</b> Track your money. Find your rhythm.</div><div class="cv-feature"><b>02</b> Split with friends. Keep everyone in sync.</div><div class="cv-feature"><b>03</b> Make a plan. Get grounded insights.</div></div>''', unsafe_allow_html=True)
        st.caption(f"A shared ledger. Your own private space. All amounts in {CURRENCY}.")
    with right:
        st.write("")
        if show_recovery():
            return
        st.subheader("Your next good money habit")
        st.caption("Sign in or create your own CashVibe account.")
        login_tab, signup_tab, recovery_tab = st.tabs(["Sign in", "Create account", "Recover access"])
        with login_tab:
            with st.form("login_form"):
                email = st.text_input("Email", placeholder="you@college.edu", key="login_email")
                password = st.text_input("Password", type="password", key="login_password")
                submitted = st.form_submit_button("Sign in →", type="primary", width="stretch")
            if submitted:
                try:
                    user = store.login(email, password)
                    st.session_state["user"] = user
                    st.session_state.pop("login_password", None)
                    st.rerun()
                except (ValueError, PermissionError, SQLAlchemyError) as exc:
                    error_message(exc)
        with signup_tab:
            with st.form("signup_form"):
                name = st.text_input("Your name", max_chars=60, placeholder="What should we call you?")
                email = st.text_input("Email", key="signup_email", placeholder="you@college.edu")
                password = st.text_input("Password", type="password", key="signup_password", help="Use at least 10 characters. A longer passphrase works well.")
                confirm = st.text_input("Confirm password", type="password", key="signup_confirm")
                accepted = st.checkbox("I understand my records are private and group expenses are visible to group members.")
                submitted = st.form_submit_button("Create my account →", type="primary", width="stretch")
            if submitted:
                try:
                    if password != confirm:
                        raise ValueError("The passwords don't match.")
                    if not accepted:
                        raise ValueError("Please acknowledge who can see your records.")
                    result = store.register(name, email, password)
                    st.session_state["user"] = result["user"]
                    st.session_state["recovery_code"] = result["recovery_code"]
                    st.session_state["recovery_email"] = result["user"]["email"]
                    st.rerun()
                except (ValueError, PermissionError, SQLAlchemyError) as exc:
                    error_message(exc)
        with recovery_tab:
            st.caption("Use the private recovery key you saved when creating your account.")
            with st.form("recovery_form"):
                email = st.text_input("Account email", key="recover_email")
                recovery = st.text_input("Recovery key", type="password")
                password = st.text_input("New password", type="password", help="At least 10 characters.")
                confirm = st.text_input("Confirm new password", type="password")
                submitted = st.form_submit_button("Reset password", width="stretch")
            if submitted:
                try:
                    if password != confirm:
                        raise ValueError("The passwords don't match.")
                    code = store.recover(email, recovery, password)
                    st.session_state["recovery_code"] = code
                    st.session_state["recovery_email"] = email
                    st.rerun()
                except (ValueError, PermissionError, SQLAlchemyError) as exc:
                    error_message(exc)


if "user" not in st.session_state:
    authentication()
    st.stop()

if show_recovery():
    st.stop()

user = st.session_state["user"]
uid = user["id"]


@st.dialog("Your transaction", width="large")
def transaction_dialog(dialog_key, record=None):
    validate_session()
    record = record or {}
    editing = bool(record)
    with st.form(f"transaction_editor_{dialog_key}"):
        kind = st.radio("Type", ["expense", "income"], index=0 if record.get("kind", "expense") == "expense" else 1, format_func=str.title, horizontal=True)
        description = st.text_input("Description", value=record.get("description", ""), max_chars=150, placeholder="Lunch, allowance, metro top-up…")
        a, b = st.columns(2)
        amount = a.number_input(f"Amount ({CURRENCY})", min_value=.01, max_value=10000000.0, value=record.get("amount_cents", 10000) / 100, step=10.0, format="%.2f")
        when = b.date_input("Date", value=date.fromisoformat(record["occurred_on"]) if record else today(), max_value=today())
        a, b = st.columns(2)
        category = a.selectbox("Category", CATEGORIES, index=CATEGORIES.index(record.get("category", "Other")))
        methods = ["UPI", "Cash", "Card", "Bank transfer", "Other"]
        payment = b.selectbox("Payment method", methods, index=methods.index(record.get("payment_method", "UPI")) if record.get("payment_method", "UPI") in methods else 4)
        notes = st.text_area("Notes (optional)", value=record.get("notes", ""), max_chars=1000)
        save = st.form_submit_button("Save changes" if editing else "Add transaction", type="primary", width="stretch")
    if save:
        try:
            fields = dict(description=description, amount_cents=to_cents(amount), category=category, occurred_on=when.isoformat(), kind=kind, payment_method=payment, notes=notes)
            if editing:
                store.update_transaction(uid, record["id"], record["version"], **fields)
            else:
                store.create_transaction(uid, **fields)
            flash("Transaction updated." if editing else "Transaction added. You're all caught up.")
            st.rerun()
        except (ValueError, PermissionError, SQLAlchemyError) as exc:
            error_message(exc)


@st.dialog("Delete transaction")
def delete_transaction_dialog(record):
    validate_session()
    st.write(f"Delete **{record['description']}** · {money(record['amount_cents'])}?")
    st.caption("This removes the record from your history and reports.")
    if st.button("Delete transaction", type="primary", width="stretch"):
        try:
            store.delete_transaction(uid, record["id"], record["version"])
            flash("Transaction deleted.")
            st.rerun()
        except (ValueError, PermissionError, SQLAlchemyError) as exc:
            error_message(exc)


@st.dialog("Create a group")
def create_group_dialog(dialog_key):
    validate_session()
    with st.form(f"new_group_{dialog_key}"):
        name = st.text_input("Group name", max_chars=80, placeholder="Hostel crew, Goa trip, Flat 402…")
        submitted = st.form_submit_button("Create group", type="primary", width="stretch")
    if submitted:
        try:
            group = store.create_group(uid, name)
            st.session_state["selected_group"] = group["id"]
            flash("Your group is ready. Share its invite code with your friends.")
            st.rerun()
        except (ValueError, PermissionError, SQLAlchemyError) as exc:
            error_message(exc)


@st.dialog("Join your people")
def join_group_dialog(dialog_key):
    validate_session()
    with st.form(f"join_group_{dialog_key}"):
        code = st.text_input("Invite code", placeholder="Paste the code your friend shared")
        st.caption("Joining gives you access to the group's expenses and balances. Share codes privately.")
        submitted = st.form_submit_button("Join group", type="primary", width="stretch")
    if submitted:
        try:
            group = store.join_group(uid, code.strip())
            st.session_state["selected_group"] = group["id"]
            flash("You're in. Your group is now synced.")
            st.rerun()
        except (ValueError, PermissionError, SQLAlchemyError) as exc:
            error_message(exc)


@st.dialog("Split an expense", width="large")
def shared_expense_dialog(group_id, dialog_key, record=None):
    validate_session()
    record = record or {}
    details = store.group_details(uid, group_id)
    members = {m["id"]: m["name"] for m in details["members"]}
    st.caption(f"{details['group']['name']} · Updates reach every open device automatically.")
    # Split inputs stay outside a form so amount and participant changes update the preview.
    description = st.text_input("What was it for?", value=record.get("description", ""), max_chars=150, key=f"shared_description_{dialog_key}")
    a, b = st.columns(2)
    amount = a.number_input(f"Total ({CURRENCY})", min_value=.01, max_value=10000000.0, value=record.get("amount_cents", 10000) / 100, step=10.0, format="%.2f", key=f"shared_amount_{dialog_key}")
    when = b.date_input("Expense date", value=date.fromisoformat(record["occurred_on"]) if record else today(), max_value=today(), key=f"shared_date_{dialog_key}")
    a, b = st.columns(2)
    payer = a.selectbox("Paid by", list(members), index=list(members).index(record.get("payer_id", uid)), format_func=lambda x: members[x], key=f"shared_payer_{dialog_key}")
    category = b.selectbox("Category", CATEGORIES, index=CATEGORIES.index(record.get("category", "Food & drinks")), key=f"shared_category_{dialog_key}")
    existing = {r["user_id"]: r["amount_cents"] for r in record.get("shares", [])}
    participants = st.multiselect("Split between", list(members), default=list(existing) or list(members), format_func=lambda x: members[x], key=f"shared_participants_{dialog_key}")
    mode = st.radio("Split method", ["Equally", "Exact amounts", "Percentages"], index=1 if record else 0, horizontal=True, key=f"shared_mode_{dialog_key}")
    shares = {}
    split_error = None
    try:
        cents = to_cents(amount)
        defaults = equal_split(cents, participants)
        if mode == "Equally":
            shares = defaults
        elif mode == "Exact amounts":
            for member in participants:
                v = st.number_input(f"{members[member]}'s share ({CURRENCY})", min_value=0.0, max_value=10000000.0, value=(existing.get(member, defaults[member])) / 100, step=.01, format="%.2f", key=f"exact_{dialog_key}_{member}")
                shares[member] = int((Decimal(str(v)) * 100).to_integral_value())
            if sum(shares.values()) != cents:
                raise ValueError(f"Shares total {money(sum(shares.values()))}; the expense total is {money(cents)}.")
        else:
            percents = {}
            default_percent = equal_split(10000, participants)
            for member in participants:
                percents[member] = st.number_input(f"{members[member]}'s percentage", min_value=0.0, max_value=100.0, value=default_percent[member] / 100, step=.01, format="%.2f", key=f"percent_{dialog_key}_{member}")
            shares = percentage_split(cents, percents)
        st.dataframe(pd.DataFrame([{"Participant": members[k], f"Share ({CURRENCY})": v / 100} for k, v in shares.items()]), hide_index=True, width="stretch")
    except ValueError as exc:
        split_error = str(exc)
        st.info(split_error)
    notes = st.text_area("Notes (optional)", value=record.get("notes", ""), max_chars=1000, key=f"shared_notes_{dialog_key}")
    if st.button("Update shared expense" if record else "Add shared expense", type="primary", width="stretch", disabled=bool(split_error), key=f"shared_save_{dialog_key}"):
        try:
            fields = dict(description=description, amount_cents=to_cents(amount), category=category, occurred_on=when.isoformat(), payer_id=payer, shares=shares, notes=notes)
            if record:
                store.update_shared_expense(uid, record["id"], record["version"], **fields)
            else:
                store.create_shared_expense(uid, group_id, **fields)
            flash("Shared expense updated for everyone." if record else "Expense split. Everyone's balance is updated.")
            st.rerun()
        except (ValueError, PermissionError, SQLAlchemyError) as exc:
            error_message(exc)


@st.dialog("Delete shared expense")
def delete_shared_dialog(record):
    validate_session()
    st.write(f"Delete **{record['description']}** · {money(record['amount_cents'])} for everyone?")
    st.caption("All participants' balances will be recalculated.")
    if st.button("Delete for everyone", type="primary", width="stretch"):
        try:
            store.delete_shared_expense(uid, record["id"], record["version"])
            flash("Shared expense deleted. Group balances updated.")
            st.rerun()
        except (ValueError, PermissionError, SQLAlchemyError) as exc:
            error_message(exc)


@st.dialog("Record a settlement")
def settlement_dialog(group_id, dialog_key):
    validate_session()
    details = store.group_details(uid, group_id)
    creditors = {b["user_id"]: b for b in details["balances"] if b["balance_cents"] > 0 and b["user_id"] != uid}
    own_balance = next((b["balance_cents"] for b in details["balances"] if b["user_id"] == uid), 0)
    if own_balance >= 0 or not creditors:
        st.info("You're settled up. No payment to record.")
        return
    receiver = st.selectbox("Paid to", list(creditors), format_func=lambda k: creditors[k]["name"], key=f"settle_receiver_{dialog_key}")
    limit = min(-own_balance, creditors[receiver]["balance_cents"])
    amount = st.number_input(f"Amount sent ({CURRENCY})", min_value=.01, max_value=limit / 100, value=limit / 100, step=.01, format="%.2f", key=f"settle_amount_{dialog_key}")
    st.caption("CashVibe records the transfer. Send the money using your payment app or cash first.")
    confirmed = st.checkbox("I have already sent this payment.", key=f"settle_confirm_{dialog_key}")
    if st.button("Record payment", type="primary", width="stretch", disabled=not confirmed, key=f"settle_save_{dialog_key}"):
        try:
            store.record_settlement(uid, group_id, receiver, to_cents(amount))
            flash("Payment recorded. Your group balances are updated.")
            st.rerun()
        except (ValueError, PermissionError, SQLAlchemyError) as exc:
            error_message(exc)


def dashboard(month):
    rows = ledger(store, uid, month)
    values = totals(rows)
    budgets = store.list_budgets(uid, month)
    overall = next((b["amount_cents"] for b in budgets if b["category"] == "Overall"), None)
    title_col, action_col = st.columns([4, 1])
    with title_col:
        section("YOUR MONEY, IN RHYTHM", f"Hey, {user['name'].split()[0]}. Find your flow.", f"Here's your money picture for {month_label(month)}.")
    with action_col:
        st.write("")
        if st.button("＋ Add transaction", type="primary", width="stretch"):
            transaction_dialog(uuid.uuid4().hex)
    columns = st.columns(4)
    with columns[0]: metric("Tracked balance", money(values["balance"]), "Recorded income minus your spending")
    with columns[1]: metric("Money in", money(values["income"]), "Allowance, earnings & other income", "violet")
    with columns[2]: metric("Your spending", money(values["spending"]), "Personal expenses + your group shares", "amber")
    with columns[3]: metric("Budget remaining", money(overall - values["spending"]) if overall is not None else "Set your pace", "Monthly overall budget" if overall is not None else "Create a budget in the Budgets tab", "neutral")
    st.write("")
    left, right = st.columns([1.4, 1], gap="large")
    with left, st.container(border=True):
        st.subheader("The month in motion")
        st.caption("Your daily spending, including your share of group expenses.")
        if rows:
            daily = {day: 0 for day in range(1, days_in_month(month) + 1)}
            for r in rows:
                if r["kind"] == "expense":
                    daily[int(r["occurred_on"][-2:])] += r["amount_cents"] / 100
            fig = go.Figure(go.Scatter(x=list(daily), y=list(daily.values()), mode="lines", line=dict(color="#48e3b1", width=3), fill="tozeroy", fillcolor="rgba(72,227,177,.09)", hovertemplate=f"Day %{{x}}<br>{SYMBOL}%{{y:,.2f}}<extra></extra>"))
            fig.update_xaxes(title="Day of month", dtick=5)
            st.plotly_chart(chart_style(fig, 300), width="stretch", config={"displayModeBar": False})
        else:
            empty("A fresh start feels good", "Add your allowance and first expense. Your daily spending will appear here.")
    with right, st.container(border=True):
        st.subheader("Where it goes")
        st.caption("Small habits. Clearer patterns.")
        category_chart(values["categories"])
    left, right = st.columns([1.4, 1], gap="large")
    with left, st.container(border=True):
        st.subheader("Latest moves")
        if rows:
            st.dataframe(transaction_frame(rows[:7]), hide_index=True, width="stretch", column_config={f"Amount ({CURRENCY})": st.column_config.NumberColumn(format="%.2f")})
        else:
            st.caption("Your latest transactions will appear here.")
    with right, st.container(border=True):
        st.subheader("✦ A little nudge")
        if not rows:
            st.write("Start with your monthly allowance, then record what you spend. A few small entries make a big difference.")
        elif overall and values["spending"] >= overall:
            st.warning(f"You've reached your monthly budget. You're {money(max(0, values['spending']-overall))} over your plan.")
        elif overall and month == today().strftime("%Y-%m"):
            remaining_days = days_in_month(month) - today().day + 1
            st.write(f"Your remaining budget works out to **{money(max(0, overall-values['spending']) // remaining_days)} a day** for the rest of the month.")
        else:
            top = max(values["categories"], key=values["categories"].get)
            st.write(f"**{top}** is your largest spending category at **{money(values['categories'][top])}**. Set a category budget to keep it in view." if values["spending"] else "You've recorded your income. Add expenses to start spotting patterns.")
        st.caption("SpendGenie can help you explore the numbers and plan your next move.")


def transactions_page(month):
    section("EVERY MOVE COUNTS", "Your money, on record.", "Track personal transactions and see your share of group expenses in one place.")
    if st.button("＋ Add transaction", type="primary"):
        transaction_dialog(uuid.uuid4().hex)
    all_rows = ledger(store, uid, month)
    a, b, c = st.columns([2, 1, 1])
    search = a.text_input("Search", placeholder="Search descriptions or notes…")
    selected_category = b.selectbox("Category", ["All categories"] + CATEGORIES)
    kind = c.selectbox("Type", ["All types", "Expense", "Income"])
    rows = [r for r in all_rows if (not search or search.casefold() in (r["description"] + " " + r.get("notes", "")).casefold()) and (selected_category == "All categories" or r["category"] == selected_category) and (kind == "All types" or r["kind"] == kind.lower())]
    if rows:
        st.dataframe(transaction_frame(rows), hide_index=True, width="stretch", column_config={f"Amount ({CURRENCY})": st.column_config.NumberColumn(format="%.2f")})
        st.caption(f"{len(rows)} records · {month_label(month)} · Shared amounts show only your share.")
        st.download_button("↓ Export this view", export_csv(rows), f"cashvibe-{month}.csv", "text/csv")
    else:
        empty("No transactions here yet", "Add a transaction or adjust your filters to see your records.")
    personal = [r for r in rows if not r.get("shared")]
    if personal:
        with st.expander("Edit or delete a personal transaction"):
            selected = st.selectbox("Choose a transaction", [r["id"] for r in personal], format_func=lambda key: next(f"{r['occurred_on']} · {r['description']} · {money(r['amount_cents'])}" for r in personal if r["id"] == key))
            record = next(r for r in personal if r["id"] == selected)
            a, b, _ = st.columns([1, 1, 3])
            if a.button("Edit", width="stretch"):
                transaction_dialog(uuid.uuid4().hex, record)
            if b.button("Delete", width="stretch"):
                delete_transaction_dialog(record)
    st.caption("Edit shared expenses from their group so every participant sees the same record.")


def budgets_page(month):
    section("GIVE YOUR MONEY A PLAN", "Spend with a little intention.", f"Monthly and category budgets for {month_label(month)}. Your group shares count toward your spending.")
    values = totals(ledger(store, uid, month))
    budgets = store.list_budgets(uid, month)
    left, right = st.columns([1.5, 1], gap="large")
    with right, st.container(border=True):
        st.subheader("Make a plan")
        with st.form("budget_form"):
            category = st.selectbox("Budget for", ["Overall"] + CATEGORIES)
            amount = st.number_input(f"Monthly limit ({CURRENCY})", min_value=.01, max_value=10000000.0, value=5000.0, step=100.0, format="%.2f")
            threshold = st.slider("Give me a heads-up at", min_value=10, max_value=100, value=80, step=5, format="%d%%")
            submitted = st.form_submit_button("Save budget", type="primary", width="stretch")
        if submitted:
            try:
                store.set_budget(uid, month, category, to_cents(amount), threshold)
                flash("Budget saved. You've got a plan.")
                st.rerun()
            except (ValueError, PermissionError, SQLAlchemyError) as exc:
                error_message(exc)
        st.caption("Saving the same category replaces its limit for this month. The overall budget is separate from category budgets.")
    with left:
        if not budgets:
            empty("A little structure, more freedom", "Set an overall budget, then add limits for the categories you care about.")
        for budget in budgets:
            spent = values["spending"] if budget["category"] == "Overall" else values["categories"].get(budget["category"], 0)
            ratio = spent / budget["amount_cents"]
            with st.container(border=True):
                a, b = st.columns([2, 1])
                a.markdown(f"**{budget['category']}**")
                b.markdown(f"**{ratio:.0%} used**")
                st.progress(min(1.0, ratio))
                st.caption(f"{money(spent)} spent of {money(budget['amount_cents'])} · {money(max(0,budget['amount_cents']-spent))} remaining")
                if ratio >= 1:
                    st.warning(f"Budget reached · {money(max(0, spent-budget['amount_cents']))} over your limit.")
                elif ratio * 100 >= budget["threshold"]:
                    st.info(f"Heads-up: you've used at least {budget['threshold']}% of this budget.")


def groups_page(month):
    section("GOOD FRIENDS. CLEAR BALANCES.", "Shared plans, minus the awkward.", "Split expenses, track who owes what, and keep every participant up to date.")
    a, b, _ = st.columns([1, 1, 3])
    if a.button("＋ Create group", type="primary", width="stretch"):
        create_group_dialog(uuid.uuid4().hex)
    if b.button("Join a group", width="stretch"):
        join_group_dialog(uuid.uuid4().hex)
    groups = store.list_groups(uid)
    if not groups:
        empty("Your people, your plans", "Create a group for your flat, hostel, or next trip. Your friends can join with a private invite code.")
        return
    ids = [g["id"] for g in groups]
    if st.session_state.get("selected_group") not in ids:
        st.session_state["selected_group"] = ids[0]
    group_id = st.selectbox("Your groups", ids, format_func=lambda key: next(g["name"] for g in groups if g["id"] == key), key="selected_group")
    details = store.group_details(uid, group_id)
    group = details["group"]
    members = {m["id"]: m["name"] for m in details["members"]}
    st.caption(f"{len(members)} members · {CURRENCY} · Group totals cover all dates.")
    a, b, _ = st.columns([1, 1, 3])
    if a.button("＋ Split expense", type="primary", width="stretch"):
        shared_expense_dialog(group_id, uuid.uuid4().hex)
    if b.button("Settle up", width="stretch"):
        settlement_dialog(group_id, uuid.uuid4().hex)
    st.markdown('<div class="cv-sync">● Live sync · Changes appear on open devices within a few seconds</div>', unsafe_allow_html=True)
    balance = next((r["balance_cents"] for r in details["balances"] if r["user_id"] == uid), 0)
    a, b, c = st.columns(3)
    with a: metric("Your balance", money(abs(balance)), "You are owed" if balance > 0 else "You owe" if balance < 0 else "All settled up", "mint" if balance >= 0 else "amber")
    with b: metric("Group spending", money(sum(e["amount_cents"] for e in details["expenses"])), f"{len(details['expenses'])} shared expenses", "violet")
    with c: metric("Your share", money(sum(s["amount_cents"] for e in details["expenses"] for s in e["shares"] if s["user_id"] == uid)), "All-time spending allocated to you", "neutral")
    expenses_tab, balances_tab, activity_tab, invite_tab = st.tabs(["Expenses", "Balances", "Activity", "Invite friends"])
    with expenses_tab:
        if not details["expenses"]:
            empty("Make the first move", "Split your first expense. Everyone in the group will see it here.")
        for expense in details["expenses"]:
            with st.expander(f"{expense['description']} · {money(expense['amount_cents'])} · {expense['occurred_on']}"):
                st.caption(f"Paid by {members.get(expense['payer_id'], 'Member')} · {expense['category']}")
                st.dataframe(pd.DataFrame([{"Participant": members.get(s["user_id"], s.get("name", "Member")), f"Share ({CURRENCY})": s["amount_cents"] / 100} for s in expense["shares"]]), hide_index=True, width="stretch")
                if expense.get("notes"):
                    st.write(expense["notes"])
                if expense.get("created_by") == uid or group.get("created_by") == uid:
                    a, b, _ = st.columns([1, 1, 3])
                    if a.button("Edit", key=f"edit_{expense['id']}", width="stretch"):
                        shared_expense_dialog(group_id, uuid.uuid4().hex, expense)
                    if b.button("Delete", key=f"delete_{expense['id']}", width="stretch"):
                        delete_shared_dialog(expense)
    with balances_tab:
        for row in details["balances"]:
            value = row["balance_cents"]
            st.write(f"**{row['name']}** · {'is owed' if value > 0 else 'owes' if value < 0 else 'settled'} {money(abs(value)) if value else ''}")
        plan = debt_plan(details["balances"])
        if plan:
            st.subheader("A simple way to settle")
            for row in plan:
                st.write(f"{row['from']} → {row['to']} · **{money(row['amount_cents'])}**")
            st.caption("These transfers settle the group's net balances, even when the original expenses involved different people.")
        else:
            st.success("Everyone is settled up. Good vibes all around.")
    with activity_tab:
        activity = details.get("activity", [])
        if activity:
            for event in activity[:50]:
                st.write(event.get("description", event.get("message", event.get("action", "Group updated"))))
                st.caption(str(event.get("created_at", "")))
        else:
            st.caption("Group updates will appear here.")
        if details["settlements"]:
            st.subheader("Recorded payments")
            for payment in details["settlements"]:
                st.write(f"{members.get(payment.get('from_user_id', payment.get('from_id')), 'Member')} → {members.get(payment.get('to_user_id', payment.get('to_id')), 'Member')} · {money(payment['amount_cents'])}")
    with invite_tab:
        st.write("Share this code privately. Anyone with the code and a CashVibe account can join the group and see its history.")
        st.code(group["invite_code"], language=None)
        if group.get("created_by") == uid and st.button("Replace invite code", help="The old code will stop working. Existing members stay in the group."):
            try:
                store.rotate_invite(uid, group_id)
                flash("Invite code replaced. The previous code no longer works.")
                st.rerun()
            except (ValueError, PermissionError, SQLAlchemyError) as exc:
                error_message(exc)


def insights_page(month):
    section("SEE THE BIGGER PICTURE", "Turn transactions into clarity.", "Explore your patterns with real numbers from your records.")
    all_rows = ledger(store, uid)
    month_rows = [r for r in all_rows if r["occurred_on"].startswith(month)]
    values = totals(month_rows)
    left, right = st.columns(2, gap="large")
    with left, st.container(border=True):
        st.subheader("Your spending mix")
        category_chart(values["categories"])
    with right, st.container(border=True):
        st.subheader("Income meets spending")
        recent = []
        year, mon = map(int, month.split("-"))
        for offset in range(5, -1, -1):
            idx = year * 12 + mon - 1 - offset
            recent.append(f"{idx//12:04d}-{idx%12+1:02d}")
        by_month = {key: totals([r for r in all_rows if r["occurred_on"].startswith(key)]) for key in recent}
        if all_rows:
            fig = go.Figure()
            fig.add_trace(go.Bar(name="Income", x=recent, y=[by_month[m]["income"] / 100 for m in recent], marker_color="#48e3b1"))
            fig.add_trace(go.Bar(name="Spending", x=recent, y=[by_month[m]["spending"] / 100 for m in recent], marker_color="#9484ff"))
            st.plotly_chart(chart_style(fig, 320), width="stretch", config={"displayModeBar": False})
        else:
            empty("Patterns take a few entries", "Start recording income and expenses. Your six-month view will grow with you.")
    expenses = [r for r in month_rows if r["kind"] == "expense"]
    a, b, c = st.columns(3)
    with a: metric("Average recorded expense", money(values["spending"] // len(expenses)) if expenses else money(0), f"Across {len(expenses)} expenses", "neutral")
    with b: metric("Saved from income", money(values["balance"]), "Income minus spending this month", "mint" if values["balance"] >= 0 else "amber")
    current_month = today().strftime("%Y-%m") == month
    elapsed = today().day if current_month else days_in_month(month)
    forecast = round(values["spending"] / elapsed * days_in_month(month))
    with c: metric("At this pace", money(forecast), "Simple month-end estimate; not a prediction" if current_month else "Actual month total", "violet")
    if expenses:
        st.subheader("Largest expenses")
        st.dataframe(transaction_frame(sorted(expenses, key=lambda r:r["amount_cents"], reverse=True)[:5]), hide_index=True, width="stretch")
    st.download_button("↓ Export monthly report", export_csv(month_rows), f"cashvibe-report-{month}.csv", "text/csv")


def goals_page(month):
    section("MAKE ROOM FOR WHAT'S NEXT", "Big plans. Small steps.", "Track savings you set aside for a laptop, a trip, or your own rainy-day fund.")
    left, right = st.columns([1.5, 1], gap="large")
    with right, st.container(border=True):
        st.subheader("Start something good")
        with st.form("new_goal"):
            name = st.text_input("Goal name", max_chars=80, placeholder="My next big thing")
            target = st.number_input(f"Target ({CURRENCY})", min_value=.01, max_value=10000000.0, value=10000.0, step=100.0, format="%.2f")
            target_date = st.date_input("Aim to reach it by", value=today().replace(year=today().year + 1) if not (today().month == 2 and today().day == 29) else date(today().year+1, 2, 28), min_value=today())
            submitted = st.form_submit_button("Create goal", type="primary", width="stretch")
        if submitted:
            try:
                store.create_goal(uid, name, to_cents(target), target_date.isoformat())
                flash("Goal created. One step closer.")
                st.rerun()
            except (ValueError, PermissionError, SQLAlchemyError) as exc:
                error_message(exc)
        st.caption("Goal contributions record savings you've set aside. They don't move money or create an expense.")
    with left:
        goals = store.list_goals(uid)
        if not goals:
            empty("What's your next big thing?", "Name a goal, set a target, and track each bit you save toward it.")
        for goal in goals:
            saved = goal.get("saved_cents", goal.get("current_cents", 0))
            with st.container(border=True):
                st.subheader(goal["name"])
                st.progress(min(1.0, saved / goal["target_cents"]))
                st.caption(f"{money(saved)} of {money(goal['target_cents'])} · Target {goal['target_date']}")
                if saved >= goal["target_cents"]:
                    st.success("You made it. That's a good money moment.")
                else:
                    with st.form(f"contribute_{goal['id']}"):
                        amount = st.number_input(f"Savings to add ({CURRENCY})", min_value=.01, max_value=(goal["target_cents"]-saved)/100, value=min(100.0, (goal["target_cents"]-saved)/100), step=.01, format="%.2f", key=f"goal_amount_{goal['id']}")
                        submitted = st.form_submit_button("Add savings")
                    if submitted:
                        try:
                            store.contribute_goal(uid, goal["id"], to_cents(amount))
                            flash("Savings added to your goal.")
                            st.rerun()
                        except (ValueError, PermissionError, SQLAlchemyError) as exc:
                            error_message(exc)


def spendgenie_page(month):
    section("A LITTLE GUIDANCE. YOUR CALL.", "Meet SpendGenie ✦", "Ask about your spending, plan a budget, or turn a quick message into an expense.")
    agent = Agent(store, uid)
    st.caption(f"Mode: {agent.mode} · SpendGenie reads only your records and the groups you belong to.")
    with st.expander("How SpendGenie works"):
        st.write("Connected AI can use tools to inspect your records and propose transactions or budgets. Every change waits for your confirmation. Without a configured AI provider, Local insights can calculate summaries and parse straightforward expense entries.")
        st.caption("When connected, your message and the records needed to answer it are sent to the selected AI provider. Keep account passwords and recovery keys out of chat.")
    if "chat" not in st.session_state:
        st.session_state["chat"] = [{"role":"assistant", "content":"Hey, I'm SpendGenie. Ask me where your money went, how your budget is doing, or say ‘Spent 250 on lunch today’. I'll show any proposed change before saving it."}]
    prompts = ["How am I doing this month?", "Where am I spending the most?", "How much can I spend per day?"]
    cols = st.columns(3)
    chosen = None
    for col, prompt in zip(cols, prompts):
        if col.button(prompt, width="stretch"):
            chosen = prompt
    for message in st.session_state["chat"][-30:]:
        with st.chat_message(message["role"], avatar="🧞" if message["role"] == "assistant" else None):
            st.markdown(message["content"])
            if message.get("provider"):
                st.caption(message["provider"])
    pending = st.session_state.get("agent_proposal")
    if pending:
        with st.container(border=True):
            st.subheader("Review before saving")
            st.markdown(agent.describe_proposal(pending))
            a, b, _ = st.columns([1, 1, 2])
            if a.button("Confirm & save", type="primary", width="stretch"):
                try:
                    result = agent.execute_proposal(pending)
                    st.session_state.pop("agent_proposal", None)
                    st.session_state["chat"].append({"role":"assistant", "content":str(result.get("message", "Saved. Your records are updated.")) if isinstance(result,dict) else str(result or "Saved. Your records are updated.")})
                    st.rerun()
                except (ValueError, PermissionError, SQLAlchemyError) as exc:
                    error_message(exc)
            if b.button("Cancel", width="stretch"):
                st.session_state.pop("agent_proposal", None)
                st.rerun()
    prompt = st.chat_input("Ask SpendGenie, or describe an expense…", max_chars=2000, disabled=bool(pending))
    if chosen and not pending:
        prompt = chosen
    if prompt:
        st.session_state["chat"].append({"role":"user", "content":prompt})
        with st.spinner("SpendGenie is checking your numbers…"):
            try:
                response = agent.respond(prompt, st.session_state["chat"][-16:-1])
                st.session_state["chat"].append({"role":"assistant", "content":response["message"], "provider":response.get("provider", agent.mode)})
                if response.get("proposal"):
                    st.session_state["agent_proposal"] = response["proposal"]
            except Exception as exc:
                error_message(exc)
        st.rerun()
    if st.button("Clear conversation", type="tertiary"):
        st.session_state.pop("chat", None)
        st.session_state.pop("agent_proposal", None)
        st.rerun()


def settings_page(month):
    section("YOUR SPACE", "A few things worth knowing.", "Your records, your privacy, and your CashVibe setup.")
    with st.container(border=True):
        st.subheader(user["name"])
        st.write(user["email"])
        st.caption(f"Currency: {CURRENCY} · Personal records are visible only to you. Shared expenses and group activity are visible to the group's members.")
        st.write("Keep the recovery key you saved when signing up. You can reset your password from the sign-in page with that key; a successful reset signs out existing sessions and replaces the key.")
    with st.container(border=True):
        st.subheader("Take your records with you")
        all_rows = ledger(store, uid)
        st.download_button("↓ Export all my transactions", export_csv(all_rows), "cashvibe-all-transactions.csv", "text/csv", width="stretch")
        st.caption("The export includes personal transactions and your share of group expenses. CSV exports can be opened in a spreadsheet.")
    with st.container(border=True):
        st.subheader("Made for life across devices")
        st.write("Sign in to the same CashVibe deployment on any device. Shared changes refresh while the group page is open. A cloud deployment uses one persistent database; running separate local Docker installations creates separate trackers.")
        st.caption("CashVibe records expenses and settlements. Payments happen through your own payment service. Savings goals and tracked balance are based on your entries.")
        st.write("AI provider: **" + Agent(store, uid).mode + "**")
    if st.button("Sign out", width="stretch"):
        logout()


with st.sidebar:
    st.markdown('<div class="cv-brand"><span class="cv-mark"><svg width="23" height="23" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 19L19 5M5 5h14v14" stroke="#09201b" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/></svg></span>Cash<span>Vibe</span></div><div class="cv-tag">Your money, in rhythm</div>', unsafe_allow_html=True)
    page = st.radio("Workspace", ["Overview", "Transactions", "Budgets", "Groups", "Insights", "Savings goals", "SpendGenie", "Settings"], label_visibility="collapsed", key="navigation")
    st.divider()
    selected_date = st.date_input("Viewing month", value=today().replace(day=1), key="viewing_month", help="Choose any day in the month you want to explore.")
    month = selected_date.strftime("%Y-%m")
    st.caption(month_label(month))
    st.divider()
    st.markdown(f"**{html.escape(user['name'])}**")
    st.caption(user["email"])
    if st.button("Sign out", key="sidebar_signout", width="stretch"):
        logout()
    st.markdown('<div class="cv-sync">● Your money. A clearer picture.</div>', unsafe_allow_html=True)

if st.session_state.get("flash"):
    st.toast(st.session_state.pop("flash"), icon="✅")


def validate_session():
    current = store.get_user(uid)
    if not current or current.get("auth_version") != user.get("auth_version"):
        logout()


PAGES = {"Overview":dashboard, "Transactions":transactions_page, "Budgets":budgets_page, "Groups":groups_page, "Insights":insights_page, "Savings goals":goals_page, "SpendGenie":spendgenie_page, "Settings":settings_page}


try:
    # Keep shared ledgers and dashboard summaries current. Entry forms stay stable.
    if page in ("Groups", "Overview", "Insights"):
        try:
            sync_seconds = max(3, min(60, int(os.getenv("CASHVIBE_SYNC_SECONDS", "5"))))
        except ValueError:
            sync_seconds = 5

        @st.fragment(run_every=sync_seconds)
        def live_financial_view():
            try:
                validate_session()
                PAGES[page](month)
            except (ValueError, PermissionError, SQLAlchemyError) as exc:
                error_message(exc)

        live_financial_view()
    else:
        validate_session()
        PAGES[page](month)
except (ValueError, PermissionError, SQLAlchemyError) as exc:
    error_message(exc)
