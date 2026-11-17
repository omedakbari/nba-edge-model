-- bets: the tip time of the bet's game, stored when a card leg is logged. Lets "dropped" be judged by
-- the game itself rather than the player's current team (a traded player, or a game whose start
-- time was missing, was never marked dropped).
alter table bets add column if not exists game_start timestamptz;
