# i-connects Django MVP — Status (Phase 6)

The Blitz.js -> Django + HTMX rewrite is feature-complete for MVP. The
server boots, the major URLs return 200, and a smoke test suite guards
the critical behaviours.

## What's built

Eight Django apps ported from the original Prisma / Blitz schema:

| App              | Highlights                                                                                  |
| ---------------- | ------------------------------------------------------------------------------------------- |
| `accounts`       | Custom `User` (email login), signup/login/logout, password reset, profile, 10 default widgets auto-seeded via `post_save`, ORCID social auth via django-allauth. |
| `core`           | Shared enums, email backends (console / SMTP / Resend / SES), `setup_cron` management command, base templates. |
| `projects`       | Project CRUD, membership, privileges (PM / contributor), invitations with URL-safe tokens, member row HTMX partials. |
| `roles`          | Per-project Role model + CRUD.                                                              |
| `tasks`          | Kanban board with drag-drop (HTMX move endpoint), List view with search / filter / sort, calendar view, task CRUD, task completion + approval, peer review of each assignee's submission by the task's reviewers, individual start dates and deadlines a Lead sets per assignee (or for everyone not yet done) on top of the task's dates (moving the task's start or due date asks the Lead whether to shift, keep or remove the members' own), a timezone per task governing its start and due dates and an optional one per assignee governing their own dates (every picker starts on the user's system timezone), with an Extended state for a task whose deadline has passed while everyone still working is on a later one, milestones, task logs for audit trail. Web addresses in a task's description are links, asking before they leave the site, as in comments. |
| `comments`       | Comments on tasks, open to the task's creator, assignees and reviewers (everyone else in the project reads them, with no comment box shown). The commenter's name and each tag link to the person's profile, and web addresses in a comment are links that open in a new tab, after a "Leaving i-CONNECTS" dialog shows where they go (links back into i-CONNECTS skip it). Only the project's contributors (its Contributors list) can be tagged: as `@Name`, picked from an @ list that shows and searches each one's name and email, or as `@email` typed out. A tagged contributor gets an in-app notification and an email (`email_comment_mention`, on by default); an edit emails only the people it adds. |
| `notifications`  | In-app notifications (M2M recipients), badge / dropdown partials, admin announcements.      |
| `notes`          | Project notes with three visibility tiers (private / PM-only / contributors), markdown rendering with raw HTML escaping. |
| `wiki`           | Per-project wiki pages: markdown + `[[wiki links]]`, append-only `WikiRevision` history with diffs / restore, lead-only page locking, edit-conflict detection, search. |
| `forms_builder`  | Dynamic JSON-Schema -> Django Form renderer, versioned `FormVersion`s, jsonschema validation. Surfaced as **Surveys**; the module page is a placeholder for now. |
| `communications` | Placeholder module — a sidebar page and a project section, both empty. No models yet. |

Plus `cron/tasks.py` with four Celery-beat jobs (daily digest, weekly
digest, overdue-task reminders, tmp-file cleanup), scheduled via
`python manage.py setup_cron`.

## How to run

### Docker (production-shaped)

```bash
cd iconnects-django
cp .env.example .env
docker compose up --build
```

Brings up Postgres, Redis, Gunicorn web (port 8000), a Celery worker,
and a Celery beat scheduler.

### Local (no Docker)

```bash
cd iconnects-django
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Point DATABASE_URL at a local Postgres (or use SQLite).
export DJANGO_SETTINGS_MODULE=config.settings.dev
python manage.py migrate
python manage.py setup_cron    # once, to seed beat schedule
python manage.py runserver
```

## Test results

Pytest + pytest-django, with an in-memory SQLite DB and an eager Celery
task executor (`config/settings/test.py`).

```
$ pytest
============================= test session starts ==============================
collected 27 items
...
======================= 27 passed, 17 warnings in 2.21s ========================
```

Coverage is deliberately smoke-level:

- **accounts (6)** — signup, default widgets, login (valid + invalid), logout POST-only, profile login-required.
- **projects (6)** — public vs. private list, creator-becomes-PM, PM-only edit, invitation code shape, accept-invitation flow.
- **tasks (4)** — HTMX move endpoint + membership check, service-layer completion, PM-only approval.
- **notifications (4)** — service helper, `for_user` queryset, mark-read endpoint, badge count.
- **forms_builder (3)** — schema-to-form field wiring, jsonschema validation, version cloning.
- **notes (2)** — private-note visibility rule, markdown HTML escaping.
- **core / cron (2)** — daily digest task no-ops on empty queryset, `setup_cron` creates the 4 periodic tasks.

No bugs were found in phases 3-5 while writing these tests.

## Docker smoke test

Docker Desktop is not installed on this machine, so the usual
`docker compose config` validation could not be executed. The
`docker-compose.yml` is a well-formed YAML document with 5 services
(`db`, `redis`, `web`, `celery`, `celery-beat`) and one named volume
(`postgres_data`). Values match `.env.example`. Verifying `docker
compose up --build` end-to-end is a post-MVP task for any machine with
Docker available.

## Known gaps / post-MVP items

- **Comments / roles apps** have no tests yet (out of scope for the MVP
  smoke suite).
- **ORCID social auth** is wired via django-allauth adapters but not
  tested end-to-end (requires a live OAuth round-trip).
- **Email delivery** beyond the console backend is configured but not
  exercised in CI — Resend / SES need live credentials to verify.
- **HTMX board interactions** are covered at the endpoint level, not
  via full browser tests; a Playwright smoke test would help before the
  first production release.
- **Docker build** unverified on this machine (no Docker Desktop). The
  Dockerfile and docker-compose.yml should work but have not been run.
- **Frontend assets** (Tailwind) are served via WhiteNoise but a proper
  production build (CSS purge) is deferred.
- **Data migration from Blitz/Prisma** — the `Session` and `Token`
  models are retained for shape compatibility but no loader script has
  been written.

## References

- Full plan: `~/.claude/plans/recursive-crunching-harbor.md`
- Project root: `iconnects-django/`
- Test config: `pytest.ini`, `conftest.py`, `config/settings/test.py`
