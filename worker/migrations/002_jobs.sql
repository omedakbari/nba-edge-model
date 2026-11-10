-- 002_jobs.sql — columns the ingest jobs and the red-run rules need.

-- job_runs: the trailing-average rule compares rows per game (or per event), not raw rows, so a
-- 3-game night isn't "under half" of a 12-game night. `expected` = this run was supposed to load
-- rows (e.g. boxscores with no unloaded finals is not expected to load anything).
alter table job_runs add column if not exists volume   numeric;
alter table job_runs add column if not exists expected boolean;
alter table job_runs add column if not exists warnings text[];
create index if not exists job_runs_trailing
    on job_runs (sport, job, started_at desc) where status = 'green' and is_game_day and expected;

-- injury_flags: what kind of disagreement (status_mismatch | missing_in_espn | missing_in_nba).
alter table injury_flags add column if not exists kind text;

-- lookups used by the kalshi and propline jobs
create index if not exists games_kalshi_event on games (sport, kalshi_event);
create index if not exists games_propline_event on games (sport, propline_event_id);
create index if not exists pending_rows_open on pending_rows (sport, target, source) where resolved_at is null;
