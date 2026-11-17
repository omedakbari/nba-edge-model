-- bets: every recommended or placed bet, graded from game_logs (prop_worker.bets, nightly job "bets").
--   source   card (auto-logged from `slate --card`) | claude | discord | own
--   placed   false = recommended only. Stake / platform may be unknown (null): such bets count for hit
--            rate but not ROI.
--   line_or_strike  the over/under line; for Kalshi the strike as a half line (4+ = 3.5), so YES = over.
--   card_id  groups Sleeper legs entered together; the card wins only if every graded leg wins.
create table if not exists bets (
    id              bigserial primary key,
    sport           text not null default 'nba',
    slate_date      date not null,                         -- ET game date
    placed_at       timestamptz,
    platform        text check (platform in ('sleeper', 'kalshi')),
    card_id         text,
    player_id       bigint not null references players (player_id),
    stat            text not null check (stat in ('pts', 'reb', 'ast', 'fg3m', 'pra', 'ra')),
    side            text not null check (side in ('over', 'under', 'yes', 'no')),
    line_or_strike  numeric(6, 1) not null,
    price_cents     numeric(5, 1) check (price_cents > 0 and price_cents < 100),
    payout_mult     numeric(8, 3) check (payout_mult > 1),
    stake           numeric(10, 2) check (stake >= 0),
    p_at_placement  numeric(5, 4) check (p_at_placement between 0 and 1),
    source          text not null check (source in ('card', 'claude', 'discord', 'own')),
    placed          boolean not null default false,
    result          text check (result in ('win', 'loss', 'push', 'void')),
    actual_value    numeric(6, 1),
    graded_at       timestamptz,
    note            text,
    created_at      timestamptz not null default now(),
    check ((side in ('yes', 'no')) = (platform is not distinct from 'kalshi') or platform is null)
);
create index if not exists bets_slate on bets (sport, slate_date);
create index if not exists bets_ungraded on bets (sport, slate_date) where result is null;
-- card / claude rows are logged by tools and must not double up when a step is re-run
create unique index if not exists bets_logged_once
    on bets (sport, slate_date, source, coalesce(platform, '?'), player_id, stat, side, line_or_strike)
    where source in ('card', 'claude');

-- Same lockdown as 004/005: RLS on, no API-role access.
alter table bets enable row level security;
do $$
declare api_role text;
begin
    foreach api_role in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = api_role) then
            execute format('revoke all on bets from %I', api_role);
            execute format('revoke all on sequence bets_id_seq from %I', api_role);
        end if;
    end loop;
end $$;
