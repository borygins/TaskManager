from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    clients: Mapped[list["Client"]] = relationship(back_populates="assigned_manager")
    comments: Mapped[list["Comment"]] = relationship(back_populates="author")
    activities: Mapped[list["ActivityLog"]] = relationship(back_populates="actor")


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
    manager_notes: Mapped[str] = mapped_column(Text, default="")
    last_call_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_call_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="Поиск контакта")
    monthly_turnover: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    mcc_code: Mapped[str] = mapped_column(String(32), default="")
    competitor_bank: Mapped[str] = mapped_column(String(255), default="")
    registration_address: Mapped[str] = mapped_column(String(500), default="")
    assigned_manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    assigned_manager: Mapped[User | None] = relationship(back_populates="clients")
    funnel_links: Mapped[list["ClientFunnel"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    comments: Mapped[list["Comment"]] = relationship(back_populates="client", cascade="all, delete-orphan")
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
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    client: Mapped[Client] = relationship(back_populates="comments")
    author: Mapped[User] = relationship(back_populates="comments")


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
