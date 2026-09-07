import pytest
from src.lol_mcp.team_comparison import compare_team_tiers


@pytest.mark.parametrize('ours', ['ORDER', 'CHAOS'])
def test_exact_counts_and_verdict_on_either_host_side(ours):
    other = 'CHAOS' if ours == 'ORDER' else 'ORDER'
    values = [3,4,2,3,2,3,3,5,4,3]
    state = {'available':True, 'activePlayer':{'slot':'1'}, 'players':[
        {'slot':str(i), 'champion':str(i), 'team':ours if i<5 else other} for i in range(10)]}
    result = compare_team_tiers(state, lambda c: {'status':'ok','championTier':values[int(c)]})
    assert result['verdict'] == 'your_team'
    assert result['teams'][0]['tierCounts'] == {'T2':2, 'T3':2, 'T4':1}
    assert result['teams'][1]['tierCounts'] == {'T3':3, 'T4':1, 'T5':1}
    assert result['remainingTiers'] == {'你们这边':[2,2], '对面':[3,5]}
    assert compare_team_tiers(state, lambda c: {'status':'unavailable'})['status'] == 'unavailable'


def test_no_advantage_claim_without_host_roster():
    assert compare_team_tiers({'available':True,'players':[]}, lambda c: None)['status'] == 'unavailable'
