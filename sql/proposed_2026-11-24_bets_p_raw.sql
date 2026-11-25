-- PROPOSED, NOT APPLIED (Phase 2 is read-only against Supabase). Apply yourself if you want a real column
-- instead of the 'p_raw=…, mid=…' text the card now appends to bets.note (DECISIONS.md D13).
alter table bets add column if not exists p_raw numeric;
alter table bets add column if not exists market_mid numeric;
comment on column bets.p_at_placement is 'card: CALIBRATED p the rules used (from 2026-11-25); before: raw formula p';
comment on column bets.p_raw is 'card: formula p before calibration';
comment on column bets.market_mid is 'card: market mid / no-vig P for the chosen side at logging';
-- backfill from the note text written since 2026-11-25
update bets set p_raw = substring(note from 'p_raw=([0-9.]+)')::numeric
 where p_raw is null and note like '%p_raw=%';
update bets set market_mid = substring(note from 'mid=([0-9.]+)')::numeric
 where market_mid is null and note like '%mid=%';
