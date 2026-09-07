"""OpenAI GA Realtime uses bearer authentication without the beta header."""

from src.ai_services.realtime_connection import RealtimeWebSocketConnection


class OpenAIRealtimeConnection(RealtimeWebSocketConnection):
    endpoint = "wss://api.openai.com/v1/realtime"
