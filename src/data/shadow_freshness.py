"""Shadow-only source freshness comparison. Never supplies production selections."""
from collections import Counter, defaultdict
import math
from src.data.context_archive import instant
from src.data.closing import freshness
from src.betting.quote_quality import timestamp_issue


def compare(rows, scoreboard, http_evidence, captured_at, *, capture_minutes=5, future_tolerance_seconds=60, provider_inventory=None):
    if not math.isfinite(capture_minutes) or capture_minutes <= 0 or not math.isfinite(future_tolerance_seconds) or future_tolerance_seconds < 0:
        raise ValueError('Invalid shadow-only policy parameters')
    now=instant(captured_at); events={e['id']:e for e in scoreboard.get('events',[])}
    groups=defaultdict(Counter); shadow=[]; current=[]; anomalies=[]
    provider_ids={(r['event_id'],r['provider'].get('name')):str(r['provider'].get('id')) for r in provider_inventory or []}
    for row in rows:
        eid,_,book,aid,stat,side,_,_,line,price,updated=row
        counts=groups[(book,stat)]; counts['offered_sides']+=1
        current_issue=timestamp_issue(updated,captured_at)
        if current_issue is None: current.append(row); counts['current_eligible_sides']+=1
        reason=None
        event=events.get(eid,{})
        try:
            if event.get('status',{}).get('type',{}).get('state')!='pre' or instant(event['date'])<=now:
                reason='not pregame'
            delta=(instant(updated)-now).total_seconds()
            if delta>0:
                anomalies.append(dict(event_id=eid,athlete_id=aid,stat=stat,book=book,side=side,updated_at=updated,future_seconds=delta))
                counts['future_anomaly_sides']+=1
            if delta>future_tolerance_seconds: reason='future beyond tolerance'
            evidence=[r for r in http_evidence if f'/events/{eid}/' in r['source_url'] and '/propBets?' in r['source_url']]
            pid=provider_ids.get((eid,book))
            if pid: evidence=[r for r in evidence if f'/odds/{pid}/' in r['source_url']]
            elif len({r['source_url'] for r in evidence})!=1: evidence=[]
            evidence=[r for r in evidence if r.get('http_status')==200]
            if not evidence:
                reason=reason or 'missing unambiguous HTTP source'
            else:
                http=max(evidence,key=lambda r:instant(r['captured_at']))
                source=dict(http_captured_at=http['captured_at'],http_date=http.get('http_date'),http_age=http.get('age'))
                check=freshness(source,now)
                age=(now-instant(http['captured_at'])).total_seconds()+float(http.get('age') or 0)
                if not check['verified'] or not 0<=age<=capture_minutes*60: reason=reason or 'unverified/stale feed capture'
            if side not in ('over','under') or not math.isfinite(float(line)) or not math.isfinite(float(price)) or float(price)<=1:
                reason=reason or 'invalid current price'
        except (KeyError,TypeError,ValueError,AttributeError):
            reason=reason or 'invalid source/update/start timestamp'
        if reason: counts['shadow_rejected: '+reason]+=1
        else: shadow.append(row);counts['shadow_eligible_sides']+=1
    from src.data.espn_props import no_vig_consensus
    current_pairs=no_vig_consensus(rows,True,captured_at=captured_at)
    # Deliberately isolated from the live scanner: apply only the shadow filter
    # above, retaining existing pairing/price checks but not the legacy age cap.
    shadow_pairs=no_vig_consensus(shadow,True)
    def paired_by_group(pairs):
        return Counter((p['book'],stat) for (_,stat),v in pairs.items() for p in v['paired_quotes'])
    cp,sp=paired_by_group(current_pairs),paired_by_group(shadow_pairs)
    for counts in groups.values():
        for key in ['offered_sides','current_eligible_sides','shadow_eligible_sides','future_anomaly_sides']:
            counts.setdefault(key,0)
    supported={'pts','reb','ast','fg3m','pra'}
    return dict(shadow_only=True,captured_at=captured_at,capture_minutes=capture_minutes,
        future_tolerance_seconds=future_tolerance_seconds,
        current_eligible_sides=len(current),shadow_eligible_sides=len(shadow),
        current_paired_props=len(current_pairs),shadow_paired_props=len(shadow_pairs),
        shadow_supported_stat_pairs=sum(stat in supported for _,stat in shadow_pairs),
        future_anomaly_sides=len(anomalies),future_anomalies=anomalies,
        by_book_stat=[dict(book=book,stat=stat,**dict(counts),current_paired_props=cp[(book,stat)],shadow_paired_props=sp[(book,stat)])
                      for (book,stat),counts in sorted(groups.items())],
        limitations=['Paired source markets, before player eligibility/model calibration; never approved forecasts'],approved=False,maximum_entry_cost=0)
