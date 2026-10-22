from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .util import utcnow


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))  # "!" = no local password (SSO-only)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    auth_source: Mapped[str] = mapped_column(String(20), default="local")  # local | oidc
    oidc_issuer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    oidc_sub: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    totp_secret: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_counter: Mapped[int] = mapped_column(Integer, default=0)   # replay protection
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recovery_codes: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of sha256 hashes
    session_epoch: Mapped[int] = mapped_column(Integer, default=0)  # bump to sign out everywhere
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    webauthn_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)  # random handle, not the DB id
    passkey_count: Mapped[int] = mapped_column(Integer, default=0)  # cached; checked on every request

    @property
    def has_second_factor(self) -> bool:
        return bool(self.totp_enabled) or (self.passkey_count or 0) > 0

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash) and self.password_hash != "!"


class Passkey(Base):
    """A WebAuthn credential (passkey) registered to a local account."""
    __tablename__ = "passkeys"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    credential_id: Mapped[str] = mapped_column(String(512), unique=True)  # base64url
    public_key: Mapped[str] = mapped_column(Text)                          # base64url COSE key
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    transports: Mapped[str | None] = mapped_column(String(200), nullable=True)
    aaguid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False)       # synced passkey (iCloud, Google…)
    device_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    name: Mapped[str] = mapped_column(String(100), default="Passkey")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)


class EbayAccount(Base):
    __tablename__ = "ebay_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ebay_user_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    environment: Mapped[str] = mapped_column(String(20), default="production")
    marketplace_id: Mapped[str] = mapped_column(String(20), default="EBAY_US")
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted
    access_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted
    refresh_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    orders_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finances_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    listings_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    backfill_done: Mapped[bool] = mapped_column(Boolean, default=False)
    finances_backfill_done: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class BulkBuy(Base):
    __tablename__ = "bulk_buys"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    total_cost: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    items: Mapped[list["Item"]] = relationship(back_populates="bulk_buy")


class Item(Base):
    """A product you own. One item can span many eBay listing IDs (relists)."""
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="unlisted", index=True)
    # unlisted | listed | sold | written_off
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    quantity_sold: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)  # total cost for `quantity`
    source: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    storage_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    condition: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo: Mapped[str | None] = mapped_column(String(500), nullable=True)  # local upload
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)  # from eBay
    bulk_buy_id: Mapped[int | None] = mapped_column(ForeignKey("bulk_buys.id", ondelete="SET NULL"), nullable=True)
    current_ebay_item_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    original_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    relist_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    reprice_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    promote_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    listed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    written_off_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    bulk_buy: Mapped[BulkBuy | None] = relationship(back_populates="items")
    listings: Mapped[list["Listing"]] = relationship(back_populates="item")
    sales: Mapped[list["Sale"]] = relationship(back_populates="item")

    @property
    def unit_cost(self) -> float | None:
        if self.cost is None:
            return None
        return round(self.cost / max(self.quantity or 1, 1), 4)


