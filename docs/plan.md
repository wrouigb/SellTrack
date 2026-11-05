# SellTrack: Audit & Implementation Plan (v2)

**Status:** **Approved** 5 Nov 2026 (split basis A: real money). Phases 3 and 4 merged, phase 5 built and awaiting review, see section 12 · **Repo:** `wrouigb/SellTrack` (private) · **Date:** 5 Nov 2026 · **Scope:** turn the existing Flask/SQLite SellTrack app into a production-ready full-stack app, self-hosted cheaply (Raspberry Pi or small VPS).

---

## 0. Summary

SellTrack today is one 450-line `app.py`, 10 Jinja templates, one stylesheet, SQLite and five tables. The core accounting idea is right: each sale stores the product cost at the time it was made, so old profit figures never drift. Data is consistently limited to each user's own account. Both are kept.

It is not deployable as-is. Two issues are **critical** and were reproduced, not guessed:

1. A hard-coded fallback `SECRET_KEY`. If the env var is missing, anyone can forge a login cookie for any account; a forged cookie was produced and accepted.
2. The documented way to run it, `app.run(debug=True)`, exposes the Werkzeug debugger (remote code execution).

**Plan:**
- Stay server-rendered with **Flask + Jinja + Tailwind + HTMX + Alpine.js**.
- Rebuild on Postgres, a hardened username/password login (no email), a full **profit split system** (people, upfront costs, debts, settle-up), an admin console, Docker images for Raspberry Pi and PCs, and GitHub Actions CI/CD.

---

## 1. Decisions from your answers

| # | Your answer | What it means for the build |
|---|---|---|
| 1 | One account; partners share by sharing the login | All data is owned by `user_id`. No teams/invites. Because logins may be shared: a recent sign-ins list, a "new sign-in" notification and "log out everywhere" |
| 2 | No data to transfer | Fresh schema; no import script |
| 3 | Username + password only, no email or verification | No email field at all. Forgotten passwords are handled by **recovery codes** shown once at sign-up, plus an admin password reset |
| 4 | No email access | Notifications are in-app only. The support page saves messages to an admin inbox; admin replies arrive as in-app notifications |
| 5 | Cheapest hosting later, maybe a Raspberry Pi; few users | Docker Compose stack built for both Pi (arm64) and PC (amd64), lean enough for a Pi. Deploys are *pull-based*: the Pi fetches new versions itself, so no inbound ports or CI credentials on it. Nightly database backups. Pi hosting is set up with you in phase 7 |
| 6 | AUD | Fixed AUD, en-AU formatting (`$1,234.50`, `−$3.00`), dd/mm/yyyy dates, time-zone setting for "today" |
| 7 | Detailed profit system: named people within the account, upfront costs, clear debts and reset the tally | New split engine, see **section 3** |
| 8 | No public integrations | No public API, API tokens, OAuth or file uploads (so no object storage). CSV export of your own data stays. Internal endpoints only serve the app's own pages |
| 9 | Refresh the brand | Keep SellTrack name, price-tag logo and navy/cream palette, modernised |
| 10 | Use the SellTrack repo, or create one | `wrouigb/SellTrack`, private. Production deploys use a manual "Promote" workflow run, because required-reviewer approvals aren't available on private repos without GitHub Enterprise |
| — | Split basis | **A, real money**: money in minus upfront costs; per-item product costs are not used in the split |

---

## 2. Audit of the current app

### 2.1 Pages and routes

| Route | Methods | What it does |
|---|---|---|
| `/register` | GET, POST | Username + password sign-up, logs in immediately |
| `/login` | GET, POST | Username + password login |
| `/logout` | POST | Clears session |
| `/` | GET, POST | Dashboard: 4 all-time totals, add-a-sale form with live profit preview, profit by product, latest 5 sales |
| `/sales` | GET | All sales, search `?q=` on customer/product, totals for shown rows |
| `/edit/<id>` | GET, POST | Edit sale date, customer, qty, price (product and cost locked) |
| `/delete/<id>` | POST | Delete sale |
| `/products` | GET, POST | List products with sale counts; add product (writes first cost-history row) |
| `/products/<id>` | GET, POST | Edit name/cost/sell price; logs cost changes; renaming rewrites name on past sales; shows cost history |
| `/products/delete/<id>` | POST | Delete product; sales kept but unlinked; cost history deleted |
| `/split` | GET, POST | Partners with % shares; each person's cut of all-time profit; warning if shares ≠ 100% |
| `/split/even` | POST | Equal shares for all partners |
| `/split/delete/<id>` | POST | Remove partner |

### 2.2 Data model

