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
