"""OpenID Connect (authorization code + PKCE) client and user provisioning."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import settings
from ..models import User

_cache: dict[str, tuple[float, dict]] = {}
CACHE_SECONDS = 3600


class OIDCError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.get("OIDC_ENABLED") and settings.get("OIDC_ISSUER") and settings.get("OIDC_CLIENT_ID"))


def _issuer() -> str:
    return (settings.get("OIDC_ISSUER") or "").strip()


def _get_json(url: str) -> dict:
    with httpx.Client(timeout=15, follow_redirects=True) as c:
        r = c.get(url, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OIDCError(f"GET {url} → HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise OIDCError(f"{url} did not return JSON")


def discovery(force: bool = False) -> dict:
    iss = _issuer()
    if not iss:
        raise OIDCError("OIDC issuer URL is not set")
    hit = _cache.get("disc:" + iss)
    if hit and not force and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    url = iss.rstrip("/") + "/.well-known/openid-configuration"
    d = _get_json(url)
    for k in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if not d.get(k):
            raise OIDCError(f"Discovery document is missing '{k}'")
    if d["issuer"].rstrip("/") != iss.rstrip("/"):
        raise OIDCError(f"Issuer mismatch: configured {iss} but provider says {d['issuer']}")
    _cache["disc:" + iss] = (time.time(), d)
    return d


def _jwks(uri: str, force: bool = False) -> jwt.PyJWKSet:
    hit = _cache.get("jwks:" + uri)
    if hit and not force and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    ks = jwt.PyJWKSet.from_dict(_get_json(uri))
    _cache["jwks:" + uri] = (time.time(), ks)
    return ks


def redirect_uri(base: str) -> str:
    return base.rstrip("/") + "/auth/oidc/callback"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def start(base_url: str) -> tuple[str, dict]:
    """Returns (authorize URL, values to keep in the session)."""
    d = discovery()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    params = {
        "response_type": "code",
        "client_id": settings.get("OIDC_CLIENT_ID"),
        "redirect_uri": redirect_uri(base_url),
        "scope": settings.get("OIDC_SCOPES") or "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in d["authorization_endpoint"] else "?"
    return d["authorization_endpoint"] + sep + urlencode(params), {
        "state": state, "nonce": nonce, "verifier": verifier, "ts": int(time.time()), "redirect_uri": params["redirect_uri"]}


def finish(code: str, saved: dict) -> dict:
    """Exchange the code, verify the ID token and return merged claims."""
    d = discovery()
    cid, secret = settings.get("OIDC_CLIENT_ID"), settings.get("OIDC_CLIENT_SECRET")
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": saved["redirect_uri"],
            "code_verifier": saved["verifier"], "client_id": cid}
    auth = None
    methods = d.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if secret:
        if "client_secret_basic" in methods:
            auth = (cid, secret)
        else:
            data["client_secret"] = secret
    with httpx.Client(timeout=20) as c:
        r = c.post(d["token_endpoint"], data=data, auth=auth, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OIDCError(f"Token request failed (HTTP {r.status_code}): {r.text[:300]}")
    tok = r.json()
    id_token = tok.get("id_token")
    if not id_token:
        raise OIDCError("Provider returned no id_token (is the 'openid' scope set?)")
    claims = verify_id_token(id_token, d, saved["nonce"])
    # userinfo often carries groups/email that aren't in the ID token
    if d.get("userinfo_endpoint") and tok.get("access_token"):
        try:
            with httpx.Client(timeout=15) as c:
                ui = c.get(d["userinfo_endpoint"], headers={"Authorization": f"Bearer {tok['access_token']}"})
            if ui.status_code == 200:
                info = ui.json()
                if info.get("sub") == claims.get("sub"):
                    claims = {**info, **claims}
        except Exception:
            pass
    return claims


def verify_id_token(id_token: str, d: dict, nonce: str) -> dict:
    header = jwt.get_unverified_header(id_token)
    alg = header.get("alg", "RS256")
    allowed = d.get("id_token_signing_alg_values_supported") or ["RS256"]
    if alg == "none" or alg not in allowed or alg.startswith("HS"):
        raise OIDCError(f"Unsupported ID token algorithm {alg}")
    kid = header.get("kid")

    def find_key(force: bool):
        ks = _jwks(d["jwks_uri"], force)
        for k in ks.keys:
            if kid is None or k.key_id == kid:
                return k
        return None

    key = find_key(False) or find_key(True)  # refetch once on key rotation
    if key is None:
        raise OIDCError("No matching signing key in the provider's JWKS")
    try:
        claims = jwt.decode(id_token, key=key.key, algorithms=[alg], audience=settings.get("OIDC_CLIENT_ID"),
                            issuer=d["issuer"], leeway=120, options={"require": ["exp", "iat", "sub", "iss", "aud"]})
    except jwt.PyJWTError as exc:
        raise OIDCError(f"ID token rejected: {exc}")
    if claims.get("nonce") != nonce:
        raise OIDCError("ID token nonce mismatch")
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") not in (None, settings.get("OIDC_CLIENT_ID")):
        raise OIDCError("ID token azp mismatch")
    return claims


# ------------------------------------------------------------------ claims → user
def _claim(claims: dict, path: str):
    cur = claims
    for part in (path or "").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def groups_of(claims: dict) -> set[str]:
    g = _claim(claims, settings.get("OIDC_GROUPS_CLAIM") or "groups")
    if isinstance(g, str):
        g = re.split(r"[,\s]+", g)
    return {str(x).strip() for x in (g or []) if str(x).strip()}


def _csv(key: str) -> set[str]:
    return {x.strip() for x in (settings.get(key) or "").split(",") if x.strip()}


def _email(claims: dict) -> str | None:
    email = (claims.get("email") or "").strip().lower() or None
    return email


def _email_trusted(claims: dict) -> bool:
    v = claims.get("email_verified")
    if v is True or str(v).lower() == "true":
        return True
    return bool(settings.get("OIDC_ALLOW_UNVERIFIED_EMAIL"))


def _username_from(claims: dict) -> str:
    cand = _claim(claims, settings.get("OIDC_USERNAME_CLAIM") or "preferred_username") \
        or claims.get("preferred_username") or claims.get("email") or claims.get("sub")
    name = re.sub(r"[^A-Za-z0-9._@+-]", "", str(cand))[:70]
    return name or "user"


def provision(db: Session, claims: dict) -> User:
    """Find, link or create the FlipDesk user for these claims. Raises OIDCError if not allowed."""
    iss, sub = claims["iss"], str(claims["sub"])
    groups = groups_of(claims)
    allowed = _csv("OIDC_ALLOWED_GROUPS")
    if allowed and not (groups & allowed):
        raise OIDCError("Your account isn't in a group that's allowed to use FlipDesk.")

    user = db.query(User).filter(User.oidc_issuer == iss, User.oidc_sub == sub).first()
    email = _email(claims)
    if user is None and email and _email_trusted(claims) and settings.get("OIDC_LINK_BY_EMAIL"):
        user = db.query(User).filter(func.lower(User.email) == email, User.oidc_sub.is_(None)).first()
        if user:
            user.oidc_issuer, user.oidc_sub = iss, sub
    created = False
    if user is None:
        first_user = db.query(User.id).first() is None
        if not settings.get("OIDC_AUTO_CREATE") and not first_user:
            raise OIDCError("No FlipDesk account is linked to this SSO identity, and automatic account creation is off. Ask an admin to create your account (with the same email) first.")
        base = _username_from(claims)
        name, n = base, 2
        while db.query(User.id).filter(func.lower(User.username) == name.lower()).first():
            name = f"{base}{n}"
            n += 1
        admin_groups = _csv("OIDC_ADMIN_GROUPS")
        is_admin = first_user or (bool(groups & admin_groups) if admin_groups else bool(settings.get("OIDC_NEW_USERS_ADMIN")))
        user = User(username=name, password_hash="!", is_admin=is_admin, auth_source="oidc",
                    oidc_issuer=iss, oidc_sub=sub, is_active=True)
        db.add(user)
        created = True

    if not user.is_active:
        raise OIDCError("This FlipDesk account is disabled.")
    # keep profile in sync
    if email and _email_trusted(claims):
        user.email = email
    user.display_name = claims.get("name") or user.display_name
    admin_groups = _csv("OIDC_ADMIN_GROUPS")
    if admin_groups and not created:
        should = bool(groups & admin_groups)
        if user.is_admin and not should:
            # never strip the last admin
            others = db.query(User.id).filter(User.is_admin.is_(True), User.id != user.id, User.is_active.isnot(False)).count()
            if others:
                user.is_admin = False
        elif should:
            user.is_admin = True
    db.commit()
    db.refresh(user)
    user._just_created = created  # type: ignore[attr-defined]
    return user


def test_connection() -> list[str]:
    out = []
    d = discovery(force=True)
    out.append(f"Discovery OK — issuer {d['issuer']}")
    ks = _jwks(d["jwks_uri"], force=True)
    out.append(f"JWKS OK — {len(ks.keys)} signing key(s)")
    out.append("PKCE S256 supported" if "S256" in (d.get("code_challenge_methods_supported") or ["S256"]) else
               "Warning: provider doesn't advertise PKCE S256")
    return out


def summarize(claims: dict) -> str:
    keep = {k: claims.get(k) for k in ("sub", "preferred_username", "email", "email_verified", "name")}
    keep["groups"] = sorted(groups_of(claims))
    return json.dumps(keep)
