// Copy-to-clipboard buttons, confirm prompts, and shared chart helpers.
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-copy]');
  if (b) {
    const el = document.getElementById(b.dataset.copy);
    const val = el.value !== undefined ? el.value : el.textContent;
    navigator.clipboard.writeText(val).then(() => { const t = b.textContent; b.textContent = 'Copied'; setTimeout(() => b.textContent = t, 1200); });
  }
});
document.addEventListener('submit', (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

window.FD = {
  css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); },
  money(v) { const s = (window.FD_SYM || '$'); return (v < 0 ? '-' : '') + s + Math.abs(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}); },
  base() {
    const ink = this.css('--muted'), line = this.css('--line');
    Chart.defaults.color = ink; Chart.defaults.borderColor = line;
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.plugins.legend.labels.boxWidth = 12;
    Chart.defaults.maintainAspectRatio = false;
  },
  palette() { return [this.css('--accent'), this.css('--info'), this.css('--warn'), '#8a63d2', '#c2477a', '#3aa5a5', '#7d8b44', '#a0603a']; },
};

// ---------- mobile navigation drawer ----------
document.addEventListener('click', (e) => {
  if (e.target.closest('[data-nav-toggle]')) { document.body.classList.toggle('nav-open'); return; }
  if (document.body.classList.contains('nav-open') && e.target.closest('.side a')) document.body.classList.remove('nav-open');
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') document.body.classList.remove('nav-open'); });

// ---------- PWA: service worker + install ----------
if ('serviceWorker' in navigator && (location.protocol === 'https:' || ['localhost', '127.0.0.1'].includes(location.hostname))) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {}));
}
(() => {
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const ua = navigator.userAgent;
  const isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  let deferred = null;
  const buttons = () => document.querySelectorAll('.js-install');
  const show = (on) => buttons().forEach((b) => { b.hidden = !on; });
  const status = document.querySelector('.js-install-status');
  if (standalone) { if (status) status.textContent = 'You are using the installed app.'; return; }

  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); deferred = e; show(true); });
  window.addEventListener('appinstalled', () => { deferred = null; show(false); if (status) status.textContent = 'Installed — open it from your home screen or app list.'; });
  if (isIOS) show(true);  // Safari has no install prompt; we show instructions instead

  function hint(html) {
    document.querySelector('.install-hint')?.remove();
    const d = document.createElement('div');
    d.className = 'install-hint';
    d.innerHTML = html + '<div style="text-align:right;margin-top:10px"><button class="btn sm" type="button">Got it</button></div>';
    d.querySelector('button').onclick = () => d.remove();
    document.body.appendChild(d);
  }
  document.addEventListener('click', async (e) => {
    const b = e.target.closest('.js-install');
    if (!b) return;
    e.preventDefault();
    document.body.classList.remove('nav-open');
    if (deferred) {
      deferred.prompt();
      const choice = await deferred.userChoice.catch(() => null);
      if (choice && choice.outcome === 'accepted') show(false);
      deferred = null;
    } else if (isIOS) {
      hint('<b>Install on iPhone / iPad</b><br>Tap the <b>Share</b> button in Safari (the square with an arrow), then choose <b>Add to Home Screen</b>.');
    } else {
      hint('<b>Install this app</b><br>Open your browser menu and choose <b>Install app</b> or <b>Add to Home screen</b>. Installing needs the site to be opened over HTTPS.');
    }
  });
})();

