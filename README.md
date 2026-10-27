<img width="128" height="128" alt="swap-logo-128x128" src="https://github.com/user-attachments/assets/8e26c77a-9351-40f1-b91b-9570ab04b78e" />

# FlipDesk 

A self-hosted, web-based inventory and accounting application for eBay resellers: automatic eBay sync, real profit tracking, inventory, end-and-relist / repricing / delayed-promotion automations, error detection, expense and mileage ledger, income statement and a Schedule C–style tax report. One Docker container, your own eBay developer keys, your own database.

> This application uses the eBay API but is not endorsed or certified by eBay, Inc. The tax report organizes your records; it isn't tax advice.

<img width="1906" height="714" alt="image" src="https://github.com/user-attachments/assets/b0378289-c277-47d4-bdb2-32a730f1598f" />


<img width="128" height="128" alt="swap-logo-128x128" src="https://github.com/user-attachments/assets/8e26c77a-9351-40f1-b91b-9570ab04b78e" />


## Install with the prebuilt Docker image (recommended)

Ready-made images are published to the GitHub Container Registry with every release — no need to clone the repo or build anything:

**[`ghcr.io/smith-mountain-lake-resale/flipdesk`](https://github.com/Smith-Mountain-Lake-Resale/flipdesk/pkgs/container/flipdesk)**

Images are tagged by release version (`v1.5.0`, `v1.4.0`, …); there is no `latest` tag, so always name a version. The examples below use `v1.5.0` — check the [package page](https://github.com/Smith-Mountain-Lake-Resale/flipdesk/pkgs/container/flipdesk) for the newest release.

### Docker Compose (built-in SQLite)

Create a folder with this `docker-compose.yml`:

```yaml
services:
  flipdesk:
    image: ghcr.io/smith-mountain-lake-resale/flipdesk:v1.5.0
    container_name: flipdesk
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      TZ: America/New_York
      PUID: 1000              # owner of files written to ./data and ./config
      PGID: 1000
      PUBLIC_URL: https://flip.example.com   # the HTTPS address eBay will redirect to
    volumes:
      - ./data:/data          # database, photos, receipts, backups
      - ./config:/config      # config.yml (optional) + secret.key (auto-generated, keep it!)
```

Then start it:

```bash
mkdir -p data config
docker compose pull
docker compose up -d
```

Open `http://<host>:8080`, create the admin account, then follow **eBay connection** in the sidebar.

### Plain `docker run`

```bash
docker run -d --name flipdesk -p 8080:8080 \
  -e PUBLIC_URL=https://flip.example.com -e TZ=America/New_York -e PUID=1000 -e PGID=1000 \
  -v /srv/flipdesk/data:/data -v /srv/flipdesk/config:/config \
  --restart unless-stopped ghcr.io/smith-mountain-lake-resale/flipdesk:v1.5.0
```

### PostgreSQL or MariaDB/MySQL

The bundled `docker-compose.postgres.yml` and `docker-compose.mariadb.yml` work with the prebuilt image too: replace `build: .` and `image: flipdesk:latest` in the `flipdesk` service with `image: ghcr.io/smith-mountain-lake-resale/flipdesk:v1.5.0`, then run `docker compose -f docker-compose.postgres.yml up -d`. All the other settings in [Environment variables](#environment-variables) apply unchanged.

### Updating

Change the version tag in your `docker-compose.yml` (or `docker run` command) to the new release, for example `v1.5.0` → `v1.6.0`, then:

```bash
docker compose pull
docker compose up -d
```

Your data lives in `./data`, `./config` (and your external database, if used), so replacing the container loses nothing. Tables are upgraded automatically on start. Take a backup (*Import · export · backup → Download full export*) before jumping several versions.


## What it does

| Area | Features |
|---|---|
| **eBay sync** | Active listings (Trading API), orders with a 2-year backfill (Fulfillment API), fees, shipping labels, ad fees, credits and store subscriptions (Finances API). Runs on a schedule; “Sync now” buttons too. |
| **Profit** | Per-sale net profit = price + shipping − fees − ads − label − refunds − cost of goods. Margin, ROI, avg days to list / sell. |
| **Inventory** | Listed, unlisted (“death pile”) and sold items with cost, source, purchase date, storage location, photos. One item keeps its history across relisted item IDs (product timeline). Quick-edit grid for filling in costs fast. Auto-links when the eBay SKU matches. |
| **Shelf labels & scanning** | Locations (shelves, bins, totes) with printable QR labels — 4, 6 or 8 per 4×6 thermal label with cut lines, one big label per 4×6, small-roll labels (4×2, 2.25×1.25, 2×1) or Letter sheets — each with the location name and code in plain text. Scan a label with the in-app camera scanner or your phone's camera to see what's on that shelf, put items away, or set an item's location. |
| **Inventory reconciliation** | Printable count sheet (PDF) of what's listed on eBay — item, SKU/eBay #, location, expected qty, tick box, counted-qty box and notes — grouped shelf by shelf so you can confirm everything is really there. |
| **Bulk buys** | Track a lot / storage unit as one purchase, split the cost evenly or by value, watch ROI over time. |
| **Any marketplace** | Record Amazon, Poshmark, Mercari, Etsy, Facebook, in-person sales alongside eBay. |
| **Automations** | End & relist GTC listings just before the 30-day auto-renew; reprice by % per relist with a max reduction and per-item floor; delayed promotion into a Promoted Listings campaign after N days unsold. Dry-run mode on by default, failed relists retried automatically. |
| **Auto-reconciliation** | Cancellations, full and partial refunds applied to sales; optional restock. |
| **Error detection** | Duplicate shipping-label charges, label adjustments, fee credits, failed relists, sync and token problems. |
| **Alerts** | “Sold — it's on Shelf B3” alerts in-app and via ntfy, Discord, generic webhook (Home Assistant, n8n…) or email. |
| **Books** | Expense ledger by Schedule C category (with receipts), mileage log at the IRS rate (2026: 72.5¢), income statement by marketplace, tax report (accrual/inventory or cash) with guided data checks and CSV export. |
| **Data** | CSV import/export, full JSON export/restore (also migrates SQLite ⇄ PostgreSQL/MySQL), nightly backups. |
| **Installable app** | Progressive Web App: install it from the browser on Android, iPhone/iPad or desktop for a full-screen app with its own icon, home-screen shortcuts (Add item, Record a sale) and an offline notice. Phone-friendly layout with a slide-out menu. |
| **Security** | Passkeys (Face ID, Touch ID, Windows Hello, Android, security keys) for passwordless sign-in or as a second factor; local accounts with TOTP two-factor (Google Authenticator, Authy, 1Password…) and single-use recovery codes, an admin switch to require MFA for everyone, OpenID Connect single sign-on with automatic user creation and group-based access/admin rules, admin vs. regular users, brute-force throttling, sign-out-everywhere. |

## Where data lives

Nothing stateful lives in the image. Destroying the container loses nothing.

| Host path | Container path | Contents |
|---|---|---|
| `./data` | `/data` | `flipdesk.db` (when using the built-in SQLite DB), `uploads/` (photos, receipts), `backups/` |
| `./config` | `/config` | `secret.key` (auto-generated — **back it up**, it encrypts stored eBay tokens/secrets), optional `config.yml` |
| external DB | — | All tables when `DB_TYPE=postgres` or `mysql` |

Settings precedence: **environment variable › `/config/config.yml` › value saved in the web UI › default.** Values set by env/file show as locked in the UI.

## Build from source (built-in SQLite)

Prefer the [prebuilt image](#install-with-the-prebuilt-docker-image-recommended) unless you're changing the code. To build the image yourself from a clone of this repo:

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

## Install it as an app (PWA)

FlipDesk can be installed from the browser like a native app. **Browsers only allow this over HTTPS**, so open it through your reverse proxy / `PUBLIC_URL` rather than `http://<ip>:8080`.

- **Android** (Chrome, Edge, Samsung Internet): tap **Install app** in the FlipDesk menu or on the sign-in page, or browser menu ⋮ → *Install app*.
- **iPhone / iPad** (Safari): Share → **Add to Home Screen**. The in-app *Install app* button shows these steps.
- **Desktop** (Chrome / Edge): the install icon at the right of the address bar, or *Install app* in the menu.

Long-press the installed icon for shortcuts to **Add item** and **Record a sale**. Only static files (styles, scripts, icons) are cached on the device; pages with your financial data are always loaded live from your server, and a short “You're offline” screen appears if it can't be reached. After an upgrade, the app picks up the new version on its next launch.

## Shelf locations, QR labels and scanning

**Locations.** Every place you store things (shelf, bin, tote, closet) is a location with a short code like `L0007`. Create them under **Locations & labels**: one per line, and ranges such as `Tote 1-20` or `Shelf A01-A12` make one each. Locations are also created automatically when you type a new one on an item, and existing free-text locations were converted on upgrade.

**Printing labels.** Tick locations → **Print selected** (or **Print all**), or use **Add & print labels**. Choose a layout:

| Layout | Labels | QR size |
|---|---|---|
| 4×6 — 4 per label | 2″×3″ each, cut in quarters (default) | 1.75″ |
| 4×6 — 6 per label | 2″×2″ each | 1.25″ |
| 4×6 — 8 per label | 2″×1.5″, QR beside the text | 0.95″ |
| 4×6 — 1 per label | one large label | 3.7″ |
| 4″×2″, 2.25″×1.25″, 2″×1″ | one per label, for small label rolls | 0.85–1.75″ |
| Letter sheet | 30 per page (Avery 5160-style) | 0.85″ |

Each label shows the location name in large bold text plus the code (and an optional description). Multi-up labels have dashed cut lines. Use **Copies** for duplicates, and **Skip spots** to use up a partly used 4-up/6-up label or sheet. Print from the browser (paper size 4×6, margins none, scale 100%) or download the exact-size **PDF**, which is the most reliable route for thermal printers such as Rollo, Zebra, MUNBYN and Dymo 4XL. Renaming a location doesn't invalidate printed labels, because the QR holds the code.

**Scanning.**
- **📷 Scan shelf label** (sidebar, phone top bar, or long-press the app icon): live camera scanner in the browser (needs HTTPS). It also offers **Scan a photo** and typing the code by hand.
- Your phone's **normal camera app** works too: each label is a link to `PUBLIC_URL/l/CODE`, which opens that location.
- **See what's on a shelf:** scan → location page with everything stored there.
- **Put items away:** on a location page, **Put items here** lists in-stock items with no location (or search), tick and move.
- **Set an item's location:** on the item page, **📷 Scan shelf** then scan the label. (Starting it in FlipDesk and then using the phone camera app within 5 minutes also works.)
- Every move is recorded on the item's timeline, and sale alerts tell you which shelf to pull from.

Set **Public URL** before printing so the QR links point at the address your phone uses.

## Inventory reconciliation (shelf count)

**Inventory → Reconciliation sheet** prints a PDF to take round your shelves and confirm that what's listed on eBay is actually there.

- **What's on it:** a row per item with a tick box, the item title (plus SKU, eBay item #, price), its location and code, the expected quantity, a box for the counted quantity, and space for notes. Expected quantity is flagged when eBay shows a different available quantity.
- **Grouped by location** (default): one block per shelf, so you can walk one shelf at a time. Items with no location are listed last so you can find and place them. A shelf that runs onto the next page repeats its heading marked "(continued)". You can switch to alphabetical order.
- **Scope:** active eBay listings (default), everything in stock, or unlisted only. You can limit it to chosen locations, and optionally include items with no location.
- **Paper:** Letter or A4, portrait or landscape (landscape gives wider notes). The sheet has "Counted by / Checked by" lines, a section for things found that aren't on the list, and page numbers.
- **Single shelf:** each location page has a **🧾 Count sheet** button for just that shelf.

## Users, two-factor and single sign-on

**Two-factor (TOTP).** Each user turns it on under **My account → Set up authenticator**: scan the QR code, enter a code, save the 10 recovery codes. Codes can't be reused (replay protection) and wrong guesses are rate-limited. Admins can reset a user's two-factor from **Settings → Users**.

**Passkeys.** Local accounts can add passkeys under **My account → Passkeys** (Face ID / Touch ID, Windows Hello, Android, iCloud Keychain, Google Password Manager, 1Password, Bitwarden or a hardware security key). Then:
- **Sign in with just a passkey**: the *Sign in with a passkey* button, or pick it from the username field's autofill suggestions. Because a passkey needs your device *and* its PIN/biometric, no extra code is asked for.
- **Or use it as your second step** after a password, instead of an authenticator code.
- Adding your first passkey gives you recovery codes, same as the authenticator app.

Passkeys are tied to the site's address, so they need **HTTPS** and a correct **Public URL** (`PUBLIC_URL`). If you change the domain, existing passkeys stop working and users add new ones. SSO users don't get passkeys in FlipDesk; their identity provider handles that. Admins can turn passkeys off (**Settings → Security & MFA**) or remove a user's passkeys from **Settings → Users**; there's also `python -m app.cli reset-passkeys <username>`.

**Require MFA.** **Settings → Security & MFA → Require two-factor authentication** (or `MFA_REQUIRED=true`). Anyone without an authenticator app or passkey is sent straight to setup after signing in and can't use the app until they finish. By default SSO users are exempt because your identity provider is expected to enforce MFA; turn on *Also require FlipDesk TOTP for SSO users* if it doesn't.

**Single sign-on (OIDC).** Works with Authentik, Authelia, Keycloak, Pocket ID, Zitadel, Entra ID, Google, Okta and others.
1. In your IdP, create an OpenID Connect web app with redirect URI `PUBLIC_URL/auth/oidc/callback` (shown with a copy button in **Settings → Single sign-on**), authorization-code flow, scopes `openid email profile` (+ `groups`).
2. In FlipDesk enter the issuer URL, client ID and secret, then click **Test connection**.
3. Choose who gets in: *Create users automatically* (on by default), *Allowed groups*, and *Admin groups* (membership is re-checked on every sign-in).
4. Existing local users are linked automatically the first time they sign in with SSO if their **verified** email matches. You can also pre-create “SSO only” users with just an email.
5. Once you've signed in with SSO yourself, you can disable password login and/or skip the login page (`?local=1` still shows the form unless passwords are disabled).

Security details: PKCE (S256), `state` and `nonce` checks, ID-token signature verified against the provider's JWKS (RS/ES/PS algorithms only), issuer/audience/expiry checks.

**Roles.** Admins can change settings, manage users, connect eBay and import/export data. Regular users can work with inventory, sales, ledger and reports.

**Locked out?** Run commands inside the container:

```bash
docker exec -it flipdesk python -m app.cli list-users
docker exec -it flipdesk python -m app.cli reset-mfa admin            # authenticator app
docker exec -it flipdesk python -m app.cli reset-passkeys admin
docker exec -it flipdesk python -m app.cli reset-password admin        # prompts, or generates one
docker exec -it flipdesk python -m app.cli set mfa_required false
docker exec -it flipdesk python -m app.cli set oidc_disable_password_login false
docker exec -it flipdesk python -m app.cli make-admin alice
```

Settings changed this way apply within ~15 seconds; no restart needed. Environment variables (e.g. `MFA_REQUIRED=false`) always win over saved settings.

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

| `MFA_REQUIRED` | `false` | require TOTP for everyone |
| `MFA_REQUIRED_FOR_OIDC` | `false` | also require FlipDesk TOTP for SSO users |
| `PASSKEYS_ENABLED` | `true` | allow passkeys for local accounts |
| `OIDC_ENABLED` `OIDC_ISSUER` `OIDC_CLIENT_ID` `OIDC_CLIENT_SECRET` | — | single sign-on |
| `OIDC_DISPLAY_NAME` `OIDC_SCOPES` `OIDC_USERNAME_CLAIM` `OIDC_GROUPS_CLAIM` | `SSO` … | |
| `OIDC_AUTO_CREATE` `OIDC_ALLOWED_GROUPS` `OIDC_ADMIN_GROUPS` `OIDC_NEW_USERS_ADMIN` | `true` / — | provisioning rules |
| `OIDC_LINK_BY_EMAIL` `OIDC_ALLOW_UNVERIFIED_EMAIL` | `true` / `false` | account linking |
| `OIDC_DISABLE_PASSWORD_LOGIN` `OIDC_AUTO_REDIRECT` | `false` | SSO-only mode |

Any setting in the UI can also be set by its key (e.g. `SYNC_ORDERS_MINUTES`, `NTFY_URL`, `RELIST_ENABLED`). See `config.example.yml`.

## Backups & migration

- Back up `./data` and `./config`. Nightly backups also land in `./data/backups` (SQLite copy, or JSON export for external DBs).
- Moving from SQLite to PostgreSQL: *Import · export · backup → Download full export*, restart the container with the Postgres settings, sign in to the new empty install, and *Restore* the export.

## Notes and limits

- Single app process by design (the scheduler and sign-in rate limits run in-process) — don't scale to multiple replicas.
- Set `SESSION_COOKIE_SECURE=true` when FlipDesk is only reached over HTTPS.
- Finances API data for some EU/UK sellers requires eBay digital signatures, which this version doesn't implement; orders still sync, and fees fall back to the order's marketplace fee.
- Not a cross-lister

## Development

```bash
pip install -r requirements.txt
DATA_DIR=./data CONFIG_DIR=./config uvicorn app.main:app --reload --port 8080
python tests/test_app.py   # end-to-end test with a mocked eBay API
python tests/test_auth.py  # TOTP, MFA enforcement, roles and OIDC against a local fake identity provider
python tests/test_passkeys_browser.py  # passkeys in Chromium with a virtual authenticator (needs Playwright)
python tests/test_locations.py   # locations, label layouts (HTML + PDF, QR decode if OpenCV + pdftoppm) and scanning
python tests/test_recon.py       # reconciliation sheet page + PDF
```
