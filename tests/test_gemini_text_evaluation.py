import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types
from src.evaluation.gemini_text import GeminiTextEvaluation


@pytest.mark.asyncio
async def test_text_eval_preserves_function_signature_and_passes_actual_result():
    content = types.Content(role='model', parts=[types.Part(
        function_call=types.FunctionCall(name='test_tool', args={'query':'URF'}), thought_signature=b'signature')])
    final = types.Content(role='model', parts=[types.Part(text='85')])
    api = AsyncMock(side_effect=[
        SimpleNamespace(usage_metadata=None,candidates=[SimpleNamespace(content=content,finish_reason=types.FinishReason.STOP)]),
        SimpleNamespace(usage_metadata=None,candidates=[SimpleNamespace(content=final,finish_reason=types.FinishReason.STOP)])])
    manager = GeminiTextEvaluation(SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=api), aclose=AsyncMock())))
    manager.apply_replay_config({'model':'test', 'session_config':{'system_instruction':'Test'}})
    manager._execute_tool = AsyncMock(return_value=json.dumps({'score':85}))
    assert await manager.send_text_turn('Which?', 'VOICE_CONTEXT {}')
    assert manager._history[1].parts[0].thought_signature == b'signature'
    assert manager._history[2].parts[0].function_response.response == {'score':85}
    assert manager._last_response_text == '85'
    manager._execute_tool.assert_awaited_once_with('test_tool', '{"query": "URF"}')
