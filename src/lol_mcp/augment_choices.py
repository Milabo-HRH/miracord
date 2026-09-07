"""Separate identity browsing from statistics for player-supplied options."""
import json
import math
from .name_catalog import normalize_name


IDENTITY_FIELDS = ('id', 'key', 'name', 'nameEn', 'nameZh', 'namesByLocale', 'namesByRegion', 'idNamespace', 'rarity')


async def identify(executor, champion, rarity=None, query=''):
    if query:
        resolved = executor.context.names.resolve_augment(query)
        if resolved.get('status') == 'ok':
            row = resolved['augment']
            return {'status': 'ok', 'query': query, 'match': 'exact',
                    'augments': [{'id': row['augmentId'], **{k: row[k] for k in IDENTITY_FIELDS if k in row}}],
                    'interpretation': 'Identity only. No strength or current eligibility claim.'}
    # Unknown color is not a reason to guess one or make three serial calls.
    rarity = rarity or 'all'
    args = {'champion': champion, 'all_matches': True}
    if rarity != 'all':
        args['rarity'] = rarity
    raw = json.loads(await executor.execute('get_mayhem_augments', json.dumps(args)))
    candidates = [{k: row[k] for k in IDENTITY_FIELDS if k in row} for row in raw.get('augments', [])]
    needle = normalize_name(query)
    matches = [row for row in candidates if needle and any(
        needle in normalize_name(name) for name in [row.get('nameEn', ''), row.get('nameZh', ''),
                                                   *row.get('namesByLocale', {}).values()])]
    # Chinese players commonly shorten a hat name to 帽子. Match the noun
    # against verified locale names, and accept only a unique filtered candidate.
    if not matches and query == '帽子':
        matches = [row for row in candidates if any('帽' in name for name in row.get('namesByLocale', {}).values())]
    resolved = matches[0] if len(matches) == 1 else None
    return {'status': raw.get('status'), 'reason': raw.get('reason'), 'query': query,
            'match': 'unique_candidate' if resolved else 'candidates', 'rarity': rarity, 'source': raw.get('source'),
            'resolvedAugment': resolved, 'augments': candidates,
            'nextAction': {'tool': 'compare_mayhem_choices', 'champion': champion,
                           'options': [resolved.get('nameEn') or resolved.get('name')] if resolved else [],
                           'instruction': 'After identifying the requested option, pass its verified name in options. This result has NO performance; do not answer a strength question until comparison returns statistics.'},
            'interpretation': 'Complete filtered identity candidates, NOT offered choices. No performance or ranking is supplied. Identify only the requested name; never recommend alternatives. Absence is not proof of ineligibility.'}


async def compare(executor, champion, options, rarity=None, include_descriptions=False):
    """Resolve exactly 1–3 supplied names; fallback compares the same set."""
    choices = []
    for option in options:
        identity = None
        resolved = executor.context.names.resolve_augment(option)
        if resolved.get('status') != 'ok' and rarity:
            identity = await identify(executor, champion, rarity, option)
            match = identity.get('resolvedAugment')
            if match:
                resolved = executor.context.names.resolve_augment(str(match['id']))
        if resolved.get('status') != 'ok':
            choices.append({'requestedName': option, 'status': 'name_unresolved',
                            'identityCandidates': identity.get('augments', []) if identity else [],
                            'nextAction': {'tool': 'identify_mayhem_augment', 'champion': champion,
                                           'query': option, **({'rarity': rarity} if rarity else {})}})
            continue
        entity = resolved['augment']
        args = {'champion': champion, 'query': entity['nameEn'], 'include_descriptions': include_descriptions}
        if rarity:
            args['rarity'] = rarity
        raw = json.loads(await executor.execute('get_mayhem_augments', json.dumps(args)))
        rows = [r for r in raw.get('augments', []) if r.get('id') == entity['augmentId']]
        choice = {'requestedName': option, 'id': entity['augmentId'], 'nameEn': entity['nameEn'],
                  'namesByLocale': entity.get('namesByLocale', {}),
                  'status': 'missing_statistics', 'opgg': {'status': raw.get('status'), 'source': raw.get('source')}}
        if len(rows) == 1:
            row = rows[0]
            choice['opgg']['record'] = row
            performance, popular = row.get('performance'), row.get('popular')
            if (type(performance) in (int, float) and math.isfinite(performance)
                    and type(popular) in (int, float) and math.isfinite(popular) and popular > 0):
                choice['status'] = 'ok'
        choices.append(choice)
    complete = all(c['status'] == 'ok' for c in choices)
    unresolved = any(c['status'] == 'name_unresolved' for c in choices)
    if not complete and not unresolved:
        # Compare like with like: query ALL resolved supplied choices in fallback.
        for choice in choices:
            if choice['status'] == 'name_unresolved':
                continue
            args = {'champion': champion, 'kind': 'augment', 'query': choice['nameEn']}
            if rarity:
                args['rarity'] = rarity
            raw = json.loads(await executor.execute('get_arammeta_stats', json.dumps(args)))
            rows = [r for r in raw.get('records', []) if r.get('entity', {}).get('nameEn', '').casefold() == choice['nameEn'].casefold()]
            choice['fallback'] = {k: raw[k] for k in ('status', 'reason', 'source', 'metricDefinitions', 'coverage') if k in raw}
            choice['fallback']['records'] = rows
    return {'status': 'needs_identification' if unresolved else 'ok', 'champion': champion, 'choices': choices,
            'comparisonSource': 'opgg' if complete else ('arammeta' if all(c.get('fallback', {}).get('records') for c in choices) else 'insufficient_comparable_statistics'),
            'interpretation': 'If any choice is name_unresolved, call its nextAction identity tool and retry comparison with ALL original choices resolved. Unresolved name is NOT missing statistics. Only these player-supplied choices may be evaluated. One option means evaluate only it, not a recommendation request. Never add another augment. No statistics means unknown performance, NOT unavailable in game. Compare only the same source/metric; equal scores are a tie. Do not append an offer or mechanics lesson.'}
