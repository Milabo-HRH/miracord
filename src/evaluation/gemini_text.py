"""Evaluation-only text Gemini adapter for the shared frozen conversation runner."""
import asyncio
import copy
import json

from google import genai
from google.genai import types

from src.config.config import Config


class GeminiTextEvaluation:
    """Preserve complete model contents/signatures during manual tool execution."""

    def __init__(self, client=None):
        self.client = client
        self._history = []
        self._response_completed = asyncio.Event()
        self._last_response_text = ''
        self._last_response_status = ''
        self.usage = []

    def apply_replay_config(self, snapshot):
        self.snapshot = copy.deepcopy(snapshot)
        config = snapshot['session_config']
        self.config = types.GenerateContentConfig(
            system_instruction=config['system_instruction'],
            tools=config.get('tools', []),
            max_output_tokens=config.get('max_output_tokens', 2048),
            thinking_config=config.get('thinking_config'),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    async def connect(self, *_):
        if self.client is None:
            self.client = genai.Client(api_key=Config.GEMINI_API_KEY,
                                       http_options=types.HttpOptions(timeout=60000))
        return True

    async def send_text_turn(self, question, context_text=''):
        self._response_completed.clear()
        self._last_response_text, self._last_response_status = '', 'in_progress'
        self._history.append(types.Content(role='user', parts=[types.Part(text=context_text + '\n' + question)]))
        text = []
        try:
            async with asyncio.timeout(90):
                for _ in range(8):
                    result = await self.client.aio.models.generate_content(
                        model=self.snapshot['model'], contents=self._history, config=self.config)
                    if result.usage_metadata:
                        self.usage.append(result.usage_metadata.model_dump(exclude_none=True))
                    if not result.candidates or not result.candidates[0].content:
                        self._last_response_status = 'empty_response'
                        return False
                    candidate = result.candidates[0]
                    self._history.append(candidate.content)
                    responses = []
                    for part in candidate.content.parts or []:
                        if part.text and not part.thought:
                            text.append(part.text)
                        if part.function_call:
                            call = part.function_call
                            response = json.loads(await self._execute_tool(call.name, json.dumps(call.args or {}, ensure_ascii=False)))
                            responses.append(types.Part(function_response=types.FunctionResponse(
                                name=call.name, id=call.id, response=response)))
                    if not responses:
                        self._last_response_text = '\n'.join(text)
                        self._last_response_status = 'completed' if candidate.finish_reason == types.FinishReason.STOP else str(candidate.finish_reason)
                        return self._last_response_status == 'completed'
                    self._history.append(types.Content(role='user', parts=responses))
                self._last_response_status = 'tool_round_limit'
                return False
        except Exception as exc:
            self._last_response_status = f'error:{type(exc).__name__}:{getattr(exc, "code", "")}'
            return False
        finally:
            self._response_completed.set()

    async def disconnect(self):
        if self.client:
            await self.client.aio.aclose()
