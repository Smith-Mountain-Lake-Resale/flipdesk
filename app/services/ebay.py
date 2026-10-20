"""Thin eBay API client: OAuth (authorization-code + refresh), REST and Trading API."""
from __future__ import annotations

import base64
import hashlib
import secrets
import xml.etree.ElementTree as ET
from datetime import timedelta
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
from sqlalchemy.orm import Session

from .. import settings
from ..models import EbayAccount
from ..util import decrypt, encrypt, utcnow

NS = "{urn:ebay:apis:eBLBaseComponents}"
COMPAT_LEVEL = "1349"

SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/sell.finances",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly",
]

SITE_IDS = {
    "EBAY_US": "0", "EBAY_CA": "2", "EBAY_GB": "3", "EBAY_AU": "15", "EBAY_AT": "16",
    "EBAY_BE": "23", "EBAY_FR": "71", "EBAY_DE": "77", "EBAY_IT": "101", "EBAY_NL": "146",
    "EBAY_ES": "186", "EBAY_IE": "205",
}


class EbayError(Exception):
    def __init__(self, msg: str, status: int | None = None, body: str | None = None):
        super().__init__(msg)
        self.status = status
        self.body = body


class NotConfigured(EbayError):
    pass


def env() -> str:
    return settings.get("EBAY_ENVIRONMENT") or "production"


def hosts() -> dict:
    if env() == "sandbox":
        return {
            "auth": "https://auth.sandbox.ebay.com",
            "api": "https://api.sandbox.ebay.com",
            "apiz": "https://apiz.sandbox.ebay.com",
            "trading": "https://api.sandbox.ebay.com/ws/api.dll",
        }
    return {
        "auth": "https://auth.ebay.com",
        "api": "https://api.ebay.com",
        "apiz": "https://apiz.ebay.com",
        "trading": "https://api.ebay.com/ws/api.dll",
    }


def creds() -> tuple[str, str, str]:
    cid = settings.get("EBAY_CLIENT_ID")
    secret = settings.get("EBAY_CLIENT_SECRET")
    runame = settings.get("EBAY_RUNAME")
    if not (cid and secret and runame):
        raise NotConfigured("eBay App ID, Cert ID and RuName are required (Settings → eBay setup).")
    return cid, secret, runame


def _basic() -> str:
    cid, secret, _ = creds()
    return "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))


# ------------------------------------------------------------------ OAuth
def new_state() -> str:
    return secrets.token_urlsafe(24)


def consent_url(state: str) -> str:
    cid, _, runame = creds()
    q = urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": runame,
        "scope": " ".join(SCOPES),
        "state": state,
        "prompt": "login",
    }, quote_via=quote)
    return f"{hosts()['auth']}/oauth2/authorize?{q}"


def extract_code(pasted: str) -> tuple[str | None, str | None]:
    """Accept either a raw code or the full URL eBay redirected the browser to."""
    pasted = (pasted or "").strip()
    if not pasted:
        return None, None
    if "code=" in pasted:
        qs = parse_qs(urlparse(pasted).query or pasted.split("?", 1)[-1])
        return (qs.get("code") or [None])[0], (qs.get("state") or [None])[0]
    return pasted, None


def exchange_code(db: Session, code: str) -> EbayAccount:
    _, _, runame = creds()
    with _client() as c:
        r = c.post(f"{hosts()['api']}/identity/v1/oauth2/token",
                   headers={"Authorization": _basic(), "Content-Type": "application/x-www-form-urlencoded"},
                   data={"grant_type": "authorization_code", "code": code, "redirect_uri": runame})
    if r.status_code != 200:
        raise EbayError(f"Token exchange failed ({r.status_code}): {r.text[:500]}", r.status_code, r.text)
    tok = r.json()
    acct = db.query(EbayAccount).first() or EbayAccount()
    now = utcnow()
    acct.environment = env()
    acct.marketplace_id = settings.get("EBAY_MARKETPLACE_ID") or "EBAY_US"
    acct.access_token = encrypt(tok["access_token"])
    acct.access_expires = now + timedelta(seconds=int(tok.get("expires_in", 7200)) - 120)
    acct.refresh_token = encrypt(tok.get("refresh_token"))
    acct.refresh_expires = now + timedelta(seconds=int(tok.get("refresh_token_expires_in", 47304000)))
    acct.scopes = " ".join(SCOPES)
    acct.connected_at = now
    acct.last_error = None
    if acct.id is None:
        db.add(acct)
    db.commit()
    try:
        ident = rest(db, acct, "GET", "/commerce/identity/v1/user/", host="apiz")
        acct.username = ident.get("username")
        acct.ebay_user_id = ident.get("userId")
        db.commit()
    except Exception as exc:  # identity is nice-to-have
        acct.last_error = f"Connected, but could not read username: {exc}"
        db.commit()
    return acct


def access_token(db: Session, acct: EbayAccount) -> str:
    tok = decrypt(acct.access_token)
    if tok and acct.access_expires and acct.access_expires > utcnow():
        return tok
    refresh = decrypt(acct.refresh_token)
    if not refresh:
        raise EbayError("eBay authorization missing or expired — reconnect your eBay account.")
    with _client() as c:
        r = c.post(f"{hosts()['api']}/identity/v1/oauth2/token",
                   headers={"Authorization": _basic(), "Content-Type": "application/x-www-form-urlencoded"},
                   data={"grant_type": "refresh_token", "refresh_token": refresh, "scope": " ".join(SCOPES)})
    if r.status_code != 200:
        acct.last_error = f"Token refresh failed: {r.text[:300]}"
        db.commit()
        raise EbayError(f"Token refresh failed ({r.status_code}): {r.text[:300]}", r.status_code, r.text)
    data = r.json()
    acct.access_token = encrypt(data["access_token"])
    acct.access_expires = utcnow() + timedelta(seconds=int(data.get("expires_in", 7200)) - 120)
    db.commit()
    return data["access_token"]


