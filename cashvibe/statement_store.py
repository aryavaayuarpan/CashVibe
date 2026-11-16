"""Authorized, transactional statement storage and optional ledger reconciliation."""
from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import Table, Column, String, Text, BigInteger, Integer, ForeignKey, UniqueConstraint, Index, select, insert, update, delete, and_
from cashvibe.statement_parser import CATEGORIES, APPS, PLATFORMS, row_key, MAX_ROWS


def define_statement_tables(metadata):
    global statements, entries
    statements = Table("bank_statements", metadata,
        Column("id", String(36), primary_key=True),
        Column("user_id", String(36), ForeignKey("users.id"), nullable=False),
        Column("label", String(100), nullable=False),
        Column("source_hash", String(64), nullable=False),
        Column("created_at", String(40), nullable=False),
        UniqueConstraint("user_id", "source_hash"))
    entries = Table("statement_entries", metadata,
        Column("id", String(36), primary_key=True),
        Column("statement_id", String(36), ForeignKey("bank_statements.id", ondelete="CASCADE"), nullable=False),
        Column("user_id", String(36), ForeignKey("users.id"), nullable=False),
        Column("occurred_on", String(10), nullable=False),
        Column("description", String(200), nullable=False),
        Column("amount_cents", BigInteger, nullable=False),
        Column("kind", String(10), nullable=False),
        Column("classification", String(10), nullable=False),
        Column("category", String(60), nullable=False),
        Column("payment_app", String(40), nullable=False),
        Column("platform", String(40), nullable=False),
        Column("reference", String(40), nullable=False),
        Column("balance_cents", BigInteger),
        Column("dedupe_key", String(64), nullable=False),
        Column("ledger_transaction_id", String(36), ForeignKey("transactions.id", ondelete="SET NULL")),
        Column("shared_expense_id", String(36), ForeignKey("shared_expenses.id", ondelete="SET NULL")),
        UniqueConstraint("user_id", "dedupe_key"))
    Index("ix_statement_user_date", entries.c.user_id, entries.c.occurred_on)


def _id():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, maximum, label):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or "\x00" in value:
        raise ValueError(f"Enter a valid {label} (up to {maximum} characters).")
    return value.strip()


