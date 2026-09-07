"""Use Pipecat service contracts, rather than another universal model API."""
import json
from pathlib import Path


def fish_api_key(config):
    key = config.get("fish_key", "").strip()
    if not key and config.get("fish_key_file"):
        key = Path(config["fish_key_file"]).read_text(encoding="utf-8-sig").strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("Fish requires FISH_API_KEY or a single-key FISH_API_KEY_FILE")
    return key


def vocabulary(context_text, limit=100):
    """Bias only roster/observed names, never arbitrary context prose or IDs."""
    try:
        glossary = json.loads(context_text.split("\n", 1)[1]).get("nameGlossary", {})
    except (ValueError, IndexError, AttributeError):
        return []
    words = []
    def add(value):
        if isinstance(value, str) and 1 <= len(value.strip()) <= 80 and value not in words:
            words.append(value)
    for record in glossary.get("champions", []) + glossary.get("augments", []):
        for key in ("name", "nameZh", "nameEn", "titleZh", "nameEnUS"):
            add(record.get(key))
        for key in ("namesByLocale", "names"):
            for name in (record.get(key) or {}).values():
                add(name)
        for alias in record.get("aliasesZh", []):
            add(alias)
    return words[:limit]


def google_llm(config, instructions):
    from pipecat.services.google.llm import GoogleLLMService
    return GoogleLLMService(
        api_key=config["api_key"],
        settings=GoogleLLMService.Settings(
            model=config["llm_model"], max_tokens=2048, system_instruction=instructions,
            thinking=GoogleLLMService.ThinkingConfig(thinking_level=config["thinking"])))


# New backends implement Pipecat's LLMService/TTSService and register a factory.
LLM_FACTORIES = {"google": google_llm}


def make_llm(config, instructions):
    try:
        factory = LLM_FACTORIES[config["llm_provider"]]
    except KeyError:
        raise ValueError("Unknown PIPE_LLM_PROVIDER; register a Pipecat LLM service") from None
    return factory(config, instructions)


def make_tts(config, http_session):
    provider, model, voice = config["tts_provider"], config["tts_model"], config["tts_voice"]
    if provider == "doubao":
        from .doubao import DoubaoTTSService
        return DoubaoTTSService(config, http_session)
    if provider == "gemini":
        from pipecat.services.google.tts import GeminiTTSService
        return GeminiTTSService(api_key=config["api_key"], use_genai=True,
            settings=GeminiTTSService.Settings(
                model=model or "gemini-3.1-flash-tts-preview", voice=voice or "Zephyr"))
    if provider == "fish":
        key = fish_api_key(config)
        voice = voice or config["fish_voice"]
        from pipecat.services.fish.tts import FishAudioTTSService
        from pipecat.services.tts_service import TextAggregationMode
        return FishAudioTTSService(api_key=key, sample_rate=24000,
            text_aggregation_mode=TextAggregationMode.TOKEN,
            settings=FishAudioTTSService.Settings(
                model=model or "s2.1-pro-free", voice=voice, latency="balanced"))
    if provider == "minimax":
        if not config["minimax_key"] or not config["minimax_group"] or not voice:
            raise ValueError("MiniMax requires MINIMAX_API_KEY, MINIMAX_GROUP_ID and PIPE_TTS_VOICE")
        from pipecat.services.minimax.tts import MiniMaxHttpTTSService
        return MiniMaxHttpTTSService(api_key=config["minimax_key"],
            group_id=config["minimax_group"], base_url=config["minimax_url"],
            aiohttp_session=http_session, sample_rate=24000,
            settings=MiniMaxHttpTTSService.Settings(
                model=model or "speech-2.8-turbo", voice=voice, language_boost="Chinese"))
    raise ValueError("PIPE_TTS_PROVIDER must be doubao, gemini, fish or minimax")
