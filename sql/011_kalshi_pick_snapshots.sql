-- APPLIED 2026-11-25 as worker/migrations/011_kalshi_pick_snapshots.sql (that copy is the one migrate tracks;
-- don't edit it). This file is the reviewed original. DECISIONS.md D15. Nightly step on by default since then.
--
-- kalshi_pick_snapshots: the Kalshi price IN EFFECT at each slate's pick time (first tip - 60 min) for every
-- player-prop strike, archived by the nightly job pick_snapshots BEFORE prune compacts intraday quotes into
-- quote_history (which keeps only opening/closing prices). worker/prop_worker/replay.py reads it so the
-- calibration replay never has to fall back to opening quotes again. One row per (slate, ticker).
create table if not exists kalshi_pick_snapshots (
    sport           text not null default 'nba',
    slate_date      date not null,
    pick_at         timestamptz not null,          -- first tip of the slate - 60 min
    ticker          text not null,
    game_id         bigint references games (game_id) on delete cascade,
    player_id       bigint references players (player_id),
    stat            text not null,
    line            numeric(6, 2) not null,
    yes_ask         numeric(6, 4),
    no_ask          numeric(6, 4),
    fetched_at      timestamptz not null,          -- when this price was first stored (<= pick_at)
    last_seen_at    timestamptz not null,          -- last time it was still quoted
    archived_at     timestamptz not null default now(),
    primary key (sport, slate_date, ticker)
);
create index if not exists kalshi_pick_snapshots_game on kalshi_pick_snapshots (sport, game_id);

-- Same lockdown as 004/007/010: RLS on, no API-role access.
alter table kalshi_pick_snapshots enable row level security;
do $$
declare api_role text;
begin
    foreach api_role in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = api_role) then
            execute format('revoke all on kalshi_pick_snapshots from %I', api_role);
        end if;
    end loop;
end $$;
