"""ORM modely F0. Schéma (včetně RLS a triggerů) definuje migrace
`migrations/versions/0001_initial.py`; modely jí musí odpovídat."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, LargeBinary,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import CIDR, INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True,
                         server_default=func.gen_random_uuid())


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class DeviceState(str, enum.Enum):
    pending = "pending"
    adopted = "adopted"
    offline = "offline"
    removed = "removed"


class DeviceRole(str, enum.Enum):
    unknown = "unknown"
    gateway = "gateway"
    switch = "switch"
    ap = "ap"
    ptp = "ptp"


class UserRole(str, enum.Enum):
    superadmin = "superadmin"
    tenant_admin = "tenant_admin"
    technician = "technician"
    readonly = "readonly"


class SecretKind(str, enum.Enum):
    sstp_password = "sstp_password"
    api_password = "api_password"
    checkin_key = "checkin_key"
    client_cert_key = "client_cert_key"


class Tenant(Base):
    __tablename__ = "tenant"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), unique=True)
    tunnel_block: Mapped[str] = mapped_column(CIDR, unique=True)
    created_at: Mapped[datetime] = _created()
    sites: Mapped[list["Site"]] = relationship(back_populates="tenant")


class Site(Base):
    __tablename__ = "site"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(Text)
    maintenance_window: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = _created()
    tenant: Mapped[Tenant] = relationship(back_populates="sites")


class Device(Base):
    __tablename__ = "device"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="RESTRICT"))
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("site.id", ondelete="SET NULL"))
    serial: Mapped[str] = mapped_column(String(64), unique=True)
    identity: Mapped[str | None] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(100))
    ros_version: Mapped[str | None] = mapped_column(String(50))
    role: Mapped[DeviceRole] = mapped_column(
        Enum(DeviceRole, name="device_role"), default=DeviceRole.unknown)
    state: Mapped[DeviceState] = mapped_column(
        Enum(DeviceState, name="device_state"), default=DeviceState.pending)
    tunnel_ip: Mapped[str | None] = mapped_column(INET, unique=True)
    last_checkin_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class DeviceSecret(Base):
    __tablename__ = "device_secret"
    __table_args__ = (UniqueConstraint("device_id", "kind"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="RESTRICT"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("device.id", ondelete="CASCADE"))
    kind: Mapped[SecretKind] = mapped_column(Enum(SecretKind, name="secret_kind"))
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    rotated_at: Mapped[datetime] = _created()


class AdoptionToken(Base):
    __tablename__ = "adoption_token"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("site.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_device: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("device.id"))
    created_at: Mapped[datetime] = _created()


class AppUser(Base):
    """Uživatel controlleru. Globální (není pod RLS); přístup k tenantům
    určuje RoleBinding."""

    __tablename__ = "app_user"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    totp_secret: Mapped[bytes | None] = mapped_column(LargeBinary)  # šifrovaně
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class RoleBinding(Base):
    __tablename__ = "role_binding"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", "site_id", "role"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"))
    # NULL tenant_id = globální role (jen superadmin)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("site.id", ondelete="CASCADE"))


class AuditLog(Base):
    """Append-only: UPDATE/DELETE blokuje trigger v DB."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = _created()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenant.id"))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str | None] = mapped_column(String(300))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    source_ip: Mapped[str | None] = mapped_column(INET)
