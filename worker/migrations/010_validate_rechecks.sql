-- validate_rechecks: every stat mismatch the nightly validate job finds on an ESPN game_logs row.
-- The next nightly run re-fetches that game's ESPN box score. If ESPN's value has changed since,
-- the stored stat is updated and graded bets on that player / stat / date are regraded.
--   stat           game_logs column (pts, reb, ..., min)
--   stored_value   game_logs value when the mismatch was found
--   other_value    what the second source (other_label: PropLine | nba_edge.db) said
--   severity       yellow (one-off blk/stl/tov off by 1) | red
--   found_on       the game date validated
--   outcome        null = not rechecked yet; unchanged | updated | not_in_espn | espn_problem
--   espn_value     ESPN's value at recheck time
create table if not exists validate_rechecks (
    id              bigserial primary key,
    sport           text not null default 'nba',
    game_id         bigint not null references games (game_id) on delete cascade,
    player_id       bigint not null references players (player_id),
    stat            text not null,
    stored_value    numeric(6, 2),
    other_value     numeric(6, 2),
    other_label     text not null,
    severity        text not null check (severity in ('yellow', 'red')),
    found_on        date not null,
    found_at        timestamptz not null,
    checked_at      timestamptz,
    outcome         text check (outcome in ('unchanged', 'updated', 'not_in_espn', 'espn_problem')),
    espn_value      numeric(6, 2),
    bets_regraded   integer,
    unique (sport, game_id, player_id, stat)
);
create index if not exists validate_rechecks_open on validate_rechecks (sport, found_on) where checked_at is null;

-- Same lockdown as 004/007: RLS on, no API-role access.
alter table validate_rechecks enable row level security;
do $$
declare api_role text;
begin
    foreach api_role in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = api_role) then
            execute format('revoke all on validate_rechecks from %I', api_role);
            execute format('revoke all on sequence validate_rechecks_id_seq from %I', api_role);
        end if;
    end loop;
end $$;