class Listing(Base):
    __tablename__ = "listings"
    id: Mapped[int] = mapped_column(primary_key=True)
    ebay_item_id: Mapped[str] = mapped_column(String(40), unique=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sku: Mapped[str | None] = mapped_column(String(120), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantity_sold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    listing_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    duration: Mapped[str | None] = mapped_column(String(20), nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)  # active|ended|relisted|sold
    watch_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    previous_ebay_item_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    relisted_as: Mapped[str | None] = mapped_column(String(40), nullable=True)
    promoted: Mapped[bool] = mapped_column(Boolean, default=False)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pending_relist: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_relist_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    item: Mapped[Item | None] = relationship(back_populates="listings")


class ItemEvent(Base):
    """Product timeline: price/title changes, relists, sales, notes."""
    __tablename__ = "item_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ebay_item_id: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (UniqueConstraint("marketplace", "order_id", "line_item_id", name="uq_sale_line"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace: Mapped[str] = mapped_column(String(40), default="eBay", index=True)
    order_id: Mapped[str] = mapped_column(String(80), index=True)
    line_item_id: Mapped[str] = mapped_column(String(80), default="1")
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"), nullable=True, index=True)
    ebay_item_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sku: Mapped[str | None] = mapped_column(String(120), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    sale_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    sale_price: Mapped[float] = mapped_column(Float, default=0)        # item subtotal
    shipping_charged: Mapped[float] = mapped_column(Float, default=0)  # paid by buyer
    tax_collected: Mapped[float] = mapped_column(Float, default=0)     # collected & remitted by marketplace
    marketplace_fees: Mapped[float] = mapped_column(Float, default=0)
    ad_fees: Mapped[float] = mapped_column(Float, default=0)
    shipping_cost: Mapped[float] = mapped_column(Float, default=0)     # label/postage you paid
    other_fees: Mapped[float] = mapped_column(Float, default=0)
    refund_amount: Mapped[float] = mapped_column(Float, default=0)
    refund_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cogs_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="completed", index=True)
    # completed | partially_refunded | refunded | cancelled
    fulfillment_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    restocked: Mapped[bool] = mapped_column(Boolean, default=False)
    buyer_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    buyer_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    buyer_state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    buyer_postal: Mapped[str | None] = mapped_column(String(40), nullable=True)
    buyer_country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # ebay_sync | manual | import
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    item: Mapped[Item | None] = relationship(back_populates="sales")

    @property
    def cogs(self) -> float | None:
        if self.cogs_override is not None:
            return self.cogs_override
        if self.status == "cancelled":
            return 0.0
        if self.item is not None and self.item.unit_cost is not None:
            if self.restocked:
                return 0.0
            return round(self.item.unit_cost * (self.quantity or 1), 2)
        return None

    @property
    def gross(self) -> float:
        if self.status == "cancelled":
            return 0.0
        return round((self.sale_price or 0) + (self.shipping_charged or 0), 2)

    @property
    def total_costs(self) -> float:
        if self.status == "cancelled":
            return 0.0
        return round((self.marketplace_fees or 0) + (self.ad_fees or 0) + (self.shipping_cost or 0)
                     + (self.other_fees or 0), 2)

    @property
    def net_before_cogs(self) -> float:
        return round(self.gross - self.total_costs - (self.refund_amount or 0), 2)

    @property
    def net_profit(self) -> float:
        return round(self.net_before_cogs - (self.cogs or 0), 2)

    @property
    def roi(self) -> float | None:
        c = self.cogs
        if not c:
            return None
        return round(self.net_profit / c * 100, 1)


class FinanceTransaction(Base):
    """Raw eBay Finances API transactions (fees, labels, credits, ad charges)."""
    __tablename__ = "finance_transactions"
    __table_args__ = (UniqueConstraint("transaction_id", "transaction_type", name="uq_fin_txn"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String(120))
    transaction_type: Mapped[str] = mapped_column(String(40), index=True)
    booking_entry: Mapped[str | None] = mapped_column(String(10), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Float, default=0)
    fee_amount: Mapped[float] = mapped_column(Float, default=0)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    fee_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    transaction_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    expense_date: Mapped[date] = mapped_column(Date, index=True)
    category: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"), nullable=True)
    bulk_buy_id: Mapped[int | None] = mapped_column(ForeignKey("bulk_buys.id", ondelete="SET NULL"), nullable=True)
    receipt: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    external_ref: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MileageEntry(Base):
    __tablename__ = "mileage"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_date: Mapped[date] = mapped_column(Date, index=True)
    miles: Mapped[float] = mapped_column(Float)
    purpose: Mapped[str | None] = mapped_column(String(500), nullable=True)
    start_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    end_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    round_trip: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def total_miles(self) -> float:
        return round(self.miles * (2 if self.round_trip else 1), 1)


class AutomationLog(Base):
    __tablename__ = "automation_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(40))  # relist | reprice | promote | sync
    ebay_item_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    new_ebay_item_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # ok | error | dry_run | skipped
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(60), index=True)
    started: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class DeletionNotice(Base):
    """eBay Marketplace Account Deletion notifications received (compliance log)."""
    __tablename__ = "deletion_notices"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    notification_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ebay_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    records_redacted: Mapped[int] = mapped_column(Integer, default=0)
