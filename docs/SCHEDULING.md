# Scheduling

The ingest workflow has **two triggers**, so a slot is covered when either one fires:

1. **GitHub's own cron** (`on: schedule` in `.github/workflows/ingest.yml`). Free, but GitHub runs
   scheduled workflows "best effort". Runs can start late or be skipped, especially at :00 and
   :15, when load is highest. On 2026-11-11 the schedule produced **no runs at all** for about
   3 hours after it landed on `main`. Nothing in the repo was wrong:
   - the workflow was active and on the default branch
   - the cron lines were valid
   - Actions was enabled for all actions
   - a manual `workflow_dispatch` run (43125108603) succeeded
2. **An external cron** (cron-job.org, free) that calls the workflow's `workflow_dispatch` API
   every 15 minutes. It doesn't depend on your Mac.

Both pass `--window 09:00-01:00 --min-gap-minutes 10` to the worker:
- **`--window`:** a run outside 9am–1am ET exits quietly.
- **`--min-gap-minutes 10`:** a run exits quietly if any ingest job already ran (green or red) in the last 10 minutes, from anywhere: either trigger or a local loop. That keeps a slot from running twice and spending PropLine quota twice. An *errored* run doesn't count, so the next trigger retries.

The nightly workflow (6am ET) has only GitHub's cron, on purpose. **Don't add a cron-job.org job for it.** A `workflow_dispatch` run skips the `--only-at-et-hour 6` check, and nightly has no gap guard. If both triggers fired, the 10-player PropLine validation would run twice and spend quota twice. If a morning's run is missing by 7am ET, start it by hand: `gh workflow run nightly.yml` (or Actions → nightly → Run workflow). If it's skipped often, nightly needs its own once-a-day guard first, which is a code change for after the gate.

## One-time setup (about 10 minutes)

### 1. Make a token that can only start workflows in this repo

GitHub → your avatar → **Settings → Developer settings → Personal access tokens → Fine-grained
tokens → Generate new token**:

- **Name:** `cron-job.org ingest dispatch`
- **Expiration:** 90 days. Put a calendar reminder to rotate it; the steps are below.
- **Repository access:** *Only select repositories* → `nba-edge-model`
- **Permissions → Repository permissions → Actions: Read and write.** Leave everything else at *No access*. *Metadata: Read* is added automatically.

What this token can do: start, re-run and cancel workflow runs in this repo, and read or delete their logs and artifacts. What it **can't** do: read secrets, change code or workflows, or touch any other repo. If it leaks, the worst case is extra runs: the gap guard and the 9am–1am window make those no-ops, and the Actions minutes are free on a public repo.

### 2. Test the token from your Mac (expect `204`)

```
read -rs GH_DISPATCH_TOKEN
```
Paste the token and press Enter. Nothing is shown, and it stays out of your shell history. Then:
```
curl -sS -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $GH_DISPATCH_TOKEN" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" https://api.github.com/repos/omedakbari/nba-edge-model/actions/workflows/ingest.yml/dispatches -d '{"ref":"main"}'
```
`204` means a run was queued (Actions → ingest). `401` means the token is wrong, and `403` means it lacks *Actions: Read and write*.

### 3. Create the cron-job.org job

Sign up at <https://cron-job.org> → **Create cronjob**:

- **Common tab:**
  - **Title:** `nba ingest`
  - **URL:** `https://api.github.com/repos/omedakbari/nba-edge-model/actions/workflows/ingest.yml/dispatches`
  - **Execution schedule → Custom:** minutes `0,15,30,45`; hours `0,1,9-23`; every day, every month
  - **Time zone:** `America/New_York`. cron-job.org handles daylight saving itself; the worker's window trims 1:15–1:45.
- **Advanced tab:**
  - **Request method:** `POST`
  - **Headers:**
    - `Authorization: Bearer <token>`
    - `Accept: application/vnd.github+json`
    - `X-GitHub-Api-Version: 2022-11-28`
    - `Content-Type: application/json`
  - **Request body:** `{"ref":"main"}`
  - **Timeout:** 30 s
  - **Treat redirects as success:** off
- **Notifications:** email on failure, after 2 consecutive failures.

Save, then **Test run**: expect HTTP 204, and a new `workflow_dispatch` run in `gh run list --workflow=ingest.yml`.

### Rotating the token

1. Generate a new token with the same settings (step 1).
2. Paste it into the cron-job.org job's `Authorization` header.
3. Run **Test run** and check for 204.
4. Delete the old token on GitHub.

## Checking that slots are covered

`PYTHONPATH=worker ./venv/bin/python -m prop_worker.status --days 1` lists every run. Between 9am and 1am ET there should be a run about every 15 minutes. On GitHub, `gh run list --workflow=ingest.yml -L 20` shows which trigger started each run: `schedule` or `workflow_dispatch`. A run that exits with "already ran … nothing to do" is the second trigger of a slot, which is expected.
