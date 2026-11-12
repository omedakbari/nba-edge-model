# Security

This app is for one person. The repo is public; the data and the keys are not. This page says what
anyone can see, where each secret lives, how to rotate each one, and what to do if one leaks.

## What is public

| Thing | Who can see it | What's in it |
|---|---|---|
| Code, migrations, tests, docs, `.github/` | Anyone | No secrets. `.env` is gitignored; gitleaks found nothing in the history (48 commits, 2026-11-11) |
| GitHub Actions logs | Anyone | Job summaries and errors. Secrets, and every value derived from them (DB host, user, project ref, password), are masked by the first step of every job (`python3 -m prop_worker.masking`), and the worker redacts them again before printing anything |
| Healthcheck artifact (`healthcheck.json`) | Anyone with a GitHub account, for 7 days | Probe names, PASS/FAIL, latency, redacted details. No hosts, users or keys |
| ntfy alerts | Anyone who knows the topic name | Job names and red reasons, redacted. The **topic name is the only thing that keeps them private**: ntfy.sh topics have no password |
| Supabase REST API (`https://<ref>.supabase.co/rest/v1`) | Nobody: the Data API is turned off (2026-11-11) | Nothing even if it's turned back on: row-level security is on for every table and `anon` / `authenticated` have no privileges (migration 004) |

## Where each secret lives

| Secret | Used by | Stored in |
|---|---|---|
| `SUPABASE_DB_URL` (contains the DB password) | worker: every job, migrate, backfill | GitHub → Settings → Secrets and variables → Actions; your Mac's `.env` |
| `PROPLINE_API_KEY` | `propline` and `validate` jobs, healthcheck | Same two places |
| `NTFY_TOPIC` | Alerts from `ingest` / `nightly` | Same two places; your phone's ntfy app subscription |
| GitHub fine-grained token `cron-job.org ingest dispatch` | cron-job.org, to start the ingest workflow every 15 min | cron-job.org job headers only (never in the repo or `.env`). Scope: this repo, *Actions: Read and write* only, 90-day expiry; see `docs/SCHEDULING.md` |
| Supabase anon / service-role keys | Nothing yet (Phase 3 web app) | Supabase dashboard only. Never commit; the service-role key bypasses RLS |

## Protections in place

