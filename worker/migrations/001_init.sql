-- 001_init.sql — Phase 2 schema (docs/BUILD_PLAN.md).
-- Applied by `python -m prop_worker.migrate`; runs in one transaction; safe to re-run (IF NOT EXISTS).
--
-- Conventions
--   * Every table has  sport text not null default 'nba'.
--   * Every quote / injury row has  source, provider_ts, fetched_at  (+ last_seen_at, last_provider_ts).
--   * Quotes and injury rows are stored only when the price/status changes: a BEFORE INSERT trigger
--     drops an unchanged row and instead bumps last_seen_at on the previous one. Freshness gates
--     read last_seen_at ("when did we last confirm this price"), not fetched_at ("when did it change").
--   * Team columns hold the canonical NBA.com abbreviation (= Kalshi's); ESPN's codes live in teams.
--   * RLS is enabled on every table with no policies: Supabase's anon/authenticated REST roles see
--     nothing. The worker connects as the database owner and is unaffected.

-- ── reference ───────────────────────────────────────────────────────────────

create table if not exists teams (
    team_id            serial primary key,
    sport              text not null default 'nba',
    abbr               text not null,                -- canonical: NBA.com / Kalshi
    name               text not null,
    nba_team_id        bigint,
    espn_team_id       text,
    espn_abbr          text,
    propline_team_id   text,                          -- 'nba:<nba_team_id>'
    propline_team_key  text,
    unique (sport, abbr),
    unique (sport, espn_abbr),
    unique (sport, nba_team_id)
);

insert into teams (abbr, name, nba_team_id, espn_team_id, espn_abbr, propline_team_key) values
    ('ATL', 'Atlanta Hawks',          1610612737, '1',  'ATL',  'hawks'),
    ('BOS', 'Boston Celtics',         1610612738, '2',  'BOS',  'celtics'),
    ('BKN', 'Brooklyn Nets',          1610612751, '17', 'BKN',  'nets'),
    ('CHA', 'Charlotte Hornets',      1610612766, '30', 'CHA',  'hornets'),
    ('CHI', 'Chicago Bulls',          1610612741, '4',  'CHI',  'bulls'),
    ('CLE', 'Cleveland Cavaliers',    1610612739, '5',  'CLE',  'cavaliers'),
    ('DAL', 'Dallas Mavericks',       1610612742, '6',  'DAL',  'mavericks'),
    ('DEN', 'Denver Nuggets',         1610612743, '7',  'DEN',  'nuggets'),
    ('DET', 'Detroit Pistons',        1610612765, '8',  'DET',  'pistons'),
    ('GSW', 'Golden State Warriors',  1610612744, '9',  'GS',   'warriors'),
    ('HOU', 'Houston Rockets',        1610612745, '10', 'HOU',  'rockets'),
    ('IND', 'Indiana Pacers',         1610612754, '11', 'IND',  'pacers'),
    ('LAC', 'LA Clippers',            1610612746, '12', 'LAC',  'clippers'),
    ('LAL', 'Los Angeles Lakers',     1610612747, '13', 'LAL',  'lakers'),
    ('MEM', 'Memphis Grizzlies',      1610612763, '29', 'MEM',  'grizzlies'),
    ('MIA', 'Miami Heat',             1610612748, '14', 'MIA',  'heat'),
    ('MIL', 'Milwaukee Bucks',        1610612749, '15', 'MIL',  'bucks'),
    ('MIN', 'Minnesota Timberwolves', 1610612750, '16', 'MIN',  'timberwolves'),
    ('NOP', 'New Orleans Pelicans',   1610612740, '3',  'NO',   'pelicans'),
    ('NYK', 'New York Knicks',        1610612752, '18', 'NY',   'knicks'),
    ('OKC', 'Oklahoma City Thunder',  1610612760, '25', 'OKC',  'thunder'),
    ('ORL', 'Orlando Magic',          1610612753, '19', 'ORL',  'magic'),
    ('PHI', 'Philadelphia 76ers',     1610612755, '20', 'PHI',  'sixers'),
    ('PHX', 'Phoenix Suns',           1610612756, '21', 'PHX',  'suns_nba'),
    ('POR', 'Portland Trail Blazers', 1610612757, '22', 'POR',  'trail_blazers'),
    ('SAC', 'Sacramento Kings',       1610612758, '23', 'SAC',  'kings_nba'),
    ('SAS', 'San Antonio Spurs',      1610612759, '24', 'SA',   'spurs'),
    ('TOR', 'Toronto Raptors',        1610612761, '28', 'TOR',  'raptors'),
    ('UTA', 'Utah Jazz',              1610612762, '26', 'UTAH', 'jazz'),
    ('WAS', 'Washington Wizards',     1610612764, '27', 'WSH',  'wizards')
on conflict (sport, abbr) do nothing;
update teams set propline_team_id = 'nba:' || nba_team_id where propline_team_id is null;

create table if not exists players (
    player_id   bigserial primary key,
    sport       text not null default 'nba',
    full_name   text not null,
    name_norm   text not null,                        -- prop_worker.names.normalize_name
    team_abbr   text,                                 -- current team (ESPN roster wins over last game log)
    team_as_of  date,                                 -- date of the evidence for team_abbr
    is_active   boolean not null default true,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    foreign key (sport, team_abbr) references teams (sport, abbr)
);
create index if not exists players_team_name on players (sport, team_abbr, name_norm);

-- One row per (source, external id). Long format so a player can carry several Kalshi fragments
-- (the fragment embeds team + jersey, so it changes on a trade) and every link records how it was made.
--   source       external_id example        how it is matched
--   nba          '1630567'                  seeded from nba_edge.db (defines the internal player)
--   propline     'nba:1630567'              direct join on the NBA.com id  (match_method = direct_id)
--   espn         '4683730'                  team + normalized name
--   kalshi       'UTABSENSABAUGH28'         team + normalized name
--   (espn/kalshi fallback: name unique league-wide AND our team for that player is unknown or from
--    before this season -> name_unique_stale_team. Current-season evidence of another team never links.)
create table if not exists player_xref (
    sport          text not null default 'nba',
    source         text not null check (source in ('nba', 'espn', 'kalshi', 'propline')),
    external_id    text not null,
    player_id      bigint not null references players (player_id) on delete cascade,
    external_name  text,
    team_abbr      text,
    match_method   text not null check (match_method in
                       ('seed', 'direct_id', 'team_name', 'team_last_initial', 'name_unique_stale_team',
                        'override')),
    first_seen     timestamptz not null default now(),
    last_seen      timestamptz not null default now(),
    primary key (sport, source, external_id)
);
create unique index if not exists player_xref_one_id_per_player
    on player_xref (sport, source, player_id) where source in ('nba', 'espn', 'propline');
create index if not exists player_xref_player on player_xref (player_id);

-- Anything a source mentions that we couldn't tie to a player. Never deleted; resolved_at is set
-- once a later link (or an override) matches it.
create table if not exists xref_unmatched (
    sport          text not null default 'nba',
    source         text not null,
    external_id    text not null,
    external_name  text,
    team_abbr      text,
    reason         text not null,
    candidates     jsonb,
    first_seen     timestamptz not null default now(),
    last_seen      timestamptz not null default now(),
    seen_count     integer not null default 1,
    resolved_at    timestamptz,
    primary key (sport, source, external_id)
);

-- Rows an ingest job could not load because a player didn't resolve. Re-applied once resolved.
create table if not exists pending_rows (
    id             bigserial primary key,
    sport          text not null default 'nba',
    target         text not null,                     -- e.g. 'game_logs'
    source         text not null,
    dedupe_key     text not null,                     -- e.g. 'espn:401909952:4683730'
    external_id    text,
    external_name  text,
    team_abbr      text,
    payload        jsonb not null,
    reason         text not null,
    created_at     timestamptz not null default now(),
    resolved_at    timestamptz,
    unique (sport, target, source, dedupe_key)
);

create or replace view player_xref_wide as
select p.sport, p.player_id, p.full_name, p.team_abbr, p.is_active,
       max(x.external_id) filter (where x.source = 'nba')      as nba_id,
       max(x.external_id) filter (where x.source = 'espn')     as espn_id,
       max(x.external_id) filter (where x.source = 'propline') as propline_id,
       array_remove(array_agg(x.external_id order by x.last_seen desc)
                    filter (where x.source = 'kalshi'), null)  as kalshi_fragments
from players p
left join player_xref x on x.player_id = p.player_id
group by p.sport, p.player_id, p.full_name, p.team_abbr, p.is_active;

-- ── games and box scores ────────────────────────────────────────────────────

create table if not exists games (
    game_id            bigserial primary key,
    sport              text not null default 'nba',
    game_date          date not null,                 -- ET calendar date
    season             text,                          -- '2026-27'
    season_type        text,                          -- 'Regular Season' | 'Playoffs' | ...
    start_time         timestamptz,
    home_team          text not null,
    away_team          text not null,
    status             text not null default 'scheduled'
                           check (status in ('scheduled', 'in_progress', 'final', 'postponed', 'canceled')),
    home_score         integer,
    away_score         integer,
    nba_game_id        text,
    espn_event_id      text,
    kalshi_event       text,                          -- '26NOV10MIAUTA'
    propline_event_id  text,
    source             text not null,
    provider_ts        timestamptz,
    fetched_at         timestamptz not null default now(),
    unique (sport, game_date, home_team, away_team),
    unique (sport, nba_game_id),
    unique (sport, espn_event_id),
    foreign key (sport, home_team) references teams (sport, abbr),
    foreign key (sport, away_team) references teams (sport, abbr)
);
create index if not exists games_date on games (sport, game_date);

create table if not exists game_logs (
    sport          text not null default 'nba',
    game_id        bigint not null references games (game_id) on delete cascade,
    player_id      bigint not null references players (player_id),
    team_abbr      text not null,
    opp_abbr       text not null,
    is_home        boolean not null,
    starter        boolean,
    did_not_play   boolean not null default false,
    dnp_reason     text,
    min            numeric(5, 2),
    pts            smallint, reb smallint, ast smallint, stl smallint, blk smallint, tov smallint,
    pf             smallint,
    fgm            smallint, fga smallint, fg3m smallint, fg3a smallint, ftm smallint, fta smallint,
    oreb           smallint, dreb smallint,
    plus_minus     smallint,
    source         text not null,                     -- 'espn' | 'nba_edge_db'
    provider_ts    timestamptz,
    fetched_at     timestamptz not null default now(),
    primary key (sport, game_id, player_id),
    foreign key (sport, team_abbr) references teams (sport, abbr),
    foreign key (sport, opp_abbr) references teams (sport, abbr)
);
create index if not exists game_logs_player on game_logs (player_id, game_id);

-- ── injuries ────────────────────────────────────────────────────────────────

create table if not exists injuries (
    id                bigserial primary key,
    sport             text not null default 'nba',
    source            text not null check (source in ('nba_pdf', 'espn')),
    player_key        text not null,                  -- '<TEAM>:<name_norm>', comparable across sources
    player_id         bigint references players (player_id),   -- null if unresolved (see xref_unmatched)
    external_name     text not null,
    team_abbr         text not null,
    game_date         date,                           -- the game the report is for (PDF); null for ESPN
    status            text not null,                  -- Out | Doubtful | Questionable | Probable | Available | Day-To-Day
    reason            text,
    source_url        text,
    provider_ts       timestamptz,                    -- PDF slot time / ESPN injury date
    fetched_at        timestamptz not null default now(),
    last_seen_at      timestamptz not null default now(),
    last_provider_ts  timestamptz,
    foreign key (sport, team_abbr) references teams (sport, abbr)
);
create index if not exists injuries_latest
    on injuries (sport, source, player_key, game_date, fetched_at desc, id desc);

-- Official report vs ESPN disagreements. One open row per player and date; resolved_at when they agree again.
create table if not exists injury_flags (
    id             bigserial primary key,
    sport          text not null default 'nba',
    game_date      date not null,
    player_key     text not null,
    player_id      bigint references players (player_id),
    nba_status     text,
    espn_status    text,
    nba_injury_id  bigint references injuries (id),
    espn_injury_id bigint references injuries (id),
    detected_at    timestamptz not null default now(),
    last_seen_at   timestamptz not null default now(),
    resolved_at    timestamptz
);
create unique index if not exists injury_flags_open
    on injury_flags (sport, game_date, player_key) where resolved_at is null;

-- ── markets and quotes ──────────────────────────────────────────────────────

create table if not exists kalshi_markets (
    sport                     text not null default 'nba',
    ticker                    text not null,
    event_ticker              text not null,
    series_ticker             text not null,
    market_kind               text not null check (market_kind in ('player_prop', 'game_winner', 'spread', 'total')),
    stat                      text,                   -- pts | reb | ast | fg3m | pra (props only)
    kalshi_fragment           text,                   -- 'UTABSENSABAUGH28' (props only)
    player_id                 bigint references players (player_id),
    game_id                   bigint references games (game_id),
    team_abbr                 text,
    title                     text,
    yes_sub_title             text,
    floor_strike              numeric,
    strike_type               text,
    line                      numeric,                -- e.g. 25+ points -> 24.5
    status                    text,
    result                    text,
    open_time                 timestamptz,
    close_time                timestamptz,
    expected_expiration_time  timestamptz,
    rules_primary             text,
    volume                    numeric,
    open_interest             numeric,
    source                    text not null default 'kalshi',
    provider_ts               timestamptz,
    fetched_at                timestamptz not null default now(),
    primary key (sport, ticker)
);
create index if not exists kalshi_markets_player on kalshi_markets (sport, player_id, stat);
create index if not exists kalshi_markets_event on kalshi_markets (sport, event_ticker);

create table if not exists kalshi_quotes (
    id                bigserial primary key,
    sport             text not null default 'nba',
    ticker            text not null,
    yes_bid           numeric(6, 4),                  -- dollars, 0..1
    yes_ask           numeric(6, 4),
    no_bid            numeric(6, 4),
    no_ask            numeric(6, 4),
    last_price        numeric(6, 4),
    yes_bid_size      numeric,
    yes_ask_size      numeric,
    status            text,
    source            text not null default 'kalshi',
    provider_ts       timestamptz,
    fetched_at        timestamptz not null default now(),
    last_seen_at      timestamptz not null default now(),
    last_provider_ts  timestamptz,
    foreign key (sport, ticker) references kalshi_markets (sport, ticker) on delete cascade
);
create index if not exists kalshi_quotes_latest on kalshi_quotes (sport, ticker, fetched_at desc, id desc);

create table if not exists book_quotes (
    id                  bigserial primary key,
    sport               text not null default 'nba',
    source              text not null,                -- 'propline'
    book                text not null,                -- 'draftkings', 'fanduel', ...
    event_id            text not null,                -- source's event id
    game_id             bigint references games (game_id),
    player_key          text not null,                -- 'nba:<id>' from PropLine, else 'name:<name_norm>'
    player_id           bigint references players (player_id),
    external_name       text,
    market              text not null,                -- source market key, e.g. 'player_points'
    stat                text,                         -- pts | reb | ast | fg3m | pra
    line_type           text,                         -- main | alternate | milestone
    line                numeric not null,
    side                text not null check (side in ('over', 'under')),
    price_american      integer,
    payout_multiplier   numeric,                      -- DFS books
    provider_ts         timestamptz,                  -- book/PropLine time this price was set
    fetched_at          timestamptz not null default now(),
    last_seen_at        timestamptz not null default now(),
    last_provider_ts    timestamptz
);
create index if not exists book_quotes_latest
    on book_quotes (sport, source, book, event_id, player_key, market, side, line, fetched_at desc, id desc);
create index if not exists book_quotes_player on book_quotes (sport, player_id, stat, fetched_at desc);

-- ── change-only triggers ────────────────────────────────────────────────────

create or replace function kalshi_quotes_change_only() returns trigger language plpgsql as $$
declare prev kalshi_quotes%rowtype;
begin
    select * into prev from kalshi_quotes
     where sport = new.sport and ticker = new.ticker
     order by fetched_at desc, id desc limit 1;
    if found
       and prev.yes_bid    is not distinct from new.yes_bid
       and prev.yes_ask    is not distinct from new.yes_ask
       and prev.no_bid     is not distinct from new.no_bid
       and prev.no_ask     is not distinct from new.no_ask
       and prev.last_price is not distinct from new.last_price
       and prev.status     is not distinct from new.status then
        update kalshi_quotes
           set last_seen_at = greatest(prev.last_seen_at, new.fetched_at),
               last_provider_ts = new.provider_ts
         where id = prev.id;
        return null;
    end if;
    new.last_seen_at := new.fetched_at;
    new.last_provider_ts := new.provider_ts;
    return new;
end $$;

create or replace function book_quotes_change_only() returns trigger language plpgsql as $$
declare prev book_quotes%rowtype;
begin
    select * into prev from book_quotes
     where sport = new.sport and source = new.source and book = new.book and event_id = new.event_id
       and player_key = new.player_key and market = new.market and side = new.side and line = new.line
     order by fetched_at desc, id desc limit 1;
    if found
       and prev.price_american    is not distinct from new.price_american
       and prev.payout_multiplier is not distinct from new.payout_multiplier then
        update book_quotes
           set last_seen_at = greatest(prev.last_seen_at, new.fetched_at),
               last_provider_ts = new.provider_ts
         where id = prev.id;
        return null;
    end if;
    new.last_seen_at := new.fetched_at;
    new.last_provider_ts := new.provider_ts;
    return new;
end $$;

create or replace function injuries_change_only() returns trigger language plpgsql as $$
declare prev injuries%rowtype;
begin
    select * into prev from injuries
     where sport = new.sport and source = new.source and player_key = new.player_key
       and game_date is not distinct from new.game_date
     order by fetched_at desc, id desc limit 1;
    if found
       and prev.status is not distinct from new.status
       and prev.reason is not distinct from new.reason then
        update injuries
           set last_seen_at = greatest(prev.last_seen_at, new.fetched_at),
               last_provider_ts = new.provider_ts,
               player_id = coalesce(prev.player_id, new.player_id)
         where id = prev.id;
        return null;
    end if;
    new.last_seen_at := new.fetched_at;
    new.last_provider_ts := new.provider_ts;
    return new;
end $$;

drop trigger if exists kalshi_quotes_change_only on kalshi_quotes;
create trigger kalshi_quotes_change_only before insert on kalshi_quotes
    for each row execute function kalshi_quotes_change_only();
drop trigger if exists book_quotes_change_only on book_quotes;
create trigger book_quotes_change_only before insert on book_quotes
    for each row execute function book_quotes_change_only();
drop trigger if exists injuries_change_only on injuries;
create trigger injuries_change_only before insert on injuries
    for each row execute function injuries_change_only();

-- ── model output, picks, grading (filled from Phase 4 on) ──────────────────

create table if not exists projections (
    id                 bigserial primary key,
    sport              text not null default 'nba',
    game_id            bigint not null references games (game_id),
    player_id          bigint not null references players (player_id),
    stat               text not null,
    model_version      text not null,
    projected_minutes  numeric(5, 2),
    mean               numeric,
    dist               text,                          -- 'negbin' | 'normal' | 'sim'
    dist_params        jsonb,
    inputs_as_of       timestamptz,                   -- newest input row the projection used
    created_at         timestamptz not null default now()
);
create index if not exists projections_lookup on projections (sport, game_id, player_id, stat, created_at desc);

create table if not exists picks (
    id              bigserial primary key,
    sport           text not null default 'nba',
    game_id         bigint not null references games (game_id),
    player_id       bigint not null references players (player_id),
    stat            text not null,
    venue           text not null,                    -- 'kalshi' | book key
    side            text not null check (side in ('over', 'under')),
    line            numeric not null,
    price_american  integer,
    price_dollars   numeric(6, 4),                    -- Kalshi
    kalshi_ticker   text,
    book_quote_id   bigint references book_quotes (id),
    kalshi_quote_id bigint references kalshi_quotes (id),
    projection_id   bigint references projections (id),
    p_model         numeric(6, 5),
    p_market        numeric(6, 5),
    edge            numeric(7, 5),
    stake           numeric(10, 2) not null default 0,
    status          text not null default 'research'
                        check (status in ('research', 'pending', 'approved', 'blocked', 'void')),
    blocked_reason  text,
    model_version   text,
    created_at      timestamptz not null default now()
);
create index if not exists picks_game on picks (sport, game_id);

create table if not exists results (
    id              bigserial primary key,
    sport           text not null default 'nba',
    pick_id         bigint not null unique references picks (id) on delete cascade,
    actual          numeric,
    outcome         text check (outcome in ('win', 'loss', 'push', 'void')),
    closing_price   numeric,
    clv             numeric,
    source          text not null,
    graded_at       timestamptz not null default now()
);

-- ── job bookkeeping ─────────────────────────────────────────────────────────

create table if not exists job_runs (
    id            bigserial primary key,
    sport         text not null default 'nba',
    job           text not null,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    duration_s    numeric(10, 3),
    status        text not null default 'running' check (status in ('running', 'green', 'red', 'error')),
    is_game_day   boolean,
    rows_in       integer,
    rows_out      integer,
    trailing_avg  numeric,
    red_reasons   text[],
    error         text,
    meta          jsonb,
    git_sha       text,
    runner        text
);
create index if not exists job_runs_job on job_runs (sport, job, started_at desc);

-- ── lock down the Supabase REST API (no policies = no anon/authenticated access) ──

do $$
declare t text;
begin
    foreach t in array array['teams', 'players', 'player_xref', 'xref_unmatched', 'pending_rows', 'games',
                             'game_logs', 'injuries', 'injury_flags', 'kalshi_markets', 'kalshi_quotes',
                             'book_quotes', 'projections', 'picks', 'results', 'job_runs'] loop
        execute format('alter table %I enable row level security', t);
    end loop;
end $$;
alter view player_xref_wide set (security_invoker = true);
