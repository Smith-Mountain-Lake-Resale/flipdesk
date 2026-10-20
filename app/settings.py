"""Runtime settings editable from the web UI (overridable by env / config file)."""
from __future__ import annotations

import json
import threading

from sqlalchemy.orm import Session

from . import config
from .db import SessionLocal
from .models import Setting
from .util import decrypt, encrypt

# key: (default, type, secret, group, label, help)
REGISTRY: dict[str, tuple] = {
    # ---- eBay developer keys
    "EBAY_ENVIRONMENT": ("production", "choice:production,sandbox", False, "ebay", "Environment", "Use sandbox keys for testing, production keys for your real store."),
    "EBAY_CLIENT_ID": ("", "str", False, "ebay", "App ID (Client ID)", "From developer.ebay.com → Application Keys."),
    "EBAY_CLIENT_SECRET": ("", "str", True, "ebay", "Cert ID (Client Secret)", "From developer.ebay.com → Application Keys. Stored encrypted."),
    "EBAY_DEV_ID": ("", "str", False, "ebay", "Dev ID", "Optional. Shown in Application Keys."),
    "EBAY_RUNAME": ("", "str", False, "ebay", "RuName (eBay Redirect URL name)", "From User Tokens → Get a Token from eBay via Your Application. Looks like Your_Name-YourApp-PRD-abc123-xyz."),
    "EBAY_MARKETPLACE_ID": ("EBAY_US", "choice:EBAY_US,EBAY_CA,EBAY_GB,EBAY_AU,EBAY_DE,EBAY_FR,EBAY_IT,EBAY_ES,EBAY_IE,EBAY_NL,EBAY_AT,EBAY_BE", False, "ebay", "Marketplace", "Your home eBay site."),
    "EBAY_VERIFICATION_TOKEN": ("", "str", True, "ebay", "Account-deletion verification token", "32–80 characters (letters, digits, _ and -). Generated for you; paste the same value into eBay."),
    "PUBLIC_URL": ("", "str", False, "ebay", "Public URL of this app", "e.g. https://flip.example.com — used to build the OAuth accepted URL and the account-deletion endpoint. Must be HTTPS for eBay."),
    "STORE_BUYER_USERNAME": ("false", "bool", False, "ebay", "Store buyer usernames", "Off by default. Buyer city/state/country is always kept for the buyer map."),
    # ---- sync
    "SYNC_ENABLED": ("true", "bool", False, "sync", "Automatic sync", "Pull listings, orders and finances on a schedule."),
    "SYNC_ORDERS_MINUTES": ("15", "int", False, "sync", "Orders every (minutes)", ""),
    "SYNC_LISTINGS_MINUTES": ("30", "int", False, "sync", "Active listings every (minutes)", ""),
    "SYNC_FINANCES_MINUTES": ("60", "int", False, "sync", "Finances (fees, labels, ad fees) every (minutes)", ""),
    "BACKFILL_DAYS": ("730", "int", False, "sync", "History to backfill on first connect (days)", "eBay returns up to 2 years (730 days) of orders."),
    "RESTOCK_ON_CANCEL": ("true", "bool", False, "sync", "Return cancelled items to inventory", "Auto-reconciliation for cancellations."),
    "RESTOCK_ON_FULL_REFUND": ("false", "bool", False, "sync", "Return fully-refunded items to inventory", "Turn on if most of your refunds are returned items you resell."),
    # ---- automations
    "AUTOMATION_DRY_RUN": ("true", "bool", False, "automation", "Dry-run mode", "Log what automations would do without changing anything on eBay. Turn off once you are happy with the log."),
    "RELIST_ENABLED": ("false", "bool", False, "automation", "End & relist before auto-renew", "Ends and relists Good-'Til-Cancelled fixed-price listings shortly before eBay's 30-day auto-renew."),
    "RELIST_WINDOW_HOURS": ("1", "int", False, "automation", "Relist this many hours before renewal", ""),
    "RELIST_MIN_AGE_DAYS": ("30", "int", False, "automation", "Only relist listings at least this old (days)", ""),
    "RELIST_SKIP_WATCHERS": ("0", "int", False, "automation", "Skip if watchers ≥ (0 = never skip)", "Watchers are lost on relist."),
    "RELIST_SKIP_WITH_SALES": ("true", "bool", False, "automation", "Skip multi-quantity listings that have sales", "Keeps sales history / best-match boost on proven sellers."),
    "RELIST_MAX_PER_RUN": ("50", "int", False, "automation", "Max relists per run", ""),
    "REPRICE_ENABLED": ("false", "bool", False, "automation", "Reprice on relist", "Lowers price by a percentage each time an item is relisted."),
    "REPRICE_PERCENT": ("5", "float", False, "automation", "Lower price by (%) per relist", ""),
    "REPRICE_MAX_REDUCTION_PERCENT": ("30", "float", False, "automation", "Maximum total reduction from original price (%)", "Item-level minimum price is also respected."),
    "REPRICE_AFTER_DAYS": ("30", "int", False, "automation", "Start repricing after item listed (days)", ""),
    "PROMOTE_ENABLED": ("false", "bool", False, "automation", "Delayed promotion", "Adds listings to a Promoted Listings campaign only after they've gone unsold for a while."),
    "PROMOTE_AFTER_DAYS": ("30", "int", False, "automation", "Promote after unsold for (days)", ""),
    "PROMOTE_CAMPAIGN_ID": ("", "str", False, "automation", "Campaign ID", "A RUNNING Promoted Listings General (cost per sale) campaign with manual listing selection."),
    "PROMOTE_AD_RATE": ("2.0", "float", False, "automation", "Ad rate (%)", "2.0 – 100.0"),
    # ---- alerts
    "ALERT_ON_SALE": ("true", "bool", False, "alerts", "Alert on new sale (with storage location)", ""),
    "ALERT_ON_ISSUES": ("true", "bool", False, "alerts", "Alert on detected errors", "Duplicate label charges, failed relists, etc."),
    "NTFY_URL": ("", "str", False, "alerts", "ntfy topic URL", "e.g. https://ntfy.sh/my-flip-sales"),
    "DISCORD_WEBHOOK_URL": ("", "str", True, "alerts", "Discord webhook URL", ""),
    "WEBHOOK_URL": ("", "str", True, "alerts", "Generic JSON webhook URL", "POSTs {title, body, kind, link}. Works with Home Assistant, n8n, Slack-compatible relays, etc."),
    "SMTP_HOST": ("", "str", False, "alerts", "SMTP host", ""),
    "SMTP_PORT": ("587", "int", False, "alerts", "SMTP port", ""),
    "SMTP_USER": ("", "str", False, "alerts", "SMTP username", ""),
    "SMTP_PASSWORD": ("", "str", True, "alerts", "SMTP password", ""),
    "SMTP_FROM": ("", "str", False, "alerts", "From address", ""),
    "SMTP_TO": ("", "str", False, "alerts", "Send alerts to", ""),
    "SMTP_STARTTLS": ("true", "bool", False, "alerts", "Use STARTTLS", ""),
    # ---- tax / general
    "TAX_METHOD": ("accrual", "choice:accrual,cash", False, "tax", "Default tax-report inventory method", "Accrual: cost is deducted when the item sells (COGS). Cash: inventory purchases are deducted when paid."),
    "MILEAGE_RATES": ('{"2023": 0.655, "2024": 0.67, "2025": 0.70, "2026": 0.725}', "json", False, "tax", "IRS standard mileage rate by year ($/mile)", "Add new years as the IRS publishes them."),
    "CURRENCY_SYMBOL": ("$", "str", False, "tax", "Currency symbol", ""),
    "BACKUP_ENABLED": ("true", "bool", False, "system", "Nightly database backup", "SQLite: copies the DB to /data/backups. External DB: writes a JSON export to /data/backups."),
    "BACKUP_KEEP": ("14", "int", False, "system", "Backups to keep", ""),
    "BACKUP_HOUR": ("3", "int", False, "system", "Backup hour (0–23, container TZ)", ""),
}

