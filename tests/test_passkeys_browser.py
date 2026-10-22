"""Passkey end-to-end test in real Chromium with a virtual WebAuthn authenticator.

Requires Playwright + Chromium:  python tests/test_passkeys_browser.py
"""
import os, re, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright
S = tempfile.mkdtemp(); os.makedirs(f"{S}/pk"); os.makedirs(f"{S}/shots")
PORT = int(os.environ.get("PK_TEST_PORT", "8817")); B = f"http://localhost:{PORT}"
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
env = {**os.environ, "DATA_DIR": f"{S}/pk/data", "CONFIG_DIR": f"{S}/pk/config"}
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], env=env,
                       stdout=open(f"{S}/pk/srv.log", "w"), stderr=subprocess.STDOUT)
for _ in range(80):
    try: urllib.request.urlopen(B + "/health"); break
    except Exception: time.sleep(0.25)

def ok(cond, msg):
    print(("✓ " if cond else "✗ ") + msg)
    if not cond: raise SystemExit(1)

try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1200, "height": 900})
        pg = ctx.new_page()
        errs = []; pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        cdp = ctx.new_cdp_session(pg)
        cdp.send("WebAuthn.enable")
        auth_id = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
            "isUserVerified": True, "automaticPresenceSimulation": True}})["authenticatorId"]

        # first-run setup + password sign-in
        pg.goto(B + "/setup")
        pg.fill("input[name=username]", "admin"); pg.fill("input[name=password]", "password123"); pg.fill("input[name=password2]", "password123")
        pg.click("form button"); pg.wait_for_load_state("networkidle")

        # register a passkey
        pg.goto(B + "/account")
        ok(pg.locator("[data-passkey-add]").is_visible(), "Add passkey button visible (secure context, WebAuthn supported)")
        pg.fill("#pk_name", "Test laptop")
        pg.click("[data-passkey-add]"); pg.wait_for_url("**/account/recovery-codes"); 
        codes = re.findall(r"\b[a-z2-9]{4}-[a-z2-9]{4}\b", pg.inner_text("#codes"))
        ok(len(codes) == 10, "first passkey issues 10 recovery codes")
        creds = cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth_id})["credentials"]
        ok(len(creds) == 1 and creds[0]["isResidentCredential"], "authenticator holds 1 discoverable credential")
        pg.goto(B + "/account")
        ok("Test laptop" in pg.inner_text("#passkeys"), "passkey listed on My account")
        # adding the same authenticator again is refused (excludeCredentials)
        pg.click("[data-passkey-add]"); pg.wait_for_timeout(1500)
        ok(not pg.locator(".js-pk-error").is_hidden() and "already" in pg.inner_text(".js-pk-error"), "same device can't register twice: " + pg.inner_text(".js-pk-error"))

        # passwordless sign-in via autofill (conditional mediation) — the virtual authenticator picks immediately
        pg.goto(B + "/logout"); pg.goto(B + "/login"); pg.wait_for_url(B + "/", timeout=10000); pg.wait_for_load_state("networkidle")
        ok("Dashboard" in pg.inner_text("h1"), "autofill passkey sign-in (conditional UI) works")
        # from here on, turn autofill off so the explicit buttons are exercised
        ctx.add_init_script("PublicKeyCredential.isConditionalMediationAvailable = async () => false;")
        pg.goto(B + "/logout"); pg.goto(B + "/login")
        pg.click("[data-passkey-login]"); pg.wait_for_url(B + "/"); pg.wait_for_load_state("networkidle")
        ok("Dashboard" in pg.inner_text("h1"), "signed in with passkey button (no password, no code)")
        pg.goto(B + "/account")
        ok("signed in with a passkey" in pg.inner_text(".topbar"), "session method recorded as passkey")

        # password sign-in now needs a second step; passkey satisfies it
        pg.goto(B + "/logout"); pg.goto(B + "/login")
        pg.fill("input[name=username]", "admin"); pg.fill("input[name=password]", "password123"); pg.click("form button")
        pg.wait_for_url("**/login/mfa")
        ok(pg.locator("[data-passkey-login='mfa']").is_visible(), "password sign-in asks for second step with passkey option")
        ok(pg.locator("input[name=code]").count() == 0, "no authenticator-code box for a passkey-only account")
        pg.click("[data-passkey-login='mfa']"); pg.wait_for_url(B + "/")
        ok(True, "passkey accepted as second factor")

        # recovery code still works as fallback
        pg.goto(B + "/logout"); pg.goto(B + "/login")
        pg.fill("input[name=username]", "admin"); pg.fill("input[name=password]", "password123"); pg.click("form button")
        pg.wait_for_url("**/login/mfa"); pg.goto(B + "/login/mfa?recovery=1")
        pg.fill("input[name=code]", codes[0]); pg.click("form button"); pg.wait_for_load_state("networkidle")
        ok("/account" in pg.url, "recovery code works for passkey-only account")

        # sign count increments (clone detection data)
        c2 = cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth_id})["credentials"][0]
        ok(c2["signCount"] >= 2, f"authenticator sign count advanced ({c2['signCount']})")

        # MFA required: can't delete last passkey
        pg.goto(B + "/settings?tab=auth")
        pg.check("input[name=MFA_REQUIRED]"); pg.click("form button.primary"); pg.wait_for_load_state("networkidle")
        pg.goto(B + "/account")
        pg.on("dialog", lambda d: d.accept())
        pg.click("#passkeys form[action$='/delete'] button"); pg.wait_for_load_state("networkidle")
        ok("required" in pg.inner_text(".flash"), "last passkey can't be removed while MFA is required")

        # second user enrolls with a passkey when MFA is required
        pg.goto(B + "/settings?tab=users")
        pg.fill("form[action='/settings-users/add'] input[name=username]", "bob")
        pg.fill("form[action='/settings-users/add'] input[name=password]", "bobpassword1")
        pg.click("form[action='/settings-users/add'] button"); pg.wait_for_load_state("networkidle")
        bctx = b.new_context(); bp = bctx.new_page()
        bcdp = bctx.new_cdp_session(bp); bcdp.send("WebAuthn.enable")
        bcdp.send("WebAuthn.addVirtualAuthenticator", {"options": {"protocol": "ctap2", "transport": "internal",
                  "hasResidentKey": True, "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})
        bctx.add_init_script("PublicKeyCredential.isConditionalMediationAvailable = async () => false;")
        bp.goto(B + "/login"); bp.fill("input[name=username]", "bob"); bp.fill("input[name=password]", "bobpassword1"); bp.click("form button")
        bp.wait_for_url("**/account/mfa**")
        ok(bp.locator("[data-passkey-add]").is_visible(), "forced enrollment page offers a passkey")
        bp.click("[data-passkey-add]"); bp.wait_for_url("**/account/recovery-codes")
        bp.goto(B + "/inventory")
        ok("Inventory" in bp.inner_text("h1"), "after adding a passkey bob can use the app")
        # bob's passkey must not log in as admin and vice versa: bob's authenticator only has bob
        bp.goto(B + "/logout"); bp.goto(B + "/login"); bp.click("[data-passkey-login]"); bp.wait_for_url(B + "/")
        bp.goto(B + "/account")
        ok("bob" in bp.inner_text(".topbar"), "bob's passkey signs in as bob")

        # admin removes bob's passkeys → bob signed out
        pg.goto(B + "/settings?tab=users")
        pg.locator("tr", has_text="bob").locator("summary").click()
        pg.locator("tr", has_text="bob").locator("form[action$='/reset-passkeys'] button").click(); pg.wait_for_load_state("networkidle")
        bp.goto(B + "/inventory")
        ok("/login" in bp.url, "removing bob's passkeys signs him out")
        bp.goto(B + "/login"); bp.click("[data-passkey-login]"); bp.wait_for_timeout(1500)
        ok("isn't registered" in bp.inner_text(".js-pk-error"), "removed passkey can't sign in: " + bp.inner_text(".js-pk-error"))

        # cloned authenticator: same private key but an older signature counter → rejected
        cred = cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth_id})["credentials"][0]
        cctx = b.new_context(); cp = cctx.new_page(); ccdp = cctx.new_cdp_session(cp); ccdp.send("WebAuthn.enable")
        cctx.add_init_script("PublicKeyCredential.isConditionalMediationAvailable = async () => false;")
        cid = ccdp.send("WebAuthn.addVirtualAuthenticator", {"options": {"protocol": "ctap2", "transport": "internal",
              "hasResidentKey": True, "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})["authenticatorId"]
        clone = {k: cred[k] for k in ("credentialId", "isResidentCredential", "rpId", "privateKey", "userHandle")}
        clone["signCount"] = 1
        ccdp.send("WebAuthn.addCredential", {"authenticatorId": cid, "credential": clone})
        cp.goto(B + "/login"); cp.click("[data-passkey-login]"); cp.wait_for_timeout(1500)
        ok("/login" in cp.url and not cp.locator(".js-pk-error").is_hidden(), "cloned passkey with stale counter rejected: " + cp.inner_text(".js-pk-error")[:90])
        # API without CSRF header is refused
        r = cp.request.post(B + "/auth/passkey/options")
        ok(r.status == 400, "passkey API without CSRF token refused")

        # passkeys switched off by admin → hidden and refused
        pg.goto(B + "/settings?tab=auth"); pg.uncheck("input[name=PASSKEYS_ENABLED]"); pg.click("form button.primary"); pg.wait_for_load_state("networkidle")
        cp.goto(B + "/login")
        ok(cp.locator("[data-passkey-login]").count() == 0, "login page hides passkeys when disabled")
        tok = cp.locator("meta[name=csrf-token]").get_attribute("content")
        r = cp.request.post(B + "/auth/passkey/options", headers={"X-CSRF-Token": tok})
        ok(r.status == 403, "passkey sign-in API refused when disabled")
        pg.goto(B + "/settings?tab=auth"); pg.check("input[name=PASSKEYS_ENABLED]"); pg.click("form button.primary"); pg.wait_for_load_state("networkidle")

        pg.goto(B + "/account"); pg.screenshot(path=f"{S}/shots/pk-account.png", full_page=True)
        pg.goto(B + "/logout"); pg.goto(B + "/login"); pg.screenshot(path=f"{S}/shots/pk-login.png")
        print("console errors:", [e for e in errs if "Failed to load resource" not in e])
        print("PASSKEYS ALL GOOD")
        b.close()
finally:
    srv.terminate()
