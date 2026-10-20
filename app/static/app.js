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
