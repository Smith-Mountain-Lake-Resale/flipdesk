"""Auth tests: TOTP enrollment/login/replay/recovery, MFA enforcement, admin gating,
session revocation, CLI, and OIDC against a real (fake) identity provider.

Run:  python tests/test_auth.py
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

if "DATA_DIR" not in os.environ:
    _t = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = _t + "/data"
    os.environ["CONFIG_DIR"] = _t + "/config"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
import jwt  # noqa: E402
import pyotp  # noqa: E402
import uvicorn  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.responses import JSONResponse, RedirectResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ----------------------------------------------------------------- fake IdP
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ROGUE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PORT = (lambda s: (s.bind(("127.0.0.1", 0)), s.getsockname()[1], s.close())[1])(socket.socket())
ISS = f"http://127.0.0.1:{PORT}"
CLIENT_ID, CLIENT_SECRET = "flipdesk", "s3cret"
IDP = {"profile": {}, "codes": {}, "mode": "ok"}
idp = FastAPI()


def _jwk(pub):
    n = pub.public_numbers()
    b = lambda i: base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()  # noqa: E731
    return {"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256", "n": b(n.n), "e": b(n.e)}


@idp.get("/.well-known/openid-configuration")
def disc():
    return {"issuer": ISS, "authorization_endpoint": f"{ISS}/authorize", "token_endpoint": f"{ISS}/token",
            "userinfo_endpoint": f"{ISS}/userinfo", "jwks_uri": f"{ISS}/jwks",
            "id_token_signing_alg_values_supported": ["RS256"], "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"]}


@idp.get("/jwks")
def jwks():
    return {"keys": [_jwk(KEY.public_key())]}


@idp.get("/authorize")
def authorize(request: Request):
    q = request.query_params
    assert q["client_id"] == CLIENT_ID and q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    code = secrets.token_urlsafe(12)
    IDP["codes"][code] = {"nonce": q["nonce"], "challenge": q["code_challenge"], "redirect": q["redirect_uri"],
                          "profile": dict(IDP["profile"])}
    return RedirectResponse(q["redirect_uri"] + "?" + urlencode({"code": code, "state": q["state"]}), status_code=302)


@idp.post("/token")
def token(request: Request, grant_type: str = Form(), code: str = Form(), redirect_uri: str = Form(),
          code_verifier: str = Form(), client_id: str = Form(None)):
    auth = request.headers.get("authorization", "")
    assert auth == "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode(), "client auth"
    c = IDP["codes"].pop(code, None)
    if not c or redirect_uri != c["redirect"]:
        return JSONResponse({"error": "invalid_grant"}, 400)
    chal = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    if chal != c["challenge"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"}, 400)
    p = c["profile"]
    now = int(time.time())
    claims = {"iss": ISS, "aud": CLIENT_ID, "sub": p["sub"], "iat": now, "exp": now + 300,
              "nonce": "WRONG" if IDP["mode"] == "bad_nonce" else c["nonce"],
              **{k: v for k, v in p.items() if k not in ("sub", "groups")}}
    signer = ROGUE if IDP["mode"] == "bad_sig" else KEY
    idt = jwt.encode(claims, signer, algorithm="RS256", headers={"kid": "k1"})
    IDP["last_access"] = p
    return {"access_token": "at-" + p["sub"], "token_type": "Bearer", "id_token": idt, "expires_in": 300}


@idp.get("/userinfo")
def userinfo(request: Request):
    p = IDP["last_access"]
    return {"sub": p["sub"], "groups": p.get("groups", []), "email": p.get("email")}


def run_idp():
    uvicorn.Server(uvicorn.Config(idp, host="127.0.0.1", port=PORT, log_level="warning")).run()


# ----------------------------------------------------------------- app
from app.main import app  # noqa: E402


def tok(c, url):
    r = c.get(url)
    m = re.search(r'name="_csrf" value="([^"]+)"', r.text)
    assert m, (url, r.status_code, r.text[:400])
    return m.group(1)


def post(c, url, data, from_url=None, **kw):
    return c.post(url, data={**data, "_csrf": tok(c, from_url or url)}, follow_redirects=kw.get("follow", True))


def login(c, user, pw):
    return post(c, "/login", {"username": user, "password": pw}, "/login?local=1")


def oidc_login(c, profile, mode="ok"):
    IDP["profile"], IDP["mode"] = profile, mode
    r = c.get("/auth/oidc/login", follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    auth_url = r.headers["location"]
    assert auth_url.startswith(ISS + "/authorize?")
    q = parse_qs(urlparse(auth_url).query)
    assert q["code_challenge_method"] == ["S256"] and q["nonce"] and q["state"]
    r2 = httpx.get(auth_url, follow_redirects=False)        # the browser visiting the IdP
    cb = urlparse(r2.headers["location"])
    return c.get(cb.path + "?" + cb.query)                   # the IdP redirecting back


def code_at(secret, offset_steps=0):
    return pyotp.TOTP(secret).at(time.time() + 30 * offset_steps)


def main():
    threading.Thread(target=run_idp, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get(f"{ISS}/jwks")
            break
        except Exception:
            time.sleep(0.1)

    from app import settings
    from app.db import SessionLocal
    from app.models import User

    with TestClient(app) as admin:
        # ---------------- first run + TOTP enrollment
        post(admin, "/setup", {"username": "admin", "email": "admin@example.com", "password": "password123", "password2": "password123"})
        page = admin.get("/account/mfa").text
        secret = re.search(r'id="totp_secret" value="([A-Z2-7]+)"', page).group(1)
        assert "<svg" in page and "otpauth://totp/" in page
        r = post(admin, "/account/mfa", {"code": "000000"}, "/account/mfa")
        assert "didn" in r.text  # wrong code rejected
        first_code = code_at(secret)
        r = post(admin, "/account/mfa", {"code": first_code}, "/account/mfa")
        codes = re.findall(r"\b[a-z2-9]{4}-[a-z2-9]{4}\b", r.text.split('id="codes"')[1].split('>', 1)[1].split("</pre>")[0])
        assert len(codes) == 10, (codes, r.text.split('id="codes"')[1][:600])
        print("✓ TOTP enrolled, 10 recovery codes")

        # ---------------- login requires TOTP; replay rejected; recovery works once
        admin.get("/logout")
        r = login(admin, "admin", "password123")
        assert "/login/mfa" in str(r.url), r.url
        assert admin.get("/", follow_redirects=False).status_code == 303  # not signed in yet
        r = post(admin, "/login/mfa", {"code": first_code, "mode": "totp"}, "/login/mfa")
        assert "didn" in r.text, ("replayed code must fail", str(r.url), re.findall(r"<h[12]>[^<]*", r.text))
        r = post(admin, "/login/mfa", {"code": code_at(secret, 1), "mode": "totp"}, "/login/mfa")
        assert "Dashboard" in r.text, r.text[:300]
        print("✓ TOTP login works, replayed code rejected")
        admin.get("/logout")
        login(admin, "admin", "password123")
        r = post(admin, "/login/mfa", {"code": codes[0].upper(), "mode": "recovery"}, "/login/mfa?recovery=1")
        assert "recovery code" in r.text and "9 left" in r.text, r.text[:400]
        admin.get("/logout")
        login(admin, "admin", "password123")
        r = post(admin, "/login/mfa", {"code": codes[0], "mode": "recovery"}, "/login/mfa?recovery=1")
        assert "didn" in r.text
        r = post(admin, "/login/mfa", {"code": codes[1], "mode": "recovery"}, "/login/mfa?recovery=1")
        assert "8 left" in r.text
        print("✓ recovery codes single-use")

        # ---------------- brute force throttle on TOTP
        admin.get("/logout")
        login(admin, "admin", "password123")
        for _ in range(5):
            post(admin, "/login/mfa", {"code": "123456", "mode": "totp"}, "/login/mfa")
        r = post(admin, "/login/mfa", {"code": code_at(secret, 0), "mode": "totp"}, "/login/mfa")
        assert "Too many" in r.text
        from app.services import authn
        authn.mfa_throttle._hits.clear()
        time.sleep(31 - time.time() % 30)  # move on a window: the earlier future-window code is now "used"
        r = post(admin, "/login/mfa", {"code": code_at(secret), "mode": "totp"}, "/login/mfa")
        assert "didn" in r.text, "code from an already-used window is a replay"
        r = post(admin, "/login/mfa", {"code": code_at(secret, 1), "mode": "totp"}, "/login/mfa")
        assert "Dashboard" in r.text
        print("✓ TOTP brute-force throttle")

        # ---------------- password throttle
        with TestClient(app) as bad:
            for _ in range(10):
                login(bad, "admin", "wrong-password")
            r = login(bad, "admin", "password123")
            assert "Too many failed" in r.text
            authn.login_throttle._hits.clear()
        print("✓ password brute-force throttle")

        # ---------------- users, admin gating, MFA required
        r = post(admin, "/settings-users/add", {"username": "bob", "password": "bobpassword", "email": "bob@example.com"}, "/settings?tab=users")
        assert "User bob added" in r.text
        post(admin, "/settings/auth", {"MFA_REQUIRED": "on", "MFA_ISSUER_NAME": "FlipDesk"}, "/settings?tab=auth")
        assert settings.get("MFA_REQUIRED") is True
        with TestClient(app) as bob:
            r = login(bob, "bob", "bobpassword")
            assert "/account/mfa" in str(r.url) and "requires two-factor" in r.text, r.url
            r = bob.get("/inventory", follow_redirects=False)
            assert r.headers["location"].startswith("/account/mfa"), "must enroll before using app"
            s2 = re.search(r'id="totp_secret" value="([A-Z2-7]+)"', bob.get("/account/mfa").text).group(1)
            post(bob, "/account/mfa", {"code": code_at(s2)}, "/account/mfa")
            assert bob.get("/inventory").status_code == 200
            r = bob.get("/settings")
            assert r.status_code == 403, "non-admin blocked from settings"
            assert bob.get("/data").status_code == 403
            print("✓ MFA enforcement forces enrollment; non-admin blocked from admin areas")
            # admin disables bob → bob's session dies immediately
            with SessionLocal() as db:
                bid = db.query(User).filter_by(username="bob").one().id
            post(admin, f"/settings-users/{bid}/toggle-active", {}, "/settings?tab=users")
            assert bob.get("/inventory", follow_redirects=False).status_code == 303
            post(admin, f"/settings-users/{bid}/toggle-active", {}, "/settings?tab=users")
            print("✓ disabling a user revokes their session")

        # ---------------- CLI break-glass
        from app import cli
        cli.main(["reset-mfa", "bob"])
        cli.main(["set", "mfa_required", "false"])
        settings.invalidate()
        assert settings.get("MFA_REQUIRED") is False
        with SessionLocal() as db:
            assert db.query(User).filter_by(username="bob").one().totp_enabled is False
        print("✓ CLI reset-mfa / set")

        # ---------------- OIDC
        post(admin, "/settings/oidc", {"OIDC_ENABLED": "on", "OIDC_DISPLAY_NAME": "Authentik", "OIDC_ISSUER": ISS,
                                       "OIDC_CLIENT_ID": CLIENT_ID, "OIDC_CLIENT_SECRET": CLIENT_SECRET,
                                       "OIDC_SCOPES": "openid email profile groups", "OIDC_USERNAME_CLAIM": "preferred_username",
                                       "OIDC_GROUPS_CLAIM": "groups", "OIDC_AUTO_CREATE": "on",
                                       "OIDC_ALLOWED_GROUPS": "flip-users,flip-admins", "OIDC_ADMIN_GROUPS": "flip-admins",
                                       "OIDC_LINK_BY_EMAIL": "on", "OIDC_DISABLE_PASSWORD_LOGIN": "on"}, "/settings?tab=oidc")
        assert settings.get("OIDC_DISABLE_PASSWORD_LOGIN") is False, "can't disable passwords before proving SSO"
        r = post(admin, "/settings-oidc-test", {}, "/settings?tab=oidc")
        assert "Discovery OK" in r.text and "JWKS OK" in r.text, r.text[:500]
        assert "Sign in with Authentik" in TestClient(app).get("/login").text
        print("✓ OIDC settings + discovery test")

    with TestClient(app) as alice:
        r = oidc_login(alice, {"sub": "u-alice", "preferred_username": "alice", "email": "alice@example.com",
                               "email_verified": True, "name": "Alice A", "groups": ["flip-users"]})
        assert "Welcome, Alice A" in r.text, r.text[:600]
        assert alice.get("/settings").status_code == 403
        with SessionLocal() as db:
            a = db.query(User).filter_by(oidc_sub="u-alice").one()
            assert a.username == "alice" and not a.is_admin and a.auth_source == "oidc" and not a.has_password
        print("✓ OIDC auto-creates user (non-admin)")
        # promoted via admin group on next login
        alice.get("/logout")
        oidc_login(alice, {"sub": "u-alice", "preferred_username": "alice", "email": "alice@example.com",
                           "email_verified": True, "groups": ["flip-admins"]})
        assert alice.get("/settings").status_code == 200
        print("✓ OIDC admin group grants admin")
        # now SSO is proven, alice (admin via SSO) may disable password login
        post(alice, "/settings/oidc", {"OIDC_ENABLED": "on", "OIDC_DISPLAY_NAME": "Authentik", "OIDC_ISSUER": ISS,
                                       "OIDC_CLIENT_ID": CLIENT_ID, "OIDC_SCOPES": "openid email profile groups",
                                       "OIDC_USERNAME_CLAIM": "preferred_username", "OIDC_GROUPS_CLAIM": "groups",
                                       "OIDC_AUTO_CREATE": "on", "OIDC_ALLOWED_GROUPS": "flip-users,flip-admins",
                                       "OIDC_ADMIN_GROUPS": "flip-admins", "OIDC_LINK_BY_EMAIL": "on",
                                       "OIDC_DISABLE_PASSWORD_LOGIN": "on"}, "/settings?tab=oidc")
        assert settings.get("OIDC_DISABLE_PASSWORD_LOGIN") is True
        assert settings.get("OIDC_CLIENT_SECRET") == CLIENT_SECRET, "blank secret keeps existing"

    with TestClient(app) as x:
        assert 'name="password"' not in x.get("/login?local=1").text
        with SessionLocal() as db:
            settings.set_one(db, "OIDC_DISABLE_PASSWORD_LOGIN", "false")
            t = tok(x, "/login?local=1")
            settings.set_one(db, "OIDC_DISABLE_PASSWORD_LOGIN", "true")
        r = x.post("/login", data={"username": "admin", "password": "password123", "_csrf": t})
        assert r.status_code == 403 and "disabled" in r.text.lower(), "server refuses direct password POST"
    settings.set_one(SessionLocal(), "OIDC_DISABLE_PASSWORD_LOGIN", "false")
    print("✓ password login can be disabled")

    with TestClient(app) as eve:
        r = oidc_login(eve, {"sub": "u-eve", "preferred_username": "eve", "groups": ["outsiders"]})
        assert "allowed to use FlipDesk" in r.text
        r = oidc_login(eve, {"sub": "u-eve", "preferred_username": "eve", "groups": ["flip-users"]}, mode="bad_sig")
        assert "ID token rejected" in r.text or "Signature" in r.text, r.text[:400]
        r = oidc_login(eve, {"sub": "u-eve", "preferred_username": "eve", "groups": ["flip-users"]}, mode="bad_nonce")
        assert "nonce" in r.text
        # forged callback with no saved state
        r = eve.get("/auth/oidc/callback?code=abc&state=forged")
        assert "expired or didn" in r.text
        with SessionLocal() as db:
            assert db.query(User).filter_by(oidc_sub="u-eve").count() == 0
        print("✓ OIDC rejects wrong group, bad signature, bad nonce, forged state")

    with TestClient(app) as bob:
        # link by verified email to existing local user bob
        r = oidc_login(bob, {"sub": "u-bob", "preferred_username": "robert", "email": "BOB@example.com",
                             "email_verified": True, "groups": ["flip-users"]})
        with SessionLocal() as db:
            b = db.query(User).filter_by(username="bob").one()
            assert b.oidc_sub == "u-bob" and b.has_password
            assert db.query(User).filter_by(username="robert").count() == 0
        print("✓ OIDC links to existing user by verified email")

    # unverified email must NOT link; auto-create off rejects strangers
    with SessionLocal() as db:
        db.add(User(username="carol", password_hash="!", email="carol@example.com", is_admin=False))
        db.commit()
        settings.set_one(db, "OIDC_AUTO_CREATE", "false")
    with TestClient(app) as c:
        r = oidc_login(c, {"sub": "u-carol-fake", "preferred_username": "carol", "email": "carol@example.com",
                           "email_verified": False, "groups": ["flip-users"]})
        assert "automatic account creation is off" in r.text
        r = oidc_login(c, {"sub": "u-carol", "preferred_username": "carol", "email": "carol@example.com",
                           "email_verified": True, "groups": ["flip-users"]})
        assert c.get("/inventory").status_code == 200
        print("✓ unverified email not trusted; SSO-only pre-created user linked; auto-create off enforced")

    # MFA for OIDC users
    with SessionLocal() as db:
        settings.set_many(db, {"MFA_REQUIRED": "true", "MFA_REQUIRED_FOR_OIDC": "false", "OIDC_AUTO_CREATE": "true"})
    with TestClient(app) as c:
        oidc_login(c, {"sub": "u-dan", "preferred_username": "dan", "groups": ["flip-users"]})
        assert c.get("/inventory").status_code == 200, "IdP-trusted: no FlipDesk enrollment"
    with SessionLocal() as db:
        settings.set_one(db, "MFA_REQUIRED_FOR_OIDC", "true")
    with TestClient(app) as c:
        r = oidc_login(c, {"sub": "u-dan", "preferred_username": "dan", "groups": ["flip-users"]})
        assert "/account/mfa" in str(r.url)
        print("✓ MFA_REQUIRED_FOR_OIDC respected")

    # username collision gets a suffix
    with TestClient(app) as c:
        with SessionLocal() as db:
            settings.set_one(db, "MFA_REQUIRED", "false")
        oidc_login(c, {"sub": "u-other-admin", "preferred_username": "admin", "groups": ["flip-users"]})
        with SessionLocal() as db:
            assert db.query(User).filter_by(oidc_sub="u-other-admin").one().username == "admin2"
        print("✓ username collisions get a suffix")

    # pages render for admin
    with TestClient(app) as c:
        oidc_login(c, {"sub": "u-alice", "preferred_username": "alice", "email": "alice@example.com",
                       "email_verified": True, "groups": ["flip-admins"]})
        for u in ["/account", "/settings?tab=users", "/settings?tab=auth", "/settings?tab=oidc", "/"]:
            assert c.get(u).status_code == 200, u
    print("AUTH ALL GOOD")


if __name__ == "__main__":
    main()
