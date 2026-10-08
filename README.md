# HelpDesk

A Django company workspace with default-deny multi-tenant isolation. Each company has its own organizations, users and roles, support groups, projects, policies, members, tickets, workflows, documents, knowledge, notifications, configuration and audit history.

This is a new application built from the supplied [multi-company requirements](docs/tenant-requirements.md). It uses Django 6.1.2, server-rendered templates and HTMX. SQLite supports local development; PostgreSQL is the production database.

## Start locally

Python 3.12 or newer is required.

```bash
git clone https://github.com/nasirsaikh/HelpDesk.git
cd HelpDesk
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux / macOS:
# source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env`, then run:

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py onboard_company --code TAKAFUL_OMAN --name "Takaful Oman" --admin-email nasir@example.com
python manage.py changepassword nasir@example.com
python manage.py runserver
```

Open `http://127.0.0.1:8000/`. `createsuperuser` creates a deliberate platform operator. It does not silently give that identity access to every company. Company administrator accounts use the portal to manage their own settings; `/admin/` provides platform company and access-grant administration.

To try a populated development workspace:

```bash
python manage.py seed_demo --allow-demo
```

The command prints a randomly generated password. `demo.admin@takaful.example` is Administrator in Takaful Oman and Auditor in ABC Insurance, making the company switcher and different permissions easy to review. Both companies intentionally have the same sample policy number. The command refuses to run with `DJANGO_DEBUG=0`.

## Included workflows

- Dashboard, company switcher, global search, responsive navigation and light/dark theme.
- Tickets with tenant-specific references, SLA deadlines, categories, support assignment, comments, internal notes, attachments, assigned approvals and validated workflow progression.
- Policy enrollment, multiple benefit plans, principal/dependent members, active/inactive member tabs and fullscreen member cards.
- Linked endorsements/claims, tasks, protected document downloads and company knowledge articles.
- Company settings for projects, products, categories, groups, organization types, workflows, SLA, notifications, mailbox credentials, email authority, AI providers, prompts and service accounts.
- Invitation acceptance for new users and existing global identities, without transferring roles between companies.
- Scoped API reads, report exports, queued jobs, tenant-aware Graph mailbox polling, notification outbox and scoped AI knowledge retrieval.
- SQL analytics against an authorized in-memory snapshot; generated SQL never runs against shared application tables.
- Temporary, reasoned, audited platform support sessions with a prominent portal banner.

See [architecture and authorization](docs/architecture.md), [operations](docs/operations.md), [requirement coverage](docs/requirement-coverage.md) and [API usage](docs/api.md).

## Verify

```bash
pip install -r requirements-dev.txt
ruff check .
ruff format --check .
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test apps.desk.tests
```

The test suite uses two companies with identical references, policy numbers, member IDs and filenames. It tests read/write isolation, forged foreign keys, direct database constraints, company switching, scoped roles, revoked grants, private downloads, scoped APIs, jobs, cache keys, AI retrieval, secrets and exports.

Browser checks run real forms, HTMX search, member tabs/fullscreen, company switching, different roles and desktop/mobile layouts:

```bash
npm ci
npx playwright install chromium
# Set DEMO_PASSWORD to the password printed by seed_demo.
# Linux / macOS:
DEMO_PASSWORD='your-demo-password' TEST_START_SERVER=1 DJANGO_PYTHON=.venv/bin/python npm run test:browser
# PowerShell:
# $env:DEMO_PASSWORD='your-demo-password'
# $env:TEST_START_SERVER='1'
# $env:DJANGO_PYTHON='.venv\Scripts\python.exe'
# npm run test:browser
```

Screenshots and browser results are written under `test-results/`. GitHub Actions runs both SQLite and native PostgreSQL suites.

## Production

Set a random `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, your allowed hosts, trusted CSRF origins and PostgreSQL credentials in `.env`. Serve behind HTTPS. Never mount private file storage as a public `/media/` directory.

```bash
docker compose build
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py collectstatic --noinput
docker compose run --rm web python manage.py createsuperuser
docker compose run --rm web python manage.py onboard_company --code TAKAFUL_OMAN --name "Takaful Oman" --admin-email nasir@example.com
docker compose up -d
```

Company data and files remain retained when a company is suspended or archived. Hard company deletion and impersonation are deliberately absent; retention/destruction and impersonation require dedicated operational designs.

Graph polling imports and classifies emails for review. Full OCR/attachment extraction and zero-touch endorsement execution are separate integrations; the tenancy and authority boundaries needed by them are already enforced. AI providers and SMTP/Graph services must be configured with your real infrastructure before external calls are made.