// ---------- Passkeys (WebAuthn) ----------
window.Passkeys = (() => {
  const enc = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const dec = (s) => { s = s.replace(/-/g, '+').replace(/_/g, '/'); while (s.length % 4) s += '='; return Uint8Array.from(atob(s), (c) => c.charCodeAt(0)).buffer; };
  const supported = () => !!(window.PublicKeyCredential && navigator.credentials && window.isSecureContext);
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
  async function post(url, body) {
    const r = await fetch(url, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() }, body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('Request failed (' + r.status + ')'));
    return j;
  }
  const toCreate = (o) => { o.challenge = dec(o.challenge); o.user.id = dec(o.user.id); (o.excludeCredentials || []).forEach((c) => { c.id = dec(c.id); }); return o; };
  const toGet = (o) => { o.challenge = dec(o.challenge); (o.allowCredentials || []).forEach((c) => { c.id = dec(c.id); }); return o; };
  function serialize(c) {
    const r = c.response;
    const out = { id: c.id, rawId: enc(c.rawId), type: c.type, authenticatorAttachment: c.authenticatorAttachment || null,
      clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {}, response: { clientDataJSON: enc(r.clientDataJSON) } };
    if (r.attestationObject) { out.response.attestationObject = enc(r.attestationObject); out.response.transports = r.getTransports ? r.getTransports() : []; }
    if (r.authenticatorData) { out.response.authenticatorData = enc(r.authenticatorData); out.response.signature = enc(r.signature); out.response.userHandle = r.userHandle ? enc(r.userHandle) : null; }
    return out;
  }
  function friendly(err) {
    if (!err) return 'Something went wrong.';
    if (err.name === 'NotAllowedError') return 'Passkey request was cancelled or timed out.';
    if (err.name === 'InvalidStateError') return 'This device already has a passkey for your account.';
    if (err.name === 'SecurityError') return 'Passkeys need this site opened at its HTTPS address (check Public URL).';
    return err.message || String(err);
  }
  let ac = null;
  async function signIn({ step = false, next = '/', conditional = false } = {}) {
    if (ac) ac.abort('restart');
    ac = new AbortController();
    const opts = await post('/auth/passkey/options' + (step ? '?step=mfa' : ''));
    const cred = await navigator.credentials.get({ publicKey: toGet(opts), signal: ac.signal, ...(conditional ? { mediation: 'conditional' } : {}) });
    return post('/auth/passkey/verify', { credential: serialize(cred), next });
  }
  async function register(name) {
    const opts = await post('/account/passkeys/options');
    const cred = await navigator.credentials.create({ publicKey: toCreate(opts) });
    return post('/account/passkeys/register', { credential: serialize(cred), name });
  }
  return { supported, signIn, register, friendly };
})();

document.addEventListener('DOMContentLoaded', () => {
  const ok = Passkeys.supported();
  document.querySelectorAll('.js-pk').forEach((el) => { el.hidden = !ok; });
  document.querySelectorAll('.js-pk-unsupported').forEach((el) => { el.hidden = ok; });
  if (!ok) return;
  const showErr = (msg) => { const e = document.querySelector('.js-pk-error'); if (e) { e.textContent = msg; e.hidden = !msg; } };
  const go = (res) => { if (res && res.redirect) location.href = res.redirect; };

  document.querySelectorAll('[data-passkey-login]').forEach((btn) => btn.addEventListener('click', async () => {
    showErr(''); btn.disabled = true;
    try { go(await Passkeys.signIn({ next: btn.dataset.next || '/', step: btn.dataset.passkeyLogin === 'mfa' })); }
    catch (e) { if (e.name !== 'AbortError') showErr(Passkeys.friendly(e)); }
    finally { btn.disabled = false; }
  }));

  // Autofill: offer passkeys in the username field's suggestions (Chrome, Safari, Edge).
  const auto = document.querySelector('input[autocomplete~="webauthn"]');
  if (auto && PublicKeyCredential.isConditionalMediationAvailable) {
    PublicKeyCredential.isConditionalMediationAvailable().then((yes) => {
      if (!yes) return;
      Passkeys.signIn({ conditional: true, next: auto.dataset.next || '/' }).then(go)
        .catch((e) => { if (e.name !== 'AbortError' && e !== 'restart' && e.name !== 'NotAllowedError') showErr(Passkeys.friendly(e)); });
    });
  }

  document.querySelectorAll('[data-passkey-add]').forEach((btn) => btn.addEventListener('click', async () => {
    showErr(''); btn.disabled = true;
    const name = (document.querySelector('#pk_name') || {}).value || '';
    try { go(await Passkeys.register(name)); }
    catch (e) { showErr(Passkeys.friendly(e)); }
    finally { btn.disabled = false; }
  }));
});
