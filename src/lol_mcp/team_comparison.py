"""Compute tier counts and relative sides from the current host roster."""
from collections import Counter


def compare_team_tiers(state, lookup):
    players = state.get('players', [])
    slot = state.get('activePlayer', {}).get('slot')
    host = next((p for p in players if slot and p.get('slot') == slot), None)
    teams = {p.get('team') for p in players}
    if not state.get('available') or not host or len(teams) != 2 or None in teams or len(players) != 10:
        return {'status': 'unavailable', 'reason': 'complete_host_roster_required'}
    ours = host['team']
    opponent = next(t for t in teams if t != ours)
    result = {'status': 'ok', 'source': 'OP.GG champion tiers + current host roster',
              'teams': [], 'gameTimeSeconds': state.get('game', {}).get('gameTimeSeconds'),
              'interpretation': 'Tier-only comparison, lower number is stronger; not a win probability. Use computed counts/verdict without recalculating.'}
    values = []
    for team in (ours, opponent):
        members = [p for p in players if p['team'] == team]
        if len(members) != 5:
            return {'status': 'unavailable', 'reason': 'five_players_per_team_required'}
        rows = []
        for player in members:
            record = lookup(player.get('championSlug') or player['champion'])
            tier = record.get('championTier')
            if record.get('status') != 'ok' or type(tier) is not int or not 0 <= tier <= 5:
                return {'status': 'unavailable', 'reason': 'missing_champion_tier', 'champion': player.get('champion')}
            rows.append({'champion': player['champion'], 'tier': tier,
                         'tierLabel': 'OP' if tier == 0 else f'T{tier}', 'source': record.get('source')})
        counts = Counter(r['tier'] for r in rows)
        values.append(counts)
        result['teams'].append({'label': '你们这边' if team == ours else '对面',
                                'players': rows, 'tierCounts': {('OP' if t == 0 else f'T{t}'): n for t,n in sorted(counts.items())}})
    common = values[0] & values[1]
    left, right = [sorted((c-common).elements()) for c in values]
    if not left:
        verdict, answer = 'equal', '两边英雄评级打平。'
    elif all(a <= b for a,b in zip(left,right)):
        verdict, answer = 'your_team', '只看英雄评级，你们这边更强。'
    elif all(a >= b for a,b in zip(left,right)):
        verdict, answer = 'opponent', '只看英雄评级，对面更强。'
    else:
        verdict, answer = 'mixed', '两边评级各有高低，不能只凭评级分出明确优势。'
    result.update(verdict=verdict, answer=answer, remainingTiers={'你们这边': left, '对面': right})
    return result