def _row(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid statement row.")
    try:
        occurred = date.fromisoformat(value["occurred_on"])
        if not 1900 <= occurred.year <= 2100:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError("Every saved row needs a valid YYYY-MM-DD date.") from None
    amount = value.get("amount_cents")
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount <= 100_000_000_000:
        raise ValueError("Every saved row needs a positive amount with at most two decimal places.")
    if value.get("kind") not in ("expense", "income"):
        raise ValueError("Confirm Money out or Money in for every saved row.")
    classification = value.get("classification", value["kind"])
    if classification not in ("expense", "income", "transfer"):
        raise ValueError("Choose Spending, Received, or Transfer for every row.")
    if classification != "transfer" and classification != value["kind"]:
        raise ValueError("Spending must be Money out; Received must be Money in. Use Transfer for internal transfers.")
    if value.get("category") not in CATEGORIES or value.get("payment_app") not in APPS or value.get("platform") not in PLATFORMS:
        raise ValueError("Choose supported categories, payment apps and merchant platforms.")
    balance = value.get("balance_cents")
    if balance is not None and (isinstance(balance, bool) or not isinstance(balance, int) or abs(balance) > 100_000_000_000):
        raise ValueError("Invalid running balance.")
    return dict(occurred_on=occurred.isoformat(), description=_text(value.get("description"),200,"description"),
                amount_cents=amount, kind=value["kind"], classification=classification,
                category=value["category"], payment_app=value["payment_app"], platform=value["platform"],
                reference=str(value.get("reference", "")).strip()[:40], balance_cents=balance)


class StatementMixin:
    def list_statements(self, user_id):
        with self.engine.connect() as conn:
            self._require_user(conn, user_id)
            return [dict(r) for r in conn.execute(select(statements).where(statements.c.user_id == user_id).order_by(statements.c.created_at.desc())).mappings()]

    def list_statement_entries(self, user_id, month=None, day=None, statement_id=None):
        statement = select(entries, statements.c.label.label("statement_label")).join(statements, entries.c.statement_id == statements.c.id).where(entries.c.user_id == user_id)
        if month:
            try:
                date.fromisoformat(month + "-01")
            except (TypeError, ValueError):
                raise ValueError("Choose a valid month.") from None
            statement = statement.where(entries.c.occurred_on.like(month + "-%"))
        if day:
            try:
                day = date.fromisoformat(day).isoformat()
            except (TypeError, ValueError):
                raise ValueError("Choose a valid day.") from None
            statement = statement.where(entries.c.occurred_on == day)
        if statement_id:
            statement = statement.where(entries.c.statement_id == statement_id)
        with self.engine.connect() as conn:
            self._require_user(conn, user_id)
            return [dict(r) for r in conn.execute(statement.order_by(entries.c.occurred_on.desc(), entries.c.id)).mappings()]

    def statement_link_targets(self, user_id):
        # Import here avoids a circular dependency during table registration.
        from cashvibe.store import transactions, expenses, groups, members
        with self.engine.connect() as conn:
            self._require_user(conn, user_id)
            personal = [dict(r, target_type="personal") for r in conn.execute(select(transactions).where(transactions.c.user_id == user_id).order_by(transactions.c.occurred_on.desc()).limit(500)).mappings()]
            shared = [dict(r, target_type="shared",kind="expense") for r in conn.execute(
                select(expenses,groups.c.name.label("group_name")).join(groups,expenses.c.group_id==groups.c.id)
                .join(members,and_(members.c.group_id==groups.c.id,members.c.user_id==user_id))
                .where(expenses.c.payer_id==user_id).order_by(expenses.c.occurred_on.desc()).limit(500)).mappings()]
            return personal + shared

    def _connect_statement_row(self, conn, user_id, row, action, target_id=None):
        from cashvibe.store import transactions, expenses, members
        if action == "Statement only":
            return None, None
        if row["classification"] == "transfer" and action == "Add to ledger":
            raise ValueError("Transfers stay in statement analysis; they are not new spending or income.")
        if action == "Add to ledger":
            tx = self._transaction_values(row["description"],row["amount_cents"],row["category"],row["occurred_on"],row["kind"],row["payment_app"],
                                          "Imported bank statement; merchant: "+row["platform"])
            tx.update(id=_id(),user_id=user_id,version=1,created_at=_now(),updated_at=_now())
            conn.execute(insert(transactions).values(**tx))
            return tx["id"], None
        if action not in ("Link existing", "Link shared expense"):
            raise ValueError("Choose a supported ledger action.")
        if action == "Link existing":
            target = conn.execute(select(transactions).where(transactions.c.id==target_id,transactions.c.user_id==user_id)).mappings().first()
        else:
            target = conn.execute(select(expenses).join(members,and_(members.c.group_id==expenses.c.group_id,members.c.user_id==user_id))
                                  .where(expenses.c.id==target_id,expenses.c.payer_id==user_id)).mappings().first()
        if not target:
            raise ValueError("Choose one of your own matching transactions or shared expenses.")
        target_kind = target["kind"] if action == "Link existing" else "expense"
        if (target["amount_cents"],target["occurred_on"],target_kind)!=(row["amount_cents"],row["occurred_on"],row["kind"]):
            raise ValueError("Linked records must match the statement's date, amount and direction.")
        return (target["id"],None) if action=="Link existing" else (None,target["id"])

    def save_statement_import(self, user_id, label, source_hash, rows):
        label = _text(label,100,"statement name")
        if not isinstance(source_hash,str) or not re.fullmatch(r"[a-f0-9]{64}",source_hash):
            raise ValueError("Invalid statement identifier.")
        if not isinstance(rows,list) or not rows or len(rows)>MAX_ROWS:
            raise ValueError("Review between 1 and 2,000 rows before saving.")
        sid = _id()
        saved = skipped = linked = 0
        occurrences = {}
        with self._write() as conn:
            self._require_user(conn,user_id,lock=True)
            if conn.execute(select(statements.c.id).where(statements.c.user_id==user_id,statements.c.source_hash==source_hash)).first():
                raise ValueError("This exact file was already imported. Open its saved analysis instead.")
            conn.execute(insert(statements).values(id=sid,user_id=user_id,label=label,source_hash=source_hash,created_at=_now()))
            for index,item in enumerate(rows):
                action = item.get("action","Statement only")
                if action == "Skip":
                    skipped += 1
                    continue
                row = _row(item)
                base = row_key(row)
                occurrence = occurrences.get(base,0);occurrences[base]=occurrence+1
                key = row_key(row,occurrence)
                existing = conn.execute(select(entries.c.id).where(entries.c.user_id==user_id,entries.c.dedupe_key==key)).first()
                if existing and not item.get("keep_distinct",False):
                    skipped += 1
                    continue
                if existing:
                    key = hashlib.sha256((key+source_hash+str(index)).encode()).hexdigest()
                tx,shared = self._connect_statement_row(conn,user_id,row,action,item.get("target_id"))
                conn.execute(insert(entries).values(**row,id=_id(),statement_id=sid,user_id=user_id,dedupe_key=key,
                                                   ledger_transaction_id=tx,shared_expense_id=shared))
                saved += 1;linked += bool(tx or shared)
            if not saved:
                raise ValueError("No new rows to save. Existing statement rows were detected; review duplicate matches.")
        return {"id":sid,"saved":saved,"skipped":skipped,"linked":linked}

    def connect_statement_entry(self,user_id,entry_id,action,target_id=None):
        with self._write() as conn:
            self._require_user(conn,user_id,lock=True)
            row=conn.execute(select(entries).where(entries.c.id==entry_id,entries.c.user_id==user_id)).mappings().first()
            if not row:
                raise ValueError("Statement transaction is unavailable.")
            if row["ledger_transaction_id"] or row["shared_expense_id"]:
                raise ValueError("This statement transaction is already linked.")
            tx,shared=self._connect_statement_row(conn,user_id,dict(row),action,target_id)
            conn.execute(update(entries).where(entries.c.id==entry_id,entries.c.user_id==user_id).values(ledger_transaction_id=tx,shared_expense_id=shared))
        return {"ledger_transaction_id":tx,"shared_expense_id":shared}

    def delete_statement(self,user_id,statement_id):
        with self._write() as conn:
            self._require_user(conn,user_id,lock=True)
            owned=conn.execute(select(statements).where(statements.c.id==statement_id,statements.c.user_id==user_id)).first()
            if not owned:
                raise ValueError("Statement is unavailable.")
            # Personal/shared ledger records remain intact after deleting a statement.
            conn.execute(delete(entries).where(entries.c.statement_id==statement_id,entries.c.user_id==user_id))
            conn.execute(delete(statements).where(statements.c.id==statement_id,statements.c.user_id==user_id))

    def statement_summary(self,user_id,month=None,day=None):
        rows=self.list_statement_entries(user_id,month=month,day=day)
        app_totals,platforms,categories,daily = {},{},{},{}
        for row in rows:
            app=app_totals.setdefault(row["payment_app"],{"spent_cents":0,"received_cents":0,"count":0})
            app["spent_cents" if row["kind"]=="expense" else "received_cents"]+=row["amount_cents"];app["count"]+=1
            d=daily.setdefault(row["occurred_on"],{"out_cents":0,"in_cents":0})
            d["out_cents" if row["kind"]=="expense" else "in_cents"]+=row["amount_cents"]
            if row["classification"]=="expense":
                categories[row["category"]]=categories.get(row["category"],0)+row["amount_cents"]
                platforms[row["platform"]]=platforms.get(row["platform"],0)+row["amount_cents"]
        outgoing=sum(r["amount_cents"] for r in rows if r["kind"]=="expense")
        incoming=sum(r["amount_cents"] for r in rows if r["kind"]=="income")
        return {"month":month,"day":day,"count":len(rows),"money_out_cents":outgoing,"money_in_cents":incoming,
                "net_flow_cents":incoming-outgoing,"spending_cents":sum(categories.values()),
                "transfer_out_cents":sum(r["amount_cents"] for r in rows if r["kind"]=="expense" and r["classification"]=="transfer"),
                "by_app":app_totals,"by_platform":platforms,"by_category":categories,"daily":daily,
                "linked_count":sum(bool(r["ledger_transaction_id"] or r["shared_expense_id"]) for r in rows),
                "unknown_app_count":sum(r["payment_app"]=="UPI (app unknown)" for r in rows),
                "note":"Bank cash movements; transfers are excluded from spending. Do not add these totals to ledger totals: linked records overlap."}
