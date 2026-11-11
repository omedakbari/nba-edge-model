-- 004_security.sql — lock the public schema down so only the worker's own role can touch data.
--
-- Supabase publishes the `public` schema through its REST API (PostgREST) to two built-in roles:
-- `anon` (anyone holding the project's public anon key) and `authenticated` (any logged-in app user).
-- This app never uses that API, so those roles get nothing. The worker connects as the database
-- owner (`postgres`), which owns every table, so none of this limits it.
-- Safe to run on a plain Postgres too (tests): role-specific steps are skipped if a role is absent.

do $$
declare
    r record;
    api_role text;
begin
    -- 1. Row-level security on every table. With RLS on and no policies, the API roles can read or
    --    write zero rows even if a grant slips through later. Owners are not affected.
    for r in select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relkind in ('r', 'p') loop
        execute format('alter table public.%I enable row level security', r.relname);
    end loop;

    -- 2. Views run with the permissions of whoever queries them (security_invoker), so a view can
    --    never be used to read past RLS with the owner's rights.
    for r in select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relkind = 'v' loop
        execute format('alter view public.%I set (security_invoker = true)', r.relname);
    end loop;

    -- 3. Functions use a fixed search_path, so nobody can trick them into calling a look-alike
    --    object placed earlier on the path.
    for r in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = 'public' and p.prokind = 'f' loop
        execute format('alter function %s set search_path = public, pg_catalog', r.sig);
    end loop;

    -- 4. Take away every privilege the API roles have on existing objects, and make future objects
    --    created by this role (the worker / migrations) start locked too.
    foreach api_role in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = api_role) then
            execute format('revoke all on all tables in schema public from %I', api_role);      -- tables + views
            execute format('revoke all on all sequences in schema public from %I', api_role);
            execute format('revoke all on all functions in schema public from %I', api_role);
            execute format('alter default privileges in schema public revoke all on tables from %I', api_role);
            execute format('alter default privileges in schema public revoke all on sequences from %I', api_role);
            execute format('alter default privileges in schema public revoke all on functions from %I', api_role);
        end if;
    end loop;

    -- 5. Postgres lets every role (PUBLIC) execute new functions by default; stop that as well.
    revoke all on all functions in schema public from public;
    alter default privileges in schema public revoke execute on functions from public;
end $$;
