-- 003_book_quotes_fill_ids.sql — when an unchanged book quote is seen again, also fill in a
-- player_id / game_id the earlier row didn't have (e.g. a player who resolved after the row was
-- stored). Same change-only behaviour as 001 otherwise.

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
               last_provider_ts = new.provider_ts,
               player_id = coalesce(prev.player_id, new.player_id),
               game_id = coalesce(prev.game_id, new.game_id)
         where id = prev.id;
        return null;
    end if;
    new.last_seen_at := new.fetched_at;
    new.last_provider_ts := new.provider_ts;
    return new;
end $$;
