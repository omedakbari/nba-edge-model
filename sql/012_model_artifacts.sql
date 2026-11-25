-- PROPOSED, NOT NEEDED YET (DECISIONS.md D14). Only apply if a cloud job ever builds the card.
-- One row per fitted artifact; the local refit would upsert it, the cloud would read it read-only.
create table if not exists model_artifacts (
    sport        text not null default 'nba',
    name         text not null,                 -- 'card_calibration'
    fit_at       timestamptz not null,
    n_slates     integer,
    body         jsonb not null,                -- data/processed/models/card_calibration.json
    published_at timestamptz not null default now(),
    primary key (sport, name)
);
alter table model_artifacts enable row level security;
do $$
declare api_role text;
begin
    foreach api_role in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = api_role) then
            execute format('revoke all on model_artifacts from %I', api_role);
        end if;
    end loop;
end $$;