`User(username, password_hash)` · `Product(user_id, name, cost, sell_price)` · `CostChange(product_id, old_cost, new_cost, changed_on)` · `Sale(user_id, product_id?, product_name, sale_date, customer, quantity, price_paid, cost_each)` · `Partner(user_id, name, share)`.

All money and shares are `Float`. Most tables have no timestamps, there are no DB-level checks, and SQLite doesn't enforce foreign keys by default.

### 2.3 Strengths to keep

- **Cost snapshot plus cost history.** Each sale stores the cost at the time (`cost_each`), and every cost change is logged.
- **Product name stored on each sale**, so history survives product deletion.
- **Every query limited to the user's own data.** All 13 routes were checked; none lets a user reach another user's records.
- **Forms read explicit fields only**, so users can't set fields they shouldn't. No raw SQL, so no SQL injection. Jinja autoescape is on.
- **Passwords are hashed** (Werkzeug scrypt). The session is cleared on login and logout is POST.
- **Post/redirect/get pattern**, a live profit preview and price auto-fill. These are good UX.

### 2.4 Security issues (ranked)

| # | Severity | Issue | Evidence | Fix |
|---|---|---|---|---|
| 1 | **Critical** | Fallback `SECRET_KEY`. Without the env var, session cookies can be forged for any user | **Reproduced** | App refuses to start in staging/prod without a ≥32-byte secret |
| 2 | **Critical** | `app.run(debug=True)` as the documented run method; debugger = remote code execution | `app.py:453`, README | Gunicorn only; debug impossible outside dev |
| 3 | High | No CSRF protection on any POST; `SameSite` unset | All forms | Flask-WTF CSRF + `SameSite=Lax` |
| 4 | High | No rate limiting on login/register; 6-char passwords allowed | `login()`, `register()` | Per-IP and per-username limits; 8+ chars and a bundled common-password blocklist |
| 5 | High | Cookie not `Secure`; 7-day sessions can't be revoked | config | Secure cookies, `session_version` for "log out everywhere" |
| 6 | Medium | Script injection via product name in inline `onsubmit="confirm('…')"` (HTML escaping is undone by the browser before the JS runs) | **Reproduced**: `x'); alert(1); ('` executes | Native `<dialog>`, no inline JS, strict CSP |
| 7 | Medium | No security headers (CSP, clickjacking protection, `nosniff`, Referrer-Policy, HSTS) | none set | Header middleware with nonce-based CSP |
| 8 | Medium | Unique product names enforced only in Python, so two requests at once can create duplicates | `name_is_taken()` | Partial unique index on `(user_id, lower(name))` |
| 9 | Low | Login timing reveals whether a username exists | `login()` | Constant-time dummy hash check |
| 10 | Low | Unpinned dependencies, no scanning | `requirements.txt` | Lockfile + pip-audit in CI |

### 2.5 Bugs and data integrity

- **`NaN` and `Infinity` pass validation** (verified). One crafted POST makes every total `NaN`.
- **Float money accumulates rounding errors.** e.g. `0.7 × 3 = 2.0999999999999996`. Move to `NUMERIC(12,2)` and `Decimal`.
- **Even split leaves cents unallocated.** 3 people → 99.99%. Fix with largest-remainder rounding.
- **No server-side length checks.** On Postgres, long input becomes a 500 error.
- **Search changes behaviour on Postgres.** Postgres `LIKE` is case-sensitive (SQLite's isn't), and `%`/`_` aren't escaped.
- **Validation errors discard what the user typed** and show one generic message.
- **Tables are never created under Gunicorn.** `db.create_all()` only runs under `__main__`.

### 2.6 UX and accessibility gaps

- **No viewport meta tag**, so phones get a zoomed-out desktop page; 8-column tables overflow.
- **Missing semantics.** No `lang`/`charset`, no table headers or scopes, and flash messages aren't announced to screen readers.
- **Contrast below AA.** `#777` hint/empty-state text and grey "danger" buttons are 4.48:1, below the 4.5:1 minimum.
- **Reporting is all-time only.** No date ranges, export or pagination.
- **Missing account basics.** No password change, recovery, account deletion, onboarding, help or error pages.

### 2.7 Technical debt and performance

- **No structure.** Single module with no app factory, blueprints, service layer, migrations, tests, logging or environment configs.
- **Slow aggregates.** The dashboard and split page load every sale on each request (should be SQL `SUM`/`GROUP BY`), and the products page runs one query per product.
- **Outdated code.** `Model.query` is legacy, URLs are hard-coded, and the totals key is still named `"stamps"`.
- **Inline scripts and handlers** prevent a strict CSP.

---

## 3. Profit split system (new design)

### 3.1 Concepts

