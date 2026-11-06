"""Same-book closing observations in verified pre-tipoff windows; never approvals."""
from datetime import timezone
from email.utils import parsedate_to_datetime
from src.data.context_archive import instant
from src.betting.quote_quality import timestamp_issue


def freshness(record, now):
    try:
        captured = instant(record.get('http_captured_at') or record['captured_at'])
        server = parsedate_to_datetime(record['http_date'])
        if server.tzinfo is None:
            raise ValueError('HTTP Date must include timezone')
        server = server.astimezone(timezone.utc)
        age = float(record.get('http_age') or 0)
        return dict(verified=(0 <= (now-captured).total_seconds() <= 900 and
                              abs((captured-server).total_seconds()) <= 60 and 0 <= age <= 900),
            http_date=record['http_date'], captured_at=captured.isoformat(),
            clock_delta_seconds=(captured-server).total_seconds(), http_age_seconds=age)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return dict(verified=False, reason='Missing/invalid HTTP freshness evidence')


def in_window(record, now, minutes=10):
    try:
        return record.get('status') == 'scheduled' and 0 < (instant(record['start_time'])-now).total_seconds() <= minutes*60
    except (KeyError, TypeError, ValueError):
        return False


def observations(games, props, team_picks, prop_picks, now, minutes=10):
    result, excluded = [], []
    games_by_id = {g['game_id']: g for g in games}
    for pick in team_picks:
        game = games_by_id.get(pick['game_id'])
        if not game or not in_window(game, now, minutes) or not in_window(game, instant(game['captured_at']), minutes): continue
        book = pick.get('odds_provider')
        fresh = freshness(game, now)
        if not book or game.get('odds_provider') != book or not fresh['verified']:
            excluded.append(dict(kind='team', event_id=pick['game_id'], reason='Book mismatch or unverified freshness'))
            continue
        result.append(dict(kind='team', event_id=pick['game_id'], book=book,
            captured_at=game['captured_at'], start_time=game['start_time'], source=game['source_url'],
            freshness=fresh, capture_verified=True, spread_home=game.get('spread'),
            total=game.get('over_under'), home_ml=game.get('home_ml'), away_ml=game.get('away_ml'),
            source_kind=game.get('price_kind'), approved=False, stake=0))
    for pick in prop_picks:
        quote = pick.get('quote') or {}; event_id = quote.get('event_id')
        game = games_by_id.get(event_id)
        if not game or not in_window(game, now, minutes): continue
        value = props.get((pick['player_name'], pick['stat_key'])); book = pick.get('book')
        if not value or value.get('event_id') != event_id: continue
        if not in_window(game, instant(value['captured_at']), minutes): continue
        pair = next((p for p in value['paired_quotes'] if p['book'] == book), None)
        if not pair:
            excluded.append(dict(kind='prop', event_id=event_id, reason='No same-book pair')); continue
        fresh = freshness(pair, now)
        issue = timestamp_issue(pair.get('updated_at'), value['captured_at'])
        if not fresh['verified'] or issue:
            excluded.append(dict(kind='prop', event_id=event_id, reason=issue or 'Unverified HTTP freshness')); continue
        result.append(dict(kind='prop', event_id=event_id, athlete_id=quote.get('espn_athlete_id'),
            player=pick['player_name'], stat=pick['stat_key'], book=book, line=pair['line'],
            over=pair['over'], under=pair['under'], captured_at=value['captured_at'],
            updated_at=pair['updated_at'], start_time=game['start_time'], source=pair['source_url'],
            freshness=fresh, capture_verified=True, approved=False, stake=0))
    return result, excluded
