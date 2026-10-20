# FlipDesk

A self-hosted, web-based inventory and accounting application for eBay resellers: automatic eBay sync, real profit tracking, inventory, end-and-relist / repricing / delayed-promotion automations, error detection, expense and mileage ledger, income statement and a Schedule C–style tax report. One Docker container, your own eBay developer keys, your own database.

> This application uses the eBay API but is not endorsed or certified by eBay, Inc. The tax report organizes your records; it isn't tax advice.

## What it does

| Area | Features |
|---|---|
| **eBay sync** | Active listings (Trading API), orders with a 2-year backfill (Fulfillment API), fees, shipping labels, ad fees, credits and store subscriptions (Finances API). Runs on a schedule; “Sync now” buttons too. |
| **Profit** | Per-sale net profit = price + shipping − fees − ads − label − refunds − cost of goods. Margin, ROI, avg days to list / sell. |
| **Inventory** | Listed, unlisted (“death pile”) and sold items with cost, source, purchase date, storage location, photos. One item keeps its history across relisted item IDs (product timeline). Quick-edit grid for filling in costs fast. Auto-links when the eBay SKU matches. |
| **Bulk buys** | Track a lot / storage unit as one purchase, split the cost evenly or by value, watch ROI over time. |
| **Any marketplace** | Record Amazon, Poshmark, Mercari, Etsy, Facebook, in-person sales alongside eBay. |
| **Automations** | End & relist GTC listings just before the 30-day auto-renew; reprice by % per relist with a max reduction and per-item floor; delayed promotion into a Promoted Listings campaign after N days unsold. Dry-run mode on by default, failed relists retried automatically. |
| **Auto-reconciliation** | Cancellations, full and partial refunds applied to sales; optional restock. |
| **Error detection** | Duplicate shipping-label charges, label adjustments, fee credits, failed relists, sync and token problems. |
| **Alerts** | “Sold — it's on Shelf B3” alerts in-app and via ntfy, Discord, generic webhook (Home Assistant, n8n…) or email. |
| **Books** | Expense ledger by Schedule C category (with receipts), mileage log at the IRS rate (2026: 72.5¢), income statement by marketplace, tax report (accrual/inventory or cash) with guided data checks and CSV export. |
| **Data** | CSV import/export, full JSON export/restore (also migrates SQLite ⇄ PostgreSQL/MySQL), nightly backups. |

## Where data lives

Nothing stateful lives in the image. Destroying the container loses nothing.

| Host path | Container path | Contents |
|---|---|---|
| `./data` | `/data` | `flipdesk.db` (when using the built-in SQLite DB), `uploads/` (photos, receipts), `backups/` |
| `./config` | `/config` | `secret.key` (auto-generated — **back it up**, it encrypts stored eBay tokens/secrets), optional `config.yml` |
| external DB | — | All tables when `DB_TYPE=postgres` or `mysql` |

Settings precedence: **environment variable › `/config/config.yml` › value saved in the web UI › default.** Values set by env/file show as locked in the UI.

## Quick start (built-in SQLite)

```bash
mkdir -p data config
docker compose up -d --build        # uses docker-compose.yml
```

Open `http://<host>:8080`, create the admin account, then follow **eBay connection** in the sidebar.

External database instead: `docker compose -f docker-compose.postgres.yml up -d --build` (or `docker-compose.mariadb.yml`), or point at an existing server with `DB_TYPE`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` — or a single `DATABASE_URL` (`postgresql+psycopg://…` / `mysql+pymysql://…`). Tables are created and upgraded automatically.

Plain `docker run`:

```bash
docker build -t flipdesk .
docker run -d --name flipdesk -p 8080:8080 \
  -e PUBLIC_URL=https://flip.example.com -e TZ=America/New_York -e PUID=1000 -e PGID=1000 \
  -v /srv/flipdesk/data:/data -v /srv/flipdesk/config:/config \
  --restart unless-stopped flipdesk
```