- **People:** names you add inside your account ("+ Add person"), each with a share %, e.g. Alex 50%, Sam 50%.
  - One person is marked as **usually receives sale money**.
  - People can be archived, never deleted, once they have history.
- **Sales** gain an optional **Received by** field. It's only shown when there are 2+ people and defaults to the usual receiver, so with one bank account or cash tin nothing changes.
- **Upfront costs:** money someone spent for the business. Fields: description, category (stock, equipment, fees, packaging, other), amount, date, **paid by** (a person), optional product link and notes.
- **Payments:** money one person gave another, e.g. "Sam paid Alex $20". Records part-payment of a debt.
- **Settle up:** pays off every remaining debt in one go. Saves a permanent snapshot (period dates, totals, everyone's figures, the transfers) and **resets the tally to $0** for the next period.
  - **Undo last settle-up** exists for mistakes. It reverses the settle-up exactly, including putting everyone's shares back as they were.
  - Sales, costs and payments inside a settled period are locked for money fields until it's undone.

### 3.2 The rules (all in cents, so totals always add up exactly)

For the current period (everything since the last settle-up):

```
Money in          = all sale money received
Upfront costs     = all upfront costs paid
Profit to share   = Money in − Upfront costs                 (can be negative: losses are shared too)

For each person:
  Gets back       = upfront costs they paid                  (paid back before anything is split)
  Share           = Profit to share × their %                (largest-remainder rounding)
  Should end with = Gets back + Share
  Holding         = sale money they received + payments received − payments made
  Balance         = Should end with − Holding                (+ owed to them · − they owe)
```

Balances always total $0. Settle-up suggests the fewest transfers that zero everyone, e.g. "Sam pays Alex $32.50".

### 3.3 Worked example

Alex and Sam split 50/50.
- **Upfront costs:** Alex bought $35 of stock and Sam paid a $10 stall fee.
- **Sales:** $120 in total. Sam took $80 at the stall and Alex took $40 online.

| | Alex | Sam | Total |
|---|---|---|---|
| Gets back (upfront) | $35.00 | $10.00 | $45.00 |
| Share of profit ($120 − $45 = $75) | $37.50 | $37.50 | $75.00 |
| Should end with | $72.50 | $47.50 | $120.00 |
| Holding | $40.00 | $80.00 | $120.00 |
| **Balance** | **+$32.50 owed** | **−$32.50 owes** | $0.00 |

**Settle up → "Sam pays Alex $32.50".** Afterwards Alex has $72.50 (his $35 back plus $37.50) and Sam has $47.50 (his $10 back plus $37.50). The tally starts again at $0.

### 3.4 How stock costs are counted (decided: A)

Products already have a per-item cost, used for the per-sale profit figures on the dashboard. If you *also* log the stock you bought as an upfront cost, it would be counted twice. Two ways to handle it:

- **A, real money (recommended).** The split uses only money in minus upfront costs (the rules above). Per-item product costs remain for per-sale and per-product profit figures but aren't subtracted again in the split. Log what you actually spend as upfront costs, and whoever paid gets it back. Settling always balances with real cash.
- **B, product costs.** The split subtracts each sale's per-item cost (as today). Upfront costs are only for extra expenses like fees and equipment. Simpler if you never log stock purchases, but nobody is paid back for stock they bought.

### 3.5 Split screens

`/app/split` has five tabs:
- **Overview:** money in, upfront costs, profit to share, a table per person, the suggested transfers, and the Record payment and Settle up buttons.
- **Upfront costs.**
- **Payments.**
- **History:** past settlements with full breakdowns.
- **People:** add, set shares, even split, usual receiver, archive.

Settle up is blocked unless shares total 100.00%, with the same warning as today.

---

## 4. Target product

### 4.1 Roles

| Role | Can |
|---|---|
| **Guest** | Landing, help, privacy, terms, register, login, recover with a code |
| **User** | Everything in their own account |
| **Admin** (you) | Users, support inbox, announcements, audit log, feature flags, system health. Admin **cannot** see users' sales/products/split data, only account details and activity counts. First admin created with `flask users create-admin` |

### 4.2 Sitemap

```
Public    /  ·  /help  ·  /privacy  ·  /terms  ·  /login  ·  /register  ·  /recover
App       /app                       Dashboard
          /app/sales  [/new, /<id>/edit, /export.csv]
          /app/products  [/<id>]     cost history, archive
          /app/split  [overview · costs · payments · history · people]
          /app/notifications
          /app/settings  [account · security (password, recovery codes, sign-ins, log out everywhere) · preferences (theme, time zone) · data (export, delete account)]
          /onboarding
Admin     /admin  ·  /admin/users[/<id>]  ·  /admin/support  ·  /admin/announcements  ·  /admin/audit  ·  /admin/flags  ·  /admin/system
Ops       /healthz  ·  /readyz
```

Old URLs (`/sales`, `/products`, `/split`, `/edit/<id>`) redirect to their new homes.

Navigation: top bar on desktop. On mobile, a bottom tab bar plus a persistent **+ Sale** button.

### 4.3 Core journeys

1. **First run:** landing → register (username, password) → save recovery codes → onboarding (time zone, add people and shares, first product) → first sale → dashboard.
2. **Daily:** login → quick-add sale (auto-fill, live profit, received by) → dashboard for this month vs last.
3. **Spend:** split → add upfront cost (who paid) → balances update.
4. **Square up:** split → see "Sam owes Alex $32.50" → record a part-payment or Settle up → tally resets → history kept.
5. **Forgot password:** `/recover` → username + recovery code + new password → all sessions revoked → in-app notice.
6. **Admin:** suspend an account, reset a password (temporary password, must change at next login), answer a support message, roll a feature flag to 10%, check the Pi's disk and last backup.

### 4.4 Feature scope

**Preserved:**
- Cost snapshot per sale, cost history.
- Auto-fill and live profit preview.
- Product rename flowing to past sales; past sales kept when a product is removed.
- Partner shares and even split (now exact to the cent).
- Search, edit, delete.

**Improved:**
- **Dashboard:**
  - Period selector (default this month).
  - Tiles: money in, profit on sales, upfront costs, items sold, sales count and average sale, each with change vs the previous period.
  - Money-in/profit chart, top products, recent activity, and a "who owes whom" card.
  - Next steps, e.g. "save your recovery codes", "shares total 90%", "Product X has no selling price".
- **Sales list:** filter by date range, product, customer and who received the money; sortable; paginated; bulk delete with undo; CSV export of the current filter.
- **Products:** archive instead of delete, which keeps history; margin %; sort and search.

**New:**
- **Account:** recovery codes, password change, recent sign-ins, log out everywhere, delete account, full data export (zip of CSVs).
- **In-app notifications:**
  - Security: new sign-in, password changed, recovery code used, few codes left.
  - Split: shares ≠ 100%, settle-up recorded.
  - Support replies and admin announcements.
- **Admin console** (section 4.1) with search, filter, sort, pagination and bulk suspend on users.
- **Feature flags** (on/off, % rollout, allowlist) and an audit log of security-sensitive actions.
- **Pages:** landing, onboarding, help/FAQ (including "how the split works") with a support form, privacy, terms, and 403/404/429/500 pages.
- **Billing:** extension point only, a single `entitlements(user)` check called where limits would apply. It returns unlimited today.

---

## 5. Frontend: Flask + Jinja + Tailwind + HTMX + Alpine.js

**Why this over React/Next.js:**
- **It fits the work.** The app is forms, tables and summaries, and the existing templates and flows port directly.
- **One small container to run on a Pi:** one language, one login model (cookie + CSRF), no browser-side token storage, CORS or Node server.
- **HTMX covers the interactions that matter:** live filters, sorting, pagination, inline edits, bulk actions, the split tabs and the notification bell. Pages still work without JavaScript.

**Strict CSP:** Alpine's CSP build, `htmx.config.allowEval = false` and self-hosted, pinned assets. That means no `unsafe-inline`/`unsafe-eval` and no CDNs.

**When I'd pick Next.js instead:** a mobile app, offline mode or a separate frontend team. None apply here.

---

## 6. Architecture

```
Browser ─HTTPS─▶ tunnel/TLS edge ─▶ Nginx (gzip, static files with long-cache headers, security headers, proxy)
                                          │
                                          ▼
                              Gunicorn (2 workers × 4 threads) ── Flask create_app()
                              blueprints (thin) → services (rules) → SQLAlchemy 2.x
                                   │                       │
                                   ▼                       ▼
                         Redis (rate limits, cache)   PostgreSQL 16
                                                           ▲
                          scheduler (same image): nightly cleanup
                          backup (postgres image): nightly pg_dump, keep 14
      JSON logs to stdout with request IDs · optional Sentry (free tier) via SENTRY_DSN
```

**Pi footprint:** roughly 0.5 GB RAM per stack. A Pi 4/5 with 4 GB runs staging and production comfortably; 2 GB works with one shared Postgres. Use 64-bit Raspberry Pi OS, and preferably a USB SSD, since SD cards wear out under database writes.

### 6.1 Layout

```
selltrack/
├─ app/
│  ├─ __init__.py   config.py   extensions.py
│  ├─ models/       user, recovery_code, product, sale, partner, upfront_cost, payment,
│  │                settlement, notification, support_message, audit_log, feature_flag
│  ├─ services/     auth, products, sales, reporting, split, notifications, support, audit, flags, export, entitlements
│  ├─ forms/        WTForms
│  ├─ schemas/      Pydantic (internal JSON endpoints, e.g. chart data)
│  ├─ blueprints/   public, auth, onboarding, dashboard, sales, products, split, settings,
│  │                notifications, admin, health
│  ├─ security/     rbac, headers/CSP, passwords, rate limits
│  ├─ jobs/         scheduler + cleanup jobs
│  ├─ templates/    layouts/, components/ (macros), pages/
│  └─ static/       src/ → dist/ (built, hashed filenames)
├─ migrations/      Alembic
├─ deploy/          nginx.conf, gunicorn.conf.py, compose.prod.yml, pi-deploy.sh, systemd units, db-roles.sql
├─ tests/           unit/ integration/ e2e/ factories.py conftest.py
├─ docs/            schema.md, endpoints.md, user-guide.md, admin-guide.md, runbooks/
├─ .github/workflows/  ci.yml, release.yml, promote.yml
└─ Dockerfile · docker-compose.yml · Makefile · pyproject.toml · uv.lock · README.md · CHANGELOG.md · .env.example · .pre-commit-config.yaml
```

### 6.2 Key choices

| Concern | Choice |
|---|---|
| Python / deps | 3.13, `pyproject.toml` + hashed `uv.lock` |
| Database | PostgreSQL 16 everywhere (tests too), SQLAlchemy 2.x, psycopg 3, Flask-Migrate |
| Forms / validation | Flask-WTF (CSRF + per-field errors); Pydantic for internal JSON |
| Auth | Flask-Login + `session_version` (revocable); Argon2id; common-password blocklist bundled (no network calls); recovery codes stored hashed; constant-time login |
| Rate limits | Flask-Limiter on Redis (in-memory fallback if Redis is down) |
| Headers | In-house middleware: nonce CSP, HSTS, `frame-ancestors 'none'`, nosniff, Referrer-Policy, Permissions-Policy |
| Background work | No queue needed (no email, no imports). A small scheduler process runs nightly cleanup; backups are a separate container. A queue can be added later if needed |
| Cache | Flask-Caching (Redis) for dashboard aggregates, invalidated on writes |
| Logging / errors | structlog JSON, `X-Request-ID`, error pages show a reference ID only; optional Sentry |
| Server | Gunicorn behind Nginx; the dev server is used only for local development |

### 6.3 Configuration and database access

- **Configuration:** `Development / Testing / Staging / Production` selected by `APP_ENV`. Staging/prod refuse to start without a strong `SECRET_KEY`, a Postgres URL and `DEBUG` off. `.env.example` lists every variable.
- **Database roles:** `selltrack_migrator` (schema changes, used only by the migration step), `selltrack_app` (read/write data only; insert-only on the audit log) and `selltrack_backup` (read-only).

---

## 7. Database schema (PostgreSQL)

All tables have `created_at`/`updated_at timestamptz` (UTC). Money is `numeric(12,2)`. Every business table is owned by `user_id`, and every query is filtered by it.

```
users              id · username citext UNIQUE (3–30, letters/numbers/._-) · password_hash · role enum(user,admin)
                   · status enum(active,suspended) · must_change_password bool · session_version int
                   · timezone text · theme enum(system,light,dark) · last_login_at · onboarded_at
recovery_codes     id · user_id · code_hash · used_at NULL                      (10 per user, regenerable)
sign_ins           id · user_id · ip inet · user_agent · created_at               (recent sign-ins list; append-only)
products           id · user_id · name varchar(100) · cost numeric CHECK ≥0 · sell_price numeric NULL CHECK ≥0 · archived_at NULL
                   UNIQUE (user_id, lower(name)) WHERE archived_at IS NULL
product_cost_changes  id · product_id (cascade) · old_cost NULL · new_cost · changed_at
partners           id · user_id · name varchar(50) · share_pct numeric(5,2) CHECK 0–100 · receives_money_default bool · archived_at NULL
                   UNIQUE (user_id, lower(name)) WHERE archived_at IS NULL
sales              id · user_id · product_id NULL (set null) · product_name · sale_date · customer_name NULL · notes NULL
                   · quantity int CHECK >0 · price_paid numeric CHECK ≥0 · cost_each numeric CHECK ≥0
                   · total_cost, profit  GENERATED ALWAYS … STORED
                   · received_by_partner_id NULL · settlement_id NULL · deleted_at NULL (undo; purged after 30 days)
                   INDEX (user_id, sale_date DESC, id DESC) · trigram index for search
upfront_costs      id · user_id · description · category enum(stock,equipment,fees,packaging,other) · amount numeric CHECK >0
                   · cost_date · paid_by_partner_id · product_id NULL · notes NULL · settlement_id NULL
partner_payments   id · user_id · from_partner_id · to_partner_id (≠ from) · amount CHECK >0 · paid_on · note · settlement_id NULL
settlements        id · user_id · period_start · period_end · settled_at · money_in · upfront_total · profit_to_share
                   · snapshot jsonb (output of split_engine.SplitResult.to_snapshot(), version 1)
notifications      id · user_id · kind · title · body · url NULL · read_at NULL
support_messages   id · user_id · subject · body · status enum(open,answered,closed) · admin_reply NULL · replied_at NULL
audit_logs         id · actor_id NULL · action · target_type · target_id · ip inet · user_agent · request_id · metadata jsonb
feature_flags      id · key UNIQUE · description · enabled · rollout_pct 0–100 · allow_user_ids bigint[]
```

`settlement_id IS NULL` means "in the current, open period". Settle-up stamps the open rows with the new settlement's ID; undo clears it.

Refinements made while building (phase 3):
- `sign_ins` added, for the recent sign-ins list and the "new sign-in" notification.
- Settlement totals are `numeric(16,2)`, because a period's total can exceed any single amount.
- Every money check also rejects `NaN`: Postgres sorts it above all numbers, so `'NaN' >= 0` is true.
- Rows reference people and settlements through composite foreign keys on `(user_id, id)`, so a row can't point at another account's records even if a query has a bug. They're checked at commit, so deleting an account can cascade in any order.
- A partial unique index allows only one usual receiver per account.
- Append-only tables (`audit_logs`, `sign_ins`, `product_cost_changes`) have no `updated_at`.

---

## 8. Design system

The brand (navy `#1f4e79`, cream `#f5f0e6`, price-tag logo) is refreshed, not replaced.

- **Tokens** are CSS variables used by Tailwind v4, in light and dark sets: navy scale, warm cream neutrals (light) and slate (dark), and semantic success/danger/warning/info.
- **Type and layout:** Inter (self-hosted), with `tabular-nums` on every money figure. 16px base, 4px spacing grid, radii 6/10/16.
- **Components (Jinja macros):** buttons (primary/secondary/ghost/danger, loading state), labelled inputs with hints, inline errors and an error summary, cards, stat tiles with deltas, badges, tabs, dropdowns, responsive tables (stacked cards on phones), sortable headers, pagination, bulk-action bar, `<dialog>` modals, toasts, empty states, skeleton loaders and error states.
- **Dark mode:** follows the system setting by default, with a light/dark/system toggle.
- **Accessibility target: WCAG 2.2 AA.**
  - Skip link, landmarks and a visible focus ring.
  - Full keyboard support.
  - Targets ≥ 24px (44px for main actions).
  - Profit/loss and owes/owed never shown by colour alone.
  - Reduced-motion respected.
  - A unit test checks every token colour pair is ≥ 4.5:1.
- **Mobile-first:** designed at 360px.

Decisions made while building (phase 4):
- **The swing tag** is the one bold element. It's a manila luggage label with a punched hole, used only for money owed, such as "Sam pays Alex $32.50". Everything around it is a quiet ledger: the dashboard's figures are one strip, not a row of separate cards.
- **Tabular numbers** are used on all money, as above, including large figures.
- **Skeleton loaders were dropped.** Pages are drawn on the server, so nothing loads in pieces. While filters fetch new results, the old ones stay in place, dimmed.
- **The chart** is SVG drawn on the server: money in and profit as columns on one axis, with a table view, plus hover and arrow-key readouts. Its colours passed a colour-blindness check in both themes.
- **Comparisons are like for like.** A month or year in progress is compared with the same days of the one before, because five days of November against all of October would look like a fall every month.

---

## 9. Quality, CI/CD and deployment

**Tests:**
- Unit tests for services, models, permissions and helpers. The split engine gets exhaustive cases: losses, rounding, part-payments, undo and archived people.
- Integration tests for every route on real Postgres + Redis.
- Playwright end-to-end tests:
  - register → codes → onboarding → sale → dashboard;
  - upfront cost → payment → settle up → undo;
  - recover with a code;
  - admin suspend, flag and support reply.
- factory_boy factories; coverage ≥ 85% overall and ≥ 95% on the split engine.

**Tooling:** Ruff (lint + format, security rules), mypy, pre-commit, pip-audit, gitleaks, Trivy.

**Docker:**
- A multi-stage image built for **linux/amd64 and linux/arm64**: a Node stage builds the CSS/JS, then a slim Python runtime runs as a non-root user with a healthcheck.
- `docker-compose.yml` for local development (web, scheduler, Postgres, Redis, Nginx).
- `compose.prod.yml` for the Pi or a VPS (adds backup).

**CI/CD:**
- **`ci.yml`** (every push/PR, GitHub-hosted runners): lint, types, tests, end-to-end tests, scans, image build.
- **`release.yml`** (main): builds the multi-arch image, pushes `:sha-…` to GitHub's container registry and moves the `:staging` tag.
- **`promote.yml`:** the production approval step, which retags that image `:prod`. On a public repo it uses GitHub's built-in "required reviewers" approval. On a free private repo that isn't available (verified), so it becomes a manual "Run workflow" button limited to `main`.
- **On the Pi:** a systemd timer runs `pi-deploy.sh` every 2 minutes for each stack. When its tag changes, it pulls the image, runs migrations as a one-off container (migrator role), restarts, and waits for `/readyz`. Then it runs smoke checks, and **automatically rolls back** to the previous image if they fail.
- **Why pull-based:** home internet usually has no fixed public address. This needs no open ports, no CI credentials on the Pi (just a read-only registry token), and no self-hosted runner, which GitHub advises against on public repos.

**Rollback and recovery:**
- Migrations are always backward-compatible (expand, then contract later), so the previous image runs on the new schema.
- Manual rollback is `IMAGE_TAG=<previous> docker compose up -d`.
- A tested restore-from-backup runbook is included.

**Public HTTPS on a Pi** (decided at hosting time): Cloudflare Tunnel (free; needs a domain) or Tailscale Funnel (free `*.ts.net` address). Both avoid port forwarding and handle TLS. Nginx stays inside the stack either way.

**Smoke check after each deploy:**
- `/healthz` and `/readyz` return 200 and the security headers are present.
- The landing page and static assets load with cache headers.
- A smoke account can log in, then add and delete a sale.
- The last backup is less than 26 hours old.

---

## 10. Phased roadmap

| Phase | Deliverables | Done when |
|---|---|---|
| **3. Backend foundations** | pyproject/lock, app factory, configs, models, first migration; auth (register, login, logout, recovery codes, change password, log out everywhere, sign-in history, rate limits, Argon2id); RBAC; services for products, sales, reporting and the **split engine**; audit, flags, notifications services; CSRF, headers, cookies, error handlers, request IDs, logging, health checks | All current behaviours and every split rule covered by passing tests |
| **4. Frontend** | Tailwind build, tokens, components, layouts, dark mode; dashboard; sales (filters/sort/paging/bulk + undo, CSV); products; split (5 tabs, settle-up flow); landing, help, legal, auth, onboarding, settings, error pages | Keyboard-only and axe checks clean on every page; usable at 360px |
| **5. Admin & extras** | Admin console (users, support inbox, announcements, audit log, flags, system health incl. disk and last backup); notifications UI; data export; scheduler + cleanup jobs; caching; entitlements hook | Integration tests per feature; admin and security actions audit-logged |
| **6. Quality & ops** | Full test suite, tooling, scans; multi-arch Dockerfile, compose files, Nginx, Gunicorn; CI, release and promote workflows; `pi-deploy.sh` + systemd units; backups; README (with diagram), `.env.example`, schema/endpoint docs, user & admin guides, CHANGELOG, runbooks | Fresh clone → `make setup && make test && docker compose up` works as the README says |
| **7. Launch & hosting** | Launch checklist; set up the Pi (or a cheap VPS) with you: OS, Docker, tunnel/domain, secrets, first admin, backups, first deploy | Live at your address, smoke checks green |

Checkpoints: I pause for review after phases 3 and 4. I deliver each phase as a pull request.

---

## 11. Assumptions, risks, tradeoffs

**Assumptions:**
- Tens to a few hundred users.
- AUD only, no GST/tax tracking, English only, no billing.

**Risks:**
- **Recovery codes are the only self-service way back in.** A user who loses both their password and codes needs the admin.
- **Shared logins mean the app can't tell partners apart.** The audit log and sign-in history show devices, not people.
- **Home hosting depends on your power and internet.** Backups should also be copied off the Pi; a later optional step.
- **Privacy and terms are plain-English drafts.** Have them reviewed for your situation.

**Tradeoffs:**
- **HTMX over an SPA:** less client-side polish, far less to run and secure.
- **No job queue:** simpler, but anything slow would need one added.
- **Cash-basis split:** always balances with real money, but differs from per-sale profit figures when stock is bought ahead of selling. The UI explains the difference.
- **Pull-based deploys:** up to 2 minutes' delay, but no inbound access to your network.

---

## 12. Progress and handoff

**Done (5 Nov 2026):**
- **Phase 3**: [pull request #1](https://github.com/wrouigb/SellTrack/pull/1), merged.
- **Phase 4**: [pull request #2](https://github.com/wrouigb/SellTrack/pull/2), merged. Every item in the phase 4 row of section 10 is built, and `legacy/` has been deleted. axe and keyboard checks pass on every page, in light and dark, at desktop width and at 360px.
- **Phase 5**, on branch `phase-5-admin`, delivered as its own pull request and awaiting review. Every item in the phase 5 row of section 10 is built, with integration tests per feature, and admin and security actions audit-logged.
- Tests run on real Postgres 16 and Redis, as a role that owns its database but isn't a superuser, plus a real browser for the pages.

**How the split rules were settled** (section 3 left these open; the usual receiver, archiving and undo rules were confirmed on 5 Nov 2026):
- **Usual receiver:** written onto each sale when it's saved, so changing the usual receiver later never moves old sales. A sale saved while there's no usual receiver is written to whoever becomes the usual receiver next, and stays with them. If several people remain and none is the usual receiver, the user has to choose before settling.
- **Archiving:** blocked while the person has anything in the current period, so they settle first. Archiving sets their share to 0%.
- **Undo:** reverses exactly what settling did, including shares, so settling again gives the same figures. Anyone archived since who had a share comes back. Anyone with no share at that settle-up goes to 0%, or stays archived if they already were, and their reopened rows still count. If someone else has taken an archived person's name, the undo is refused until they're renamed. The usual receiver stays as it is, unless only one person is left and there isn't one. The audit log keeps the shares from before the undo.
- **Deleting a person:** only with no history at all, including appearing in a past settlement's snapshot.
- **Locks:** on a settled row, the amount, quantity, and who paid or received are locked. Dates, names and notes can still change. Deleting is blocked. Bulk delete skips and reports locked sales.
- **Settling:** settles only the rows it read and locked, so a sale saved at the same moment falls into the next period. It's refused when there are no rows.

**Also confirmed on 5 Nov 2026:** failed logins are limited to 5 a minute per username, accepting that someone could keep a known username locked out, and the added `sign_ins` table stays.

**Decisions made while building (phase 5):**
- **The audit log holds no names or money.** Phase 3 kept the whole settle-up snapshot in the audit entry for an undo. The audit log outlives the account and the admin can read it, which broke both the promise about deleting an account and the rule that the admin can't see split data. It now keeps counts of rows, and the shares from before an undo as percentages against person IDs. Admin pages also show only the detail fields listed for each action, so a new one can't reach them by accident.
- **Retention:** sales deleted over 30 days ago, as planned. Sign-ins older than 90 days, apart from each account's latest 20. Read notifications older than 180 days. The audit log is never trimmed. The privacy page says all of this.
- **Support is for logged-in people.** There's no email to reply to, and an open form would invite spam, so someone locked out still has to reach the admin some other way. A small page added at `/app/support` shows your messages and replies, because a notification alone can't show what you asked. Each message has one reply, and changing it tells the person.
- **The bell checks every two minutes**, and that doesn't extend the session, so a tab left open still logs out after a week.
- **Suspending ends sessions for good.** Reactivating an account means logging in again, so a stolen session can't come back. An admin can't reset their own password from the console, or suspend themselves.
- **The cache** covers the dashboard's totals, chart and product list, and never who owes whom, which is always worked out fresh. It's stored as JSON rather than Flask-Caching's default of pickle, which would let anyone who got into Redis run code.
- **Last backup** reads the newest file in `BACKUP_DIR` and reports it late after 26 hours. It doesn't check what's in the file. Phase 6 has to mount that folder read-only into the web container.

**Left for later phases, as planned:**
- Phase 6: `deploy/db-roles.sql`, which is what makes the audit log insert-only at the database level (until then it's only by convention in the code); Docker; Nginx, including long-cache headers for hashed assets; CI, including pip-audit and the other scans; a scheduler container running `flask jobs scheduler`; `BACKUP_DIR` mounted read-only into the web container.

**Working in the cloud workspace:**
- `uv sync && npm ci && npm run build` installs and builds everything.
- Postgres 16 and Redis are installed but not running. Start them with `pg_ctlcluster 16 main start` and `redis-server --daemonize yes`, then create the role and databases as in the README.
- Browser tests need `E2E_CHROMIUM_PATH=/opt/pw-browsers/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell`. Don't run `playwright install` there.
- `gh` REST calls work (`gh api repos/wrouigb/SellTrack/...`), but GraphQL commands such as `gh pr list` don't. A merge through the API without the user's explicit approval is refused as a merge without review, so merge only once they've said so for that pull request.

---

### Sources
- [GitHub Docs: Managing environments for deployment](https://docs.github.com/actions/managing-workflow-runs-and-deployments/managing-deployments/managing-environments-for-deployment)
