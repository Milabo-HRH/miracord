from scripts.sensevoice_fp16 import keyword_candidates


def test_transcription_tags_spacing_and_unrelated_words():
    assert keyword_candidates('<|zh|><|Speech|>豆 包，查评级。闭嘴！结束。') == ['豆包', '闭嘴', '结束']
    assert keyword_candidates('豆瓣 都把 结书 结算 over') == []
    assert keyword_candidates('<|豆包|>普通对话') == []


def test_candidates_are_text_matches_not_intent_classification():
    assert keyword_candidates('不要说闭嘴') == ['闭嘴']