- **Database:** RLS on every table. The API roles have no privileges on tables, views, sequences or functions, and none on tables the worker creates later. Views run as the caller (`security_invoker`), and functions use a fixed `search_path`. `python -m prop_worker.migrate` re-checks all of this and fails if it slips; the healthcheck flags any table without RLS.
  - **Expected WARN:** `migrate` shows a WARN line per API role for default grants owned by `supabase_admin` (Supabase's own platform role). They hand privileges on objects *that role* creates in `public` to `anon` / `authenticated`. `postgres` isn't a member of `supabase_admin`, so it can't revoke them. That's harmless here: the worker creates everything as `postgres`, the Data API is off, and any object `supabase_admin` did create would still FAIL the RLS and existing-privilege checks. A default grant owned by a role `postgres` *can* act for stays a FAIL.
- **Connection:** TLS is required (`sslmode=require`) for every non-local connection, so libpq refuses a server that won't encrypt. After connecting, the worker also checks the client side (`conn.pgconn.ssl_in_use`) and closes any remote connection without TLS. A failed connection's error is redacted, and its exception chain is dropped, so the URL can't reach a log.
  - **Don't judge TLS by `pg_stat_ssl`.** Through Supabase's pooler (Supavisor) it describes the pooler → Postgres hop inside Supabase, so it can say `ssl = false` while your connection is encrypted. The healthcheck shows both, and judges only the client side.
  - Optional, stricter: `sslmode=require` encrypts but doesn't check the server's certificate. To check it, download the CA certificate (Supabase → Project Settings → Database → SSL Configuration) and add `?sslmode=verify-full&sslrootcert=<path>` to `SUPABASE_DB_URL`. The worker keeps a `verify-*` mode from the URL. CI would need the certificate file too (it's public, so it can be committed).
- **Workflows:** every action is pinned to a commit SHA, and Dependabot proposes updates. Token permissions are `{}` by default, with jobs granted only `contents: read`. Checkout doesn't keep the token, there are no `pull_request_target` or `workflow_run` triggers, and no `${{ }}` appears inside shell scripts. `tests/test_workflows_security.py` fails if any of this regresses.
- **Output:** `tests/test_no_secret_leaks.py` plants fake secrets and checks every output path: stdout, step summary, ntfy body, artifact, `job_runs` rows and exceptions. On your Mac it also checks the real `.env` values.
- **GitHub settings (done 2026-11-11):**
  - default workflow token set to read-only
  - approval required for fork PRs
  - secret scanning and push protection on
  - Dependabot alerts on

## Rotating a secret

Every rotation ends the same way:
1. Update the value in `.env` on your Mac.
2. Run `gh secret set NAME` and paste the new value at the prompt.
3. Run the `healthcheck` workflow, plus a local `PYTHONPATH=worker ./venv/bin/python -m prop_worker.healthcheck`.

**Supabase database password**
1. Supabase dashboard → your project → Project Settings → Database → **Reset database password**.
2. Copy the new **session pooler** connection string (port 5432) and put the new password in it.
3. `SUPABASE_DB_URL` in `.env`, then `gh secret set SUPABASE_DB_URL`.
4. Until both are updated, ingest runs go red ("could not connect"); that's expected.

**PropLine API key**
1. In your PropLine account (prop-line.com), regenerate the API key. The old one stops working.
2. `PROPLINE_API_KEY` in `.env`, then `gh secret set PROPLINE_API_KEY`.

**ntfy topic**
1. Make a new unguessable name: `python3 -c "import secrets; print('nba-' + secrets.token_urlsafe(18))"`.
2. Subscribe to it in the ntfy app, and unsubscribe from the old one.
3. `NTFY_TOPIC` in `.env`, then `gh secret set NTFY_TOPIC`.
4. Send a test: `PYTHONPATH=worker ./venv/bin/python -c "from prop_worker.runner import send_ntfy; print(send_ntfy('test', 'new topic works'))"`.

**GitHub dispatch token** (cron-job.org): follow "Rotating the token" in `docs/SCHEDULING.md`, then delete the old token. If it leaks, the worst case is extra workflow runs (no-ops thanks to the gap guard) and someone reading or deleting run logs; it can't read secrets or change code.

## If a secret leaks

1. **Rotate it first**, using the steps above. The leaked value is dead once rotated; cleanup can wait.
2. **If it was the database password:** in Supabase → Logs → Postgres, look for connections you don't recognise. Then confirm nothing was opened up: `PYTHONPATH=worker ./venv/bin/python -m prop_worker.migrate --status` should show every security check OK.
3. **If it appeared in an Actions log or artifact:** open that run → ⋯ → **Delete all logs**, and delete the artifact.
4. **If it was committed:** rotating already made it useless. Then remove it from history, for example with `git filter-repo`, and force-push. Forks and caches may keep copies, which is why step 1 comes first. Resolve the GitHub secret-scanning alert afterwards.
5. **If it was the ntfy topic:** someone could read your alerts or send fake ones. Rotate the topic; nothing else is at risk.
6. Note what happened at the bottom of this file.

## Where to check

- **Supabase Security Advisor:** dashboard → your project → **Advisors** (left sidebar) → **Security Advisor** → Refresh. Expected: **0 errors**. "RLS Enabled No Policy" shows as INFO on each table, and that's intended: no policy means no API access at all.
- **GitHub:** repo → Security tab (secret scanning, Dependabot alerts); Settings → Actions → General (workflow permissions read-only, fork PR approval).
- **Data API:** turned off 2026-11-11 (Supabase → Project Settings → API). Nothing uses it. Decide again in Phase 3, when the web app chooses how it reads data.

## Incident log

_None._