_app_token_cache: dict = {}


def app_token() -> str:
    """Client-credentials token (for connectivity tests and notification public keys)."""
    key = (env(), settings.get("EBAY_CLIENT_ID"))
    hit = _app_token_cache.get(key)
    if hit and hit[1] > utcnow():
        return hit[0]
    with _client() as c:
        r = c.post(f"{hosts()['api']}/identity/v1/oauth2/token",
                   headers={"Authorization": _basic(), "Content-Type": "application/x-www-form-urlencoded"},
                   data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})
    if r.status_code != 200:
        raise EbayError(f"App token failed ({r.status_code}): {r.text[:300]}", r.status_code, r.text)
    d = r.json()
    _app_token_cache[key] = (d["access_token"], utcnow() + timedelta(seconds=int(d.get("expires_in", 7200)) - 120))
    return d["access_token"]


# ------------------------------------------------------------------ REST
def rest(db: Session, acct: EbayAccount, method: str, path: str, *, host: str = "api",
         params: dict | None = None, json: dict | None = None, ok=(200, 201, 204)) -> dict:
    tok = access_token(db, acct)
    headers = {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": acct.marketplace_id or "EBAY_US",
    }
    url = hosts()[host] + path
    with _client() as c:
        for attempt in range(4):
            r = c.request(method, url, headers=headers, params=params, json=json)
            if r.status_code == 429 or r.status_code >= 500:
                import time
                time.sleep(2 ** attempt * 2)
                continue
            break
    if r.status_code not in ok:
        raise EbayError(f"{method} {path} → {r.status_code}: {r.text[:500]}", r.status_code, r.text)
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return {}


# ------------------------------------------------------------------ Trading API
def trading(db: Session, acct: EbayAccount, call: str, inner_xml: str) -> ET.Element:
    tok = access_token(db, acct)
    body = (f'<?xml version="1.0" encoding="utf-8"?>'
            f'<{call}Request xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>High</WarningLevel>'
            f'{inner_xml}</{call}Request>')
    headers = {
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPAT_LEVEL,
        "X-EBAY-API-CALL-NAME": call,
        "X-EBAY-API-SITEID": SITE_IDS.get(acct.marketplace_id or "EBAY_US", "0"),
        "X-EBAY-API-IAF-TOKEN": tok,
        "Content-Type": "text/xml",
    }
    with _client() as c:
        r = c.post(hosts()["trading"], headers=headers, content=body.encode("utf-8"))
    if r.status_code != 200:
        raise EbayError(f"{call} HTTP {r.status_code}: {r.text[:300]}", r.status_code, r.text)
    root = ET.fromstring(r.content)
    ack = text(root, "Ack")
    if ack not in ("Success", "Warning"):
        errs = []
        for e in root.iter(NS + "Errors"):
            if text(e, "SeverityCode") == "Error":
                errs.append(f"[{text(e, 'ErrorCode')}] {text(e, 'LongMessage') or text(e, 'ShortMessage')}")
        raise EbayError(f"{call} failed: {'; '.join(errs) or ack}", 200, r.text)
    return root


def text(el: ET.Element | None, path: str, default: str | None = None) -> str | None:
    if el is None:
        return default
    parts = "/".join(NS + p for p in path.split("/"))
    found = el.find(parts)
    return found.text if found is not None and found.text is not None else default


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


# ------------------------------------------------------------------ account deletion
def challenge_response(challenge_code: str, verification_token: str, endpoint: str) -> str:
    h = hashlib.sha256()
    h.update(challenge_code.encode())
    h.update(verification_token.encode())
    h.update(endpoint.encode())
    return h.hexdigest()


_pubkey_cache: dict = {}


def verify_notification_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Verify X-EBAY-SIGNATURE (ECDSA) using eBay's Notification API public key."""
    if not signature_header:
        return False
    try:
        import json as _json
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        sig = _json.loads(base64.b64decode(signature_header))
        kid = sig["kid"]
        pem = _pubkey_cache.get(kid)
        if not pem:
            with _client() as c:
                r = c.get(f"{hosts()['api']}/commerce/notification/v1/public_key/{kid}",
                          headers={"Authorization": f"Bearer {app_token()}"})
            r.raise_for_status()
            key = r.json()["key"]
            if "BEGIN PUBLIC KEY" in key and "\n" not in key:
                body = key.replace("-----BEGIN PUBLIC KEY-----", "").replace("-----END PUBLIC KEY-----", "")
                key = "-----BEGIN PUBLIC KEY-----\n" + body + "\n-----END PUBLIC KEY-----\n"
            pem = key
            _pubkey_cache[kid] = pem
        pub = serialization.load_pem_public_key(pem.encode())
        digest = hashes.SHA1() if str(sig.get("digest", "SHA1")).upper() == "SHA1" else hashes.SHA256()
        pub.verify(base64.b64decode(sig["signature"]), raw_body, ec.ECDSA(digest))
        return True
    except Exception as exc:
        print(f"[ebay] notification signature not verified: {exc}")
        return False
