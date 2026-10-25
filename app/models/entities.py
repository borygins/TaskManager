from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.sales import CLIENT_STATUSES, mark_labels as resolve_mark_labels
from app.db.session import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), index=True)
    role_expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    role_after_expiry: Mapped[str | None] = mapped_column(String(32), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    clients: Mapped[list["Client"]] = relationship(back_populates="assigned_manager")
    comments: Mapped[list["Comment"]] = relationship(back_populates="author")
    activities: Mapped[list["ActivityLog"]] = relationship(back_populates="actor")


class ClientImport(Base):
    __tablename__ = "client_imports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Funnel(Base):
    __tablename__ = "funnels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    client_links: Mapped[list["ClientFunnel"]] = relationship(back_populates="funnel", cascade="all, delete-orphan")


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), index=True)
    company_name: Mapped[str] = mapped_column(String(255), default="")
    tax_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(64), default="")
    last_call_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_call_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_manager_activity_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default=CLIENT_STATUSES[0])
    monthly_turnover: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    mcc_code: Mapped[str] = mapped_column(String(32), default="")
    competitor_bank: Mapped[str] = mapped_column(String(255), default="")
    registration_address: Mapped[str] = mapped_column(String(500), default="")
    assigned_manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    assigned_manager: Mapped[User | None] = relationship(back_populates="clients")
    funnel_links: Mapped[list["ClientFunnel"]] = relationship(
        back_populates="client", cascade="all, delete-orphan", order_by="ClientFunnel.id",
    )
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
        order_by="(Comment.created_at, Comment.id)",
    )
    activities: Mapped[list["ActivityLog"]] = relationship(back_populates="client", cascade="all, delete-orphan")


class ClientFunnel(Base):
    __tablename__ = "client_funnels"
    __table_args__ = (UniqueConstraint("client_id", "funnel_id", name="uq_client_funnel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    funnel_id: Mapped[int] = mapped_column(ForeignKey("funnels.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    client: Mapped[Client] = relationship(back_populates="funnel_links")
    funnel: Mapped[Funnel] = relationship(back_populates="client_links")


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="card")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    client: Mapped[Client] = relationship(back_populates="comments")
    author: Mapped[User | None] = relationship(back_populates="comments")
    contact: Mapped["ContactActivity | None"] = relationship(back_populates="comment_record")

    @property
    def source_label(self) -> str:
        return {"card": "Комментарий", "contact": "Контакт", "import": "Импорт Excel",
                "legacy": "Прежний комментарий — источник неизвестен",
                "legacy_notes": "Перенесённая заметка — исходные автор и дата неизвестны"}.get(self.source, self.source)


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(64))
    previous_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    client: Mapped[Client] = relationship(back_populates="activities")
    actor: Mapped[User] = relationship(back_populates="activities")


class ContactActivity(Base):
    """A completed attempt/contact; report values are historical snapshots."""

    __tablename__ = "contact_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    funnel_id: Mapped[int | None] = mapped_column(ForeignKey("funnels.id"), nullable=True)
    activity_date: Mapped[date] = mapped_column(Date, index=True)
    activity_type: Mapped[str] = mapped_column(String(32))
    # Kept for existing databases; derived from result on write, not used for counting.
    dialogue: Mapped[bool] = mapped_column(Boolean)
    # `result` remains during the transition to the simplified activity form.
    result: Mapped[str] = mapped_column(String(64))
    client_status: Mapped[str] = mapped_column(String(64), default=CLIENT_STATUSES[0])
    marks: Mapped[list[str]] = mapped_column(JSON, default=list)
    potential_turnover: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    next_activity_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    comment_id: Mapped[int] = mapped_column(ForeignKey("comments.id"), unique=True)
    tax_number: Mapped[str] = mapped_column(String(64))
    company_name: Mapped[str] = mapped_column(String(255))
    funnel_name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    manager: Mapped[User] = relationship()
    comment_record: Mapped[Comment] = relationship(back_populates="contact", lazy="raise")

    @property
    def mark_labels(self) -> list[str]:
        return resolve_mark_labels(self.marks or [])


class ContactReportState(Base):
    """Durable retry marker, committed in the same transaction as a contact."""

    __tablename__ = "contact_report_states"

    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    activity_date: Mapped[date] = mapped_column(Date, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    pending: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
