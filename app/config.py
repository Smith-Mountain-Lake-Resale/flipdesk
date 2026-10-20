"""
Configuration loading.

Precedence (highest first):
  1. Environment variables
  2. /config/config.yml  (CONFIG_DIR/config.yml)
  3. Values saved from the web UI (stored in the database `settings` table)
  4. Built-in defaults

Nothing stateful lives inside the container image: the database (SQLite file
or external server), uploaded photos/receipts and backups live under DATA_DIR,
and the config file plus the auto-generated secret key live under CONFIG_DIR.
Both are meant to be bind-mounted from the host.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import quote_plus

import yaml

APP_NAME = "FlipDesk"
APP_VERSION = "1.2.0"

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))


def _ensure_dirs() -> None:
    for d in (DATA_DIR, DATA_DIR / "uploads", DATA_DIR / "backups", CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)


_ensure_dirs()

CONFIG_FILE = CONFIG_DIR / "config.yml"


def _load_file() -> dict:
    if CONFIG_FILE.exists():
        try:
            data = yaml.safe_load(CONFIG_FILE.read_text()) or {}
            return {str(k).upper(): v for k, v in _flatten(data).items()}
        except Exception as exc:  # pragma: no cover - surfaced in UI
            print(f"[config] could not parse {CONFIG_FILE}: {exc}")
    return {}


def _flatten(d: dict, prefix: str = "") -> dict:
    """Allow nested YAML: `ebay: {client_id: x}` -> EBAY_CLIENT_ID."""
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}_{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


_FILE = _load_file()


def get(name: str, default=None):
    """Read a setting from env, then config file, then default."""
    name = name.upper()
    if name in os.environ and os.environ[name] != "":
        return os.environ[name]
    if name in _FILE and _FILE[name] not in (None, ""):
        return _FILE[name]
    return default


def source_of(name: str) -> str | None:
    """Where a value is locked from (env / file) so the UI can show it read-only."""
    name = name.upper()
    if name in os.environ and os.environ[name] != "":
        return "environment variable"
    if name in _FILE and _FILE[name] not in (None, ""):
        return "config file"
    return None


def get_bool(name: str, default: bool = False) -> bool:
    v = get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- secret key
def _secret_key() -> str:
    v = get("SECRET_KEY")
    if v:
        return str(v)
    keyfile = CONFIG_DIR / "secret.key"
    if keyfile.exists():
        return keyfile.read_text().strip()
    key = secrets.token_urlsafe(48)
    keyfile.write_text(key)
    try:
        os.chmod(keyfile, 0o600)
    except OSError:
        pass
    return key


SECRET_KEY = _secret_key()


# ---------------------------------------------------------------- database
def database_url() -> str:
    url = get("DATABASE_URL")
    if url:
        return str(url)
    db_type = str(get("DB_TYPE", "sqlite")).lower()
    if db_type in ("sqlite", "internal", ""):
        path = get("SQLITE_PATH", str(DATA_DIR / "flipdesk.db"))
        return f"sqlite:///{path}"
    host = get("DB_HOST", "localhost")
    name = get("DB_NAME", "flipdesk")
    user = quote_plus(str(get("DB_USER", "flipdesk")))
    pw = quote_plus(str(get("DB_PASSWORD", "")))
    if db_type in ("postgres", "postgresql", "pg"):
        port = get("DB_PORT", 5432)
        return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{name}"
    if db_type in ("mysql", "mariadb"):
        port = get("DB_PORT", 3306)
        return f"mysql+pymysql://{user}:{pw}@{host}:{port}/{name}?charset=utf8mb4"
    raise ValueError(f"Unsupported DB_TYPE '{db_type}' (use sqlite, postgres or mysql)")


DATABASE_URL = database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")


def redacted_db_url() -> str:
    u = DATABASE_URL
    if "@" in u and "://" in u:
        scheme, rest = u.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:****@{host}"
    return u


# ---------------------------------------------------------------- misc
AUTH_MODE = str(get("AUTH_MODE", "local")).lower()  # local | none
TIMEZONE = str(get("TZ", "UTC"))
SESSION_COOKIE_SECURE = get_bool("SESSION_COOKIE_SECURE", False)