_cache: dict[str, str | None] = {}
_lock = threading.Lock()


def _load_cache(db: Session | None = None) -> None:
    own = db is None
    db = db or SessionLocal()
    try:
        rows = db.query(Setting).all()
        with _lock:
            _cache.clear()
            for r in rows:
                _cache[r.key] = decrypt(r.value) if r.encrypted else r.value
    finally:
        if own:
            db.close()


def invalidate() -> None:
    _load_cache()


def locked_by(key: str) -> str | None:
    return config.source_of(key)


def get(key: str):
    """Typed value: env > config file > DB > default."""
    default, typ, *_ = REGISTRY.get(key, (None, "str", False))
    raw = config.get(key)
    if raw is None:
        if not _cache:
            try:
                _load_cache()
            except Exception:
                pass
        raw = _cache.get(key)
    if raw is None or raw == "":
        raw = default
    return _coerce(raw, typ)


def _coerce(raw, typ: str):
    if typ == "bool":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if typ == "int":
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return 0
    if typ == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    if typ == "json":
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return "" if raw is None else str(raw)


def set_many(db: Session, values: dict[str, str | None]) -> None:
    for key, val in values.items():
        if key not in REGISTRY or locked_by(key):
            continue
        secret = REGISTRY[key][2]
        row = db.get(Setting, key)
        if row is None:
            row = Setting(key=key)
            db.add(row)
        row.encrypted = secret
        row.value = encrypt(val) if (secret and val) else val
    db.commit()
    _load_cache(db)


def set_one(db: Session, key: str, val: str | None) -> None:
    set_many(db, {key: val})


def raw_display(key: str) -> str:
    v = get(key)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return "" if v is None else str(v)


def group(name: str) -> list[dict]:
    out = []
    for key, (default, typ, secret, grp, label, help_) in REGISTRY.items():
        if grp != name:
            continue
        out.append({
            "key": key, "type": typ, "secret": secret, "label": label, "help": help_,
            "value": raw_display(key), "locked": locked_by(key),
            "choices": typ.split(":", 1)[1].split(",") if typ.startswith("choice:") else None,
        })
    return out


def mileage_rate(year: int) -> float:
    rates = get("MILEAGE_RATES") or {}
    if str(year) in rates:
        return float(rates[str(year)])
    if rates:
        # fall back to the closest known year
        closest = min(rates.keys(), key=lambda y: abs(int(y) - year))
        return float(rates[closest])
    return 0.0