## eBay developer setup (also walked through in the app)

1. **Developer account & keyset** — sign up at developer.ebay.com and create a Production keyset in Application Keys.
2. **Public HTTPS URL** — put FlipDesk behind your reverse proxy or a Cloudflare Tunnel and set `PUBLIC_URL`. Only `/ebay/*` and `/privacy` need to be internet-reachable.
3. **Marketplace Account Deletion** (required before eBay activates production keys) — in *Alerts & Notifications*, paste the endpoint `PUBLIC_URL/ebay/account-deletion` and the verification token shown in the app. FlipDesk answers eBay's SHA-256 challenge, verifies notification signatures, and redacts matching buyer data.
4. **RuName** — in *User Tokens → Get a Token from eBay via Your Application → Add eBay Redirect URL*, paste the privacy, accepted (`/ebay/callback`) and declined URLs from the app, tick *OAuth Enabled*, copy the RuName.
5. **Keys** — enter App ID, Cert ID and RuName in the app (or as `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_RUNAME`). Click *Test*.
6. **Connect** — click *Connect eBay*. If eBay can't redirect back to you, paste the URL from the browser address bar into the fallback box. The first sync backfills up to two years.

Scopes requested: `sell.inventory`, `sell.account`, `sell.fulfillment`, `sell.finances`, `sell.marketing`, `commerce.identity.readonly`.

## Automations — how to roll out safely

1. Leave **Dry-run** on, enable relist/reprice/promote, and watch the automation log for a few days.
2. Set per-item minimum prices or turn automation off for specific items on their edit page.
3. Turn dry-run off. Relists run every 20 minutes for listings inside the window; promotion runs every 6 hours.

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `PUBLIC_URL` | — | HTTPS address of the app (OAuth + notifications) |
| `DB_TYPE` | `sqlite` | `sqlite`, `postgres`, `mysql` |
| `DB_HOST` `DB_PORT` `DB_NAME` `DB_USER` `DB_PASSWORD` | — | external DB |
| `DATABASE_URL` | — | overrides `DB_*` |
| `EBAY_ENVIRONMENT` | `production` | or `sandbox` |
| `EBAY_CLIENT_ID` `EBAY_CLIENT_SECRET` `EBAY_RUNAME` `EBAY_MARKETPLACE_ID` | — | optional, otherwise set in the UI |
| `EBAY_VERIFICATION_TOKEN` | auto | account-deletion token |
| `SECRET_KEY` | `/config/secret.key` | session + encryption key |
| `PUID` / `PGID` | `1000` | owner of files written to the volumes |
| `TZ` | `UTC` | schedule time zone |
| `AUTH_MODE` | `local` | `none` only behind an authenticating proxy |
| `SESSION_COOKIE_SECURE` | `false` | `true` when served only over HTTPS |
| `PORT` | `8080` | |

Any setting in the UI can also be set by its key (e.g. `SYNC_ORDERS_MINUTES`, `NTFY_URL`, `RELIST_ENABLED`). See `config.example.yml`.

## Backups & migration

- Back up `./data` and `./config`. Nightly backups also land in `./data/backups` (SQLite copy, or JSON export for external DBs).
- Moving from SQLite to PostgreSQL: *Import · export · backup → Download full export*, restart the container with the Postgres settings, sign in to the new empty install, and *Restore* the export.

## Notes and limits

- Single app process by design (the scheduler runs in-process) — don't scale to multiple replicas.
- Finances API data for some EU/UK sellers requires eBay digital signatures, which this version doesn't implement; orders still sync, and fees fall back to the order's marketplace fee.
- Not a cross-lister

## Development

```bash
pip install -r requirements.txt
DATA_DIR=./data CONFIG_DIR=./config uvicorn app.main:app --reload --port 8080
python tests/test_app.py   # end-to-end test with a mocked eBay API
```
