-- 005_quote_history.sql — compact settled-line history (approved 2026-11-11).
--
-- Once a game started more than 2 days ago, the prune job folds every quote line into ONE row here
-- and deletes that line's rows from book_quotes / kalshi_quotes (except rows a pick references,
-- which stay in full). Measured: ~2 full rows per line side in book_quotes vs one narrow row here.
--
--   venue 'book'    one row per (source, book, event, player, market, line); over and under sides
--                   together; prices are American odds (price_unit 'american').
--   venue 'kalshi'  one row per ticker; over = YES ask, under = NO ask, in dollars (price_unit
--                   'dollars'). YES bid = 1 - NO ask, so the asks carry the whole top of book.
--                   Markets without a strike (game winner) use line 0.
-- opening = first stored price per side; closing = last price stored at or before tip-off.
-- line_key = md5 of the identifying columns as a uuid: a 16-byte primary key instead of an
-- 8-column text key (measured: the wide key's index was nearly as large as the data).

create table if not exists quote_history (
    line_key        uuid primary key,              -- quote_history_line_key(...)
    sport           text not null default 'nba',
    venue           text not null check (venue in ('book', 'kalshi')),
    source          text not null,                 -- 'propline' | 'kalshi'
    book            text not null,                 -- 'draftkings' ... | 'kalshi'
    event_key       text not null,                 -- PropLine event id | Kalshi ticker
    player_key      text not null default '',
    market          text not null,                 -- 'player_points' ... | Kalshi series
    line            numeric not null default 0,
    game_id         bigint references games (game_id),
    player_id       bigint references players (player_id),
    stat            text,
    price_unit      text not null check (price_unit in ('american', 'dollars')),
    open_over       numeric,
    open_under      numeric,
    close_over      numeric,
    close_under     numeric,
    opened_at       timestamptz,
    closed_at       timestamptz,
    rows_compacted  integer not null,
    compacted_at    timestamptz not null default now()
);

create or replace function quote_history_line_key(sport text, venue text, source text, book text,
                                                  event_key text, player_key text, market text, line numeric)
returns uuid language sql immutable set search_path = public, pg_catalog as $$
    select md5(concat_ws('|', sport, venue, source, book, event_key, player_key, market,
                         trim_scale(line)::text))::uuid
$$;
create index if not exists quote_history_player on quote_history (player_id, stat);

-- Same lockdown as 004 (default privileges from 004 already cover grants; RLS must be per table).
alter table quote_history enable row level security;
do $$
declare api_role text;
begin
    foreach api_role in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = api_role) then
            execute format('revoke all on quote_history from %I', api_role);
            execute format('revoke all on function quote_history_line_key(text, text, text, text, text, text, text, numeric) from %I', api_role);
        end if;
    end loop;
end $$;
revoke all on function quote_history_line_key(text, text, text, text, text, text, text, numeric) from public;
