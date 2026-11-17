-- bets: card legs are re-logged on every `slate --card --log` run until their game starts.
--   last_logged_at  when p / price were last refreshed from the card (created_at = first seen)
--   dropped_at      the leg left the card before its game started (still graded; cleared if it returns)
alter table bets add column if not exists last_logged_at timestamptz;
alter table bets add column if not exists dropped_at timestamptz;
