"""Progressive Web App: manifest, service worker and offline page.

Only static assets are cached. Pages are always fetched live because they hold
private financial data and per-session CSRF tokens; when the network is down a
small offline page is shown instead.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import config

router = APIRouter()
V = config.APP_VERSION

ICONS = [
    {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    {"src": "/static/icons/maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
    {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    {"src": "/static/icons/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
]


@router.get("/manifest.webmanifest")
def manifest():
    shortcut_icon = [{"src": "/static/icons/shortcut-96.png", "sizes": "96x96", "type": "image/png"}]
    data = {
        "id": "/",
        "name": config.APP_NAME,
        "short_name": config.APP_NAME,
        "description": "Inventory, profit and bookkeeping for eBay resellers",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "any",
        "background_color": "#f6f7f5",
        "theme_color": "#1f7a4d",
        "categories": ["business", "finance", "productivity"],
        "icons": ICONS,
        "shortcuts": [
            {"name": "Add item", "short_name": "Add item", "url": "/inventory/new?source=pwa", "icons": shortcut_icon},
            {"name": "Record a sale", "short_name": "Record sale", "url": "/sales/new?source=pwa", "icons": shortcut_icon},
            {"name": "Scan a shelf label", "short_name": "Scan shelf", "url": "/scan?source=pwa", "icons": shortcut_icon},
            {"name": "Inventory", "url": "/inventory?source=pwa", "icons": shortcut_icon},
            {"name": "Sales", "url": "/sales?source=pwa", "icons": shortcut_icon},
        ],
    }
    return JSONResponse(data, media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


SW = r"""// FlipDesk service worker — version __V__
const VERSION = 'flipdesk-__V__';
const STATIC_CACHE = VERSION + '-static';
const PRECACHE = [
  '/offline',
  '/static/app.css?v=__V__',
  '/static/app.js?v=__V__',
  '/static/chart.umd.min.js?v=__V__',
  '/static/icons/icon-192.png',
  '/static/icons/icon.svg',
  '/static/icons/favicon-32.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(STATIC_CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('flipdesk-') && k !== STATIC_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;                 // forms always go to the server
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;  // eBay images, IdP, etc.

  // Static assets: cache first, refresh in the background.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.open(STATIC_CACHE).then(async (cache) => {
        const hit = await cache.match(req);
        const net = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; }).catch(() => hit);
        return hit || net;
      })
    );
    return;
  }

  // Pages: always live (private data + CSRF tokens). Offline page if the network is down.
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match('/offline')));
  }
});
"""


@router.get("/sw.js")
def service_worker():
    return Response(SW.replace("__V__", V), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


OFFLINE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#1f7a4d"><title>Offline · __NAME__</title>
<link rel="stylesheet" href="/static/app.css?v=__V__"></head>
<body><div class="login" style="text-align:center">
<div class="brand" style="color:var(--ink);justify-content:center;padding:0 0 16px"><span class="dot">⇄</span>__NAME__</div>
<div class="card"><h2>You're offline</h2>
<p class="sub">__NAME__ needs a connection to your server to show inventory and sales. This page will reload automatically when you're back online.</p>
<button class="btn primary" onclick="location.reload()">Try again</button></div></div>
<script>addEventListener('online', () => location.reload());</script></body></html>"""


@router.get("/offline", response_class=HTMLResponse)
def offline():
    return HTMLResponse(OFFLINE.replace("__NAME__", config.APP_NAME).replace("__V__", V))


@router.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/icons/favicon-32.png", status_code=301)


@router.get("/apple-touch-icon.png")
@router.get("/apple-touch-icon-precomposed.png")
def apple_icon(request: Request):
    return RedirectResponse("/static/icons/apple-touch-icon.png", status_code=301)
