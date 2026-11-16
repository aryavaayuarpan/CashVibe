"""Transactional persistence and authorization for CashVibe.

Money is stored as integer minor units. All shared data access is authorized here,
not only in the Streamlit UI. PostgreSQL tables live in a non-public schema.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column, ForeignKey, Index, Integer, BigInteger, MetaData, String, Table,
    Text, UniqueConstraint, and_, create_engine, delete, event, insert,
    select, text, update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

MAX_MONEY = 100_000_000_000
SCHEMA = "cashvibe"
metadata = MetaData()

users = Table(
    "users", metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(80), nullable=False),
    Column("email", String(254), nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("recovery_hash", String(64), nullable=False),
    Column("auth_version", Integer, nullable=False, default=1),
    Column("created_at", String(40), nullable=False),
)
auth_attempts = Table(
    "auth_attempts", metadata,
    Column("email", String(254), primary_key=True),
    Column("failures", Integer, nullable=False),
    Column("locked_until", String(40)),
    Column("updated_at", String(40), nullable=False),
)
transactions = Table(
    "transactions", metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("description", String(200), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("category", String(60), nullable=False),
    Column("occurred_on", String(10), nullable=False),
    Column("kind", String(10), nullable=False),
    Column("payment_method", String(40), nullable=False),
    Column("notes", Text, nullable=False, default=""),
    Column("version", Integer, nullable=False, default=1),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)
Index("ix_transactions_user_date", transactions.c.user_id, transactions.c.occurred_on)
budgets = Table(
    "budgets", metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("month", String(7), nullable=False),
    Column("category", String(60), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("threshold", Integer, nullable=False),
    UniqueConstraint("user_id", "month", "category"),
)
groups = Table(
    "groups", metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(80), nullable=False),
    Column("created_by", String(36), ForeignKey("users.id"), nullable=False),
    Column("invite_code", String(64), nullable=False, unique=True),
    Column("created_at", String(40), nullable=False),
)
members = Table(
    "members", metadata,
    Column("group_id", String(36), ForeignKey("groups.id"), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id"), primary_key=True),
    Column("joined_at", String(40), nullable=False),
)
expenses = Table(
    "shared_expenses", metadata,
    Column("id", String(36), primary_key=True),
    Column("group_id", String(36), ForeignKey("groups.id"), nullable=False),
    Column("created_by", String(36), ForeignKey("users.id"), nullable=False),
    Column("payer_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("description", String(200), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("category", String(60), nullable=False),
    Column("occurred_on", String(10), nullable=False),
    Column("notes", Text, nullable=False, default=""),
    Column("version", Integer, nullable=False, default=1),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)
Index("ix_shared_group_date", expenses.c.group_id, expenses.c.occurred_on)
splits = Table(
    "splits", metadata,
    Column("expense_id", String(36), ForeignKey("shared_expenses.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id"), primary_key=True),
    Column("amount_cents", BigInteger, nullable=False),
)
settlements = Table(
    "settlements", metadata,
    Column("id", String(36), primary_key=True),
    Column("group_id", String(36), ForeignKey("groups.id"), nullable=False),
    Column("from_user_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("to_user_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("created_at", String(40), nullable=False),
)
activity = Table(
    "activity", metadata,
    Column("id", String(36), primary_key=True),
    Column("group_id", String(36), ForeignKey("groups.id"), nullable=False),
    Column("actor_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("action", String(40), nullable=False),
    Column("description", String(240), nullable=False),
    Column("created_at", String(40), nullable=False),
)
goals = Table(
    "goals", metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("name", String(80), nullable=False),
    Column("target_cents", BigInteger, nullable=False),
    Column("saved_cents", BigInteger, nullable=False, default=0),
    Column("target_date", String(10), nullable=False),
    Column("created_at", String(40), nullable=False),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _id() -> str:
    return str(uuid.uuid4())


def _str(value: Any, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    value = value.strip()
    if (not value and not allow_empty) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} must contain {'0' if allow_empty else '1'}–{maximum} characters.")
    return value


def _money(value: Any, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Amounts must be whole minor units (cents/paise).")
    if value < (0 if allow_zero else 1) or value > MAX_MONEY:
        raise ValueError("Enter a positive amount within the supported limit.")
    return value


def _date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
        if parsed.year < 1900 or parsed.year > 2100:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Enter a valid date between 1900 and 2100.") from None
    return parsed.isoformat()


def _month(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("Month must use YYYY-MM format.")
    _date(value + "-01")
    return value


def _email(value: str) -> str:
    value = _str(value, "Email", 254).casefold()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError("Enter a valid email address.")
    return value


def _password(value: str) -> str:
    if not isinstance(value, str) or not 10 <= len(value) <= 256:
        raise ValueError("Use a password with 10–256 characters.")
    return value


def _hash_password(value: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode(), salt=salt, n=32768, r=8, p=1, maxmem=128 * 1024 * 1024)
    return "scrypt$32768$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def _check_password(value: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or len(value) > 256:
            return False
        digest = hashlib.scrypt(value.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=128 * 1024 * 1024)
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


def _public_user(row: Any) -> dict:
    return {key: row[key] for key in ("id", "name", "email", "auth_version")}


from cashvibe.statement_store import StatementMixin, define_statement_tables

define_statement_tables(metadata)


class Store(StatementMixin):
    """Thread-safe facade; each operation obtains its own DB transaction."""

    def __init__(self, database_url: str | None = None):
        url = database_url or os.environ.get("DATABASE_URL", "").strip()
        if not url and os.environ.get("REQUIRE_POSTGRES", "").casefold() in ("1", "true", "yes"):
            raise ValueError("A persistent PostgreSQL DATABASE_URL is required for this deployment.")
        if not url:
            path = Path(os.environ.get("CASHVIBE_DB_PATH", "data/cashvibe.db")).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            url = "sqlite:///" + str(path)
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url or url in ("sqlite://", "sqlite+pysqlite://"):
                kwargs["poolclass"] = StaticPool
        else:
            kwargs.update(pool_size=4, max_overflow=4, pool_recycle=300, connect_args={"prepare_threshold": None})
        self._engine = create_engine(url, **kwargs)
        self.is_postgres = self._engine.dialect.name == "postgresql"
        if os.environ.get("REQUIRE_POSTGRES", "").casefold() in ("1", "true", "yes") and not self.is_postgres:
            self._engine.dispose()
            raise ValueError("This deployment requires PostgreSQL for persistent shared data.")
        if self._engine.dialect.name == "sqlite":
            @event.listens_for(self._engine, "connect")
            def _configure_sqlite(dbapi_connection, _):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()
        if self.is_postgres:
            with self._engine.begin() as conn:
                conn.execute(text('CREATE SCHEMA IF NOT EXISTS "cashvibe"'))
                conn.execute(text('REVOKE ALL ON SCHEMA "cashvibe" FROM PUBLIC'))
            self.engine = self._engine.execution_options(schema_translate_map={None: SCHEMA})
        else:
            self.engine = self._engine
        metadata.create_all(self.engine)

    @contextmanager
    def _write(self):
        # SQLite otherwise defers BEGIN until DML, allowing two settlements to
        # validate the same stale balance. BEGIN IMMEDIATE serializes writes.
        with self.engine.connect() as conn:
            if self.is_postgres:
                with conn.begin():
                    yield conn
            else:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise

    @contextmanager
    def _snapshot(self):
        # Group screens combine several queries. Use one snapshot so an expense
        # inserted between queries cannot mismatch its split rows or balances.
        with self.engine.connect() as conn:
            if self.is_postgres:
                conn = conn.execution_options(isolation_level="REPEATABLE READ")
                with conn.begin():
                    yield conn
            else:
                conn.exec_driver_sql("BEGIN")
                try:
                    yield conn
                finally:
                    conn.rollback()

    def close(self) -> None:
        self._engine.dispose()

    def _require_user(self, conn, user_id: str, *, lock: bool = False) -> dict:
        statement = select(users).where(users.c.id == user_id)
        if lock and self.is_postgres:
            statement = statement.with_for_update()
        row = conn.execute(statement).mappings().first()
        if not row:
            raise ValueError("Your session has expired. Sign in again.")
        return dict(row)

    def _require_member(self, conn, user_id: str, group_id: str, *, lock: bool = False) -> dict:
        self._require_user(conn, user_id)
        statement = select(groups).join(members, groups.c.id == members.c.group_id).where(
            groups.c.id == group_id, members.c.user_id == user_id)
        if lock and self.is_postgres:
            statement = statement.with_for_update(of=groups)
        row = conn.execute(statement).mappings().first()
        if not row:
            raise ValueError("This group is unavailable or you are not a member.")
        return dict(row)

    def _log(self, conn, user_id: str, group_id: str, action: str, description: str) -> None:
        conn.execute(insert(activity).values(id=_id(), group_id=group_id, actor_id=user_id,
                                            action=action, description=description[:240], created_at=_now()))

    def register(self, name: str, email: str, password: str, *, issue_recovery_code: bool = True) -> dict:
        name, email, password = _str(name, "Name", 80), _email(email), _password(password)
        code = secrets.token_urlsafe(24) if issue_recovery_code else None
        row = dict(id=_id(), name=name, email=email, password_hash=_hash_password(password),
                   recovery_hash=_hash_code(code) if code else secrets.token_hex(32), auth_version=1, created_at=_now())
        try:
            with self._write() as conn:
                conn.execute(insert(users).values(**row))
        except IntegrityError:
            raise ValueError("That email cannot be registered. Sign in or use account recovery.") from None
        return {"user": _public_user(row), "recovery_code": code}

    def _attempt_status(self, conn, email: str) -> dict | None:
        if self.is_postgres:
            # A transaction-scoped advisory lock also covers a not-yet-existing
            # attempt row, so concurrent first failures cannot collide.
            lock_key = int.from_bytes(hashlib.sha256(email.encode()).digest()[:8], "big", signed=True)
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        statement = select(auth_attempts).where(auth_attempts.c.email == email)
        if self.is_postgres:
            statement = statement.with_for_update()
        row = conn.execute(statement).mappings().first()
        if row and row["locked_until"] and row["locked_until"] > _now():
            raise ValueError("Too many attempts. Try again in 15 minutes.")
        return dict(row) if row else None

    def _failed_attempt(self, conn, email: str, prior: dict | None) -> None:
        current = datetime.now(timezone.utc)
        count = 1
        if prior:
            last = datetime.fromisoformat(prior["updated_at"])
            if current - last < timedelta(minutes=15):
                count = prior["failures"] + 1
        values = dict(failures=count,
                      locked_until=(current + timedelta(minutes=15)).isoformat() if count >= 5 else None,
                      updated_at=current.isoformat())
        if prior:
            conn.execute(update(auth_attempts).where(auth_attempts.c.email == email).values(**values))
        else:
            conn.execute(insert(auth_attempts).values(email=email, **values))

    def login(self, email: str, password: str) -> dict:
        email = _email(email)
        if not isinstance(password, str) or len(password) > 256:
            raise ValueError("Email or password is incorrect.")
        found = None
        with self._write() as conn:
            prior = self._attempt_status(conn, email)
            row = conn.execute(select(users).where(users.c.email == email)).mappings().first()
            if row and _check_password(password, row["password_hash"]):
                conn.execute(delete(auth_attempts).where(auth_attempts.c.email == email))
                found = _public_user(row)
            else:
                # Unknown accounts still pay the password hash cost to reduce enumeration timing.
                if not row:
                    _hash_password(password)
                self._failed_attempt(conn, email, prior)
        if not found:
            raise ValueError("Email or password is incorrect.")
        return found

    def recover(self, email: str, code: str, new_password: str) -> str:
        email, new_password = _email(email), _password(new_password)
        code = _str(code, "Recovery code", 128)
        new_code, success = secrets.token_urlsafe(24), False
        new_hash = _hash_password(new_password)
        with self._write() as conn:
            prior = self._attempt_status(conn, email)
            row = conn.execute(select(users).where(users.c.email == email)).mappings().first()
            if row and hmac.compare_digest(_hash_code(code), row["recovery_hash"]):
                result = conn.execute(update(users).where(users.c.id == row["id"], users.c.recovery_hash == row["recovery_hash"]).values(
                    password_hash=new_hash, recovery_hash=_hash_code(new_code), auth_version=users.c.auth_version + 1))
                success = result.rowcount == 1
                conn.execute(delete(auth_attempts).where(auth_attempts.c.email == email))
            else:
                self._failed_attempt(conn, email, prior)
        if not success:
            raise ValueError("Email or recovery code is incorrect.")
        return new_code

    def get_user(self, user_id: str) -> dict:
        with self.engine.connect() as conn:
            return _public_user(self._require_user(conn, user_id))

    def _transaction_values(self, description, amount_cents, category, occurred_on,
                            kind="expense", payment_method="UPI", notes="") -> dict:
        if kind not in ("expense", "income"):
            raise ValueError("Transaction type must be expense or income.")
        return dict(description=_str(description, "Description", 200), amount_cents=_money(amount_cents),
                    category=_str(category, "Category", 60), occurred_on=_date(occurred_on), kind=kind,
                    payment_method=_str(payment_method, "Payment method", 40),
                    notes=_str(notes, "Notes", 2000, allow_empty=True))

    def create_transaction(self, user_id: str, description: str, amount_cents: int,
                           category: str, occurred_on: str, kind: str = "expense",
                           payment_method: str = "UPI", notes: str = "") -> dict:
        row = self._transaction_values(description, amount_cents, category, occurred_on, kind, payment_method, notes)
        row.update(id=_id(), user_id=user_id, version=1, created_at=_now(), updated_at=_now())
        with self._write() as conn:
            self._require_user(conn, user_id)
            conn.execute(insert(transactions).values(**row))
        return row

    def list_transactions(self, user_id: str, month: str | None = None) -> list[dict]:
        statement = select(transactions).where(transactions.c.user_id == user_id)
        if month:
            month = _month(month)
            statement = statement.where(transactions.c.occurred_on.like(month + "-%"))
        with self.engine.connect() as conn:
            self._require_user(conn, user_id)
            return [dict(row) for row in conn.execute(statement.order_by(transactions.c.occurred_on.desc(), transactions.c.created_at.desc())).mappings()]

    def update_transaction(self, user_id: str, transaction_id: str, version: int, **fields) -> dict:
        allowed = {"description", "amount_cents", "category", "occurred_on", "kind", "payment_method", "notes"}
        if set(fields) - allowed:
            raise ValueError("Unsupported transaction fields.")
        with self._write() as conn:
            self._require_user(conn, user_id)
            row = conn.execute(select(transactions).where(transactions.c.id == transaction_id, transactions.c.user_id == user_id)).mappings().first()
            if not row:
                raise ValueError("Transaction is unavailable.")
            values = self._transaction_values(**{key: fields.get(key, row[key]) for key in allowed})
            result = conn.execute(update(transactions).where(transactions.c.id == transaction_id, transactions.c.user_id == user_id,
                transactions.c.version == version).values(**values, version=transactions.c.version + 1, updated_at=_now()))
            if result.rowcount != 1:
                raise ValueError("This transaction changed elsewhere. Refresh and try again.")
            return dict(conn.execute(select(transactions).where(transactions.c.id == transaction_id)).mappings().one())

    def delete_transaction(self, user_id: str, transaction_id: str, version: int) -> None:
        with self._write() as conn:
            self._require_user(conn, user_id)
            result = conn.execute(delete(transactions).where(transactions.c.id == transaction_id,
                transactions.c.user_id == user_id, transactions.c.version == version))
            if result.rowcount != 1:
                raise ValueError("Transaction unavailable or changed elsewhere. Refresh and try again.")

    def set_budget(self, user_id: str, month: str, category: str, amount_cents: int, threshold: int = 80) -> dict:
        month, category, amount_cents = _month(month), _str(category, "Category", 60), _money(amount_cents)
        if isinstance(threshold, bool) or not isinstance(threshold, int) or not 1 <= threshold <= 100:
            raise ValueError("Alert threshold must be 1–100 percent.")
        with self._write() as conn:
            self._require_user(conn, user_id, lock=True)
            row = conn.execute(select(budgets).where(budgets.c.user_id == user_id, budgets.c.month == month, budgets.c.category == category)).mappings().first()
            if row:
                conn.execute(update(budgets).where(budgets.c.id == row["id"]).values(amount_cents=amount_cents, threshold=threshold))
                return dict(row, amount_cents=amount_cents, threshold=threshold)
            record = dict(id=_id(), user_id=user_id, month=month, category=category, amount_cents=amount_cents, threshold=threshold)
            conn.execute(insert(budgets).values(**record))
            return record

    def list_budgets(self, user_id: str, month: str) -> list[dict]:
        month = _month(month)
        with self.engine.connect() as conn:
            self._require_user(conn, user_id)
            return [dict(row) for row in conn.execute(select(budgets).where(budgets.c.user_id == user_id, budgets.c.month == month).order_by(budgets.c.category)).mappings()]

    def create_group(self, user_id: str, name: str) -> dict:
        row = dict(id=_id(), name=_str(name, "Group name", 80), created_by=user_id,
                   invite_code=secrets.token_urlsafe(18), created_at=_now())
        with self._write() as conn:
            self._require_user(conn, user_id)
            conn.execute(insert(groups).values(**row))
            conn.execute(insert(members).values(group_id=row["id"], user_id=user_id, joined_at=_now()))
            self._log(conn, user_id, row["id"], "group_created", "Created the group")
        return row

    def join_group(self, user_id: str, invite_code: str) -> dict:
        invite_code = _str(invite_code, "Invite code", 64)
        try:
            with self._write() as conn:
                self._require_user(conn, user_id)
                statement = select(groups).where(groups.c.invite_code == invite_code)
                if self.is_postgres:
                    statement = statement.with_for_update()
                group = conn.execute(statement).mappings().first()
                if not group:
                    raise ValueError("That invite code is invalid or has expired.")
                existing = conn.execute(select(members).where(members.c.group_id == group["id"], members.c.user_id == user_id)).first()
                if not existing:
                    count = len(conn.execute(select(members.c.user_id).where(members.c.group_id == group["id"])).all())
                    if count >= 50:
                        raise ValueError("This group has reached its 50-member limit.")
                    conn.execute(insert(members).values(group_id=group["id"], user_id=user_id, joined_at=_now()))
                    self._log(conn, user_id, group["id"], "member_joined", "Joined the group")
                return dict(group)
        except IntegrityError:
            # A duplicate join from another browser is harmless, but do not reveal unrelated groups.
            with self.engine.connect() as conn:
                group = conn.execute(select(groups).where(groups.c.invite_code == invite_code)).mappings().first()
                if group:
                    self._require_member(conn, user_id, group["id"])
                    return dict(group)
            raise ValueError("Could not join the group. Please try again.") from None

    def list_groups(self, user_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            self._require_user(conn, user_id)
            statement = select(groups).join(members, groups.c.id == members.c.group_id).where(members.c.user_id == user_id).order_by(groups.c.created_at.desc())
            return [dict(row) for row in conn.execute(statement).mappings()]

    def rotate_invite(self, user_id: str, group_id: str) -> dict:
        with self._write() as conn:
            group = self._require_member(conn, user_id, group_id, lock=True)
            if group["created_by"] != user_id:
                raise ValueError("Only the group owner can replace its invite code.")
            code = secrets.token_urlsafe(18)
            conn.execute(update(groups).where(groups.c.id == group_id).values(invite_code=code))
            self._log(conn, user_id, group_id, "invite_rotated", "Replaced the group invite code")
            return dict(group, invite_code=code)

    def _shared_values(self, conn, group_id: str, description: str, amount_cents: int,
                       category: str, occurred_on: str, payer_id: str, shares: dict,
                       notes: str = "") -> tuple[dict, dict]:
        valid_members = set(conn.execute(select(members.c.user_id).where(members.c.group_id == group_id)).scalars())
        if payer_id not in valid_members:
            raise ValueError("The payer must be a member of this group.")
        if not isinstance(shares, dict) or not shares or not set(shares).issubset(valid_members):
            raise ValueError("Choose valid group members for this split.")
        normalized = {key: _money(value, allow_zero=True) for key, value in shares.items()}
        amount_cents = _money(amount_cents)
        if sum(normalized.values()) != amount_cents:
            raise ValueError("Split amounts must add up exactly to the expense total.")
        return dict(description=_str(description, "Description", 200), amount_cents=amount_cents,
                    category=_str(category, "Category", 60), occurred_on=_date(occurred_on),
                    payer_id=payer_id, notes=_str(notes, "Notes", 2000, allow_empty=True)), normalized

    def _balances(self, conn, group_id: str) -> list[dict]:
        people = conn.execute(select(users.c.id, users.c.name).join(members, users.c.id == members.c.user_id).where(members.c.group_id == group_id)).mappings().all()
        balances = {p["id"]: {"user_id": p["id"], "name": p["name"], "balance_cents": 0} for p in people}
        for row in conn.execute(select(expenses.c.payer_id, expenses.c.amount_cents).where(expenses.c.group_id == group_id)).mappings():
            balances[row["payer_id"]]["balance_cents"] += row["amount_cents"]
        for row in conn.execute(select(splits.c.user_id, splits.c.amount_cents).join(expenses, splits.c.expense_id == expenses.c.id).where(expenses.c.group_id == group_id)).mappings():
            balances[row["user_id"]]["balance_cents"] -= row["amount_cents"]
        for row in conn.execute(select(settlements).where(settlements.c.group_id == group_id)).mappings():
            balances[row["from_user_id"]]["balance_cents"] += row["amount_cents"]
            balances[row["to_user_id"]]["balance_cents"] -= row["amount_cents"]
        return list(balances.values())

    def group_details(self, user_id: str, group_id: str) -> dict:
        with self._snapshot() as conn:
            group = self._require_member(conn, user_id, group_id)
            people = [dict(row) for row in conn.execute(select(users.c.id, users.c.name).join(members, users.c.id == members.c.user_id).where(members.c.group_id == group_id).order_by(members.c.joined_at)).mappings()]
            names = {p["id"]: p["name"] for p in people}
            records = [dict(row) for row in conn.execute(select(expenses).where(expenses.c.group_id == group_id).order_by(expenses.c.occurred_on.desc(), expenses.c.created_at.desc())).mappings()]
            by_id = {row["id"]: row for row in records}
            for row in records:
                row["payer_name"] = names[row["payer_id"]]
                row["shares"] = []
            for row in conn.execute(select(splits).join(expenses, splits.c.expense_id == expenses.c.id).where(expenses.c.group_id == group_id)).mappings():
                by_id[row["expense_id"]]["shares"].append({"user_id": row["user_id"], "name": names[row["user_id"]], "amount_cents": row["amount_cents"]})
            settled = [dict(row, from_name=names[row["from_user_id"]], to_name=names[row["to_user_id"]]) for row in conn.execute(select(settlements).where(settlements.c.group_id == group_id).order_by(settlements.c.created_at.desc())).mappings()]
            log = [dict(row, actor_name=names.get(row["actor_id"], "Member")) for row in conn.execute(select(activity).where(activity.c.group_id == group_id).order_by(activity.c.created_at.desc()).limit(100)).mappings()]
            return {"group": group, "members": people, "expenses": records, "settlements": settled,
                    "balances": self._balances(conn, group_id), "activity": log}

    def create_shared_expense(self, user_id: str, group_id: str, description: str,
                              amount_cents: int, category: str, occurred_on: str,
                              payer_id: str, shares: dict, notes: str = "") -> dict:
        with self._write() as conn:
            self._require_member(conn, user_id, group_id, lock=True)
            values, normalized = self._shared_values(conn, group_id, description, amount_cents, category, occurred_on, payer_id, shares, notes)
            row = dict(values, id=_id(), group_id=group_id, created_by=user_id, version=1, created_at=_now(), updated_at=_now())
            conn.execute(insert(expenses).values(**row))
            conn.execute(insert(splits), [dict(expense_id=row["id"], user_id=uid, amount_cents=amount) for uid, amount in normalized.items()])
            self._log(conn, user_id, group_id, "expense_added", "Added " + values["description"])
            return dict(row, shares=[{"user_id": uid, "amount_cents": amount} for uid, amount in normalized.items()])

    def update_shared_expense(self, user_id: str, expense_id: str, version: int, **fields) -> dict:
        allowed = {"description", "amount_cents", "category", "occurred_on", "payer_id", "shares", "notes"}
        if set(fields) - allowed:
            raise ValueError("Unsupported expense fields.")
        with self._write() as conn:
            existing = conn.execute(select(expenses).where(expenses.c.id == expense_id)).mappings().first()
            if not existing:
                raise ValueError("This expense is unavailable.")
            group = self._require_member(conn, user_id, existing["group_id"], lock=True)
            if user_id not in (existing["created_by"], group["created_by"]):
                raise ValueError("Only the expense creator or group owner can edit this expense.")
            prior_shares = {row["user_id"]: row["amount_cents"] for row in conn.execute(select(splits).where(splits.c.expense_id == expense_id)).mappings()}
            merged = {key: fields.get(key, existing[key]) for key in allowed - {"shares"}}
            values, normalized = self._shared_values(conn, existing["group_id"], **merged, shares=fields.get("shares", prior_shares))
            result = conn.execute(update(expenses).where(expenses.c.id == expense_id, expenses.c.version == version).values(**values, version=expenses.c.version + 1, updated_at=_now()))
            if result.rowcount != 1:
                raise ValueError("This expense changed on another device. Refresh before editing it again.")
            conn.execute(delete(splits).where(splits.c.expense_id == expense_id))
            conn.execute(insert(splits), [dict(expense_id=expense_id, user_id=uid, amount_cents=amount) for uid, amount in normalized.items()])
            self._log(conn, user_id, existing["group_id"], "expense_updated", "Updated " + values["description"])
            return dict(existing, **values, version=version + 1, shares=[{"user_id": uid, "amount_cents": amount} for uid, amount in normalized.items()])

    def delete_shared_expense(self, user_id: str, expense_id: str, version: int) -> None:
        with self._write() as conn:
            existing = conn.execute(select(expenses).where(expenses.c.id == expense_id)).mappings().first()
            if not existing:
                raise ValueError("This expense is unavailable.")
            group = self._require_member(conn, user_id, existing["group_id"], lock=True)
            if user_id not in (existing["created_by"], group["created_by"]):
                raise ValueError("Only the expense creator or group owner can delete this expense.")
            # Deleting shares and expense is one transaction. An optimistic-lock failure rolls both back.
            if existing["version"] != version:
                raise ValueError("This expense changed on another device. Refresh before deleting it.")
            conn.execute(delete(splits).where(splits.c.expense_id == expense_id))
            result = conn.execute(delete(expenses).where(expenses.c.id == expense_id, expenses.c.version == version))
            if result.rowcount != 1:
                raise ValueError("This expense changed on another device. Refresh before deleting it.")
            self._log(conn, user_id, existing["group_id"], "expense_deleted", "Deleted " + existing["description"])

    def record_settlement(self, user_id: str, group_id: str, to_user_id: str, amount_cents: int) -> dict:
        amount_cents = _money(amount_cents)
        with self._write() as conn:
            self._require_member(conn, user_id, group_id, lock=True)
            balance_map = {row["user_id"]: row["balance_cents"] for row in self._balances(conn, group_id)}
            if to_user_id == user_id or to_user_id not in balance_map:
                raise ValueError("Choose another group member to settle with.")
            payable = min(-balance_map[user_id], balance_map[to_user_id])
            if payable <= 0 or amount_cents > payable:
                raise ValueError("This amount exceeds your current debt or the recipient's outstanding credit. Refresh balances.")
            row = dict(id=_id(), group_id=group_id, from_user_id=user_id, to_user_id=to_user_id, amount_cents=amount_cents, created_at=_now())
            conn.execute(insert(settlements).values(**row))
            self._log(conn, user_id, group_id, "settlement_recorded", "Recorded a settlement")
            return row

    def delete_settlement(self, user_id: str, settlement_id: str) -> None:
        """Correct an erroneous recorded payment; this never moves real money."""
        with self._write() as conn:
            existing = conn.execute(select(settlements).where(settlements.c.id == settlement_id)).mappings().first()
            if not existing:
                raise ValueError("This settlement is unavailable or was already removed.")
            group = self._require_member(conn, user_id, existing["group_id"], lock=True)
            if user_id not in (existing["from_user_id"], group["created_by"]):
                raise ValueError("Only the sender or group owner can remove this settlement.")
            result = conn.execute(delete(settlements).where(settlements.c.id == settlement_id))
            if result.rowcount != 1:
                raise ValueError("This settlement was already removed. Refresh balances.")
            self._log(conn, user_id, existing["group_id"], "settlement_deleted", "Removed a recorded settlement")

    def list_shared_shares(self, user_id: str, month: str | None = None) -> list[dict]:
        statement = select(expenses, splits.c.amount_cents.label("share_cents"), groups.c.name.label("group_name")).join(
            splits, expenses.c.id == splits.c.expense_id).join(groups, expenses.c.group_id == groups.c.id).join(
            members, and_(members.c.group_id == groups.c.id, members.c.user_id == user_id)).where(splits.c.user_id == user_id)
        if month:
            month = _month(month)
            statement = statement.where(expenses.c.occurred_on.like(month + "-%"))
        with self.engine.connect() as conn:
            self._require_user(conn, user_id)
            result = []
            for item in conn.execute(statement.order_by(expenses.c.occurred_on.desc(), expenses.c.created_at.desc())).mappings():
                row = dict(item)
                row.update(total_amount_cents=row["amount_cents"], amount_cents=row.pop("share_cents"), kind="expense", payment_method="Shared", shared=True)
                result.append(row)
            return result

    def create_goal(self, user_id: str, name: str, target_cents: int, target_date: str) -> dict:
        row = dict(id=_id(), user_id=user_id, name=_str(name, "Goal name", 80),
                   target_cents=_money(target_cents), saved_cents=0, target_date=_date(target_date), created_at=_now())
        with self._write() as conn:
            self._require_user(conn, user_id)
            conn.execute(insert(goals).values(**row))
        return row

    def list_goals(self, user_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            self._require_user(conn, user_id)
            return [dict(row) for row in conn.execute(select(goals).where(goals.c.user_id == user_id).order_by(goals.c.target_date)).mappings()]

    def contribute_goal(self, user_id: str, goal_id: str, amount_cents: int) -> dict:
        amount_cents = _money(amount_cents)
        with self._write() as conn:
            self._require_user(conn, user_id)
            result = conn.execute(update(goals).where(goals.c.id == goal_id, goals.c.user_id == user_id,
                goals.c.saved_cents + amount_cents <= goals.c.target_cents).values(saved_cents=goals.c.saved_cents + amount_cents))
            if result.rowcount != 1:
                raise ValueError("Goal unavailable or this contribution exceeds the remaining target.")
            return dict(conn.execute(select(goals).where(goals.c.id == goal_id, goals.c.user_id == user_id)).mappings().one())
