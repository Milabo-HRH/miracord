"""Grok Realtime JSON WebSocket transport."""

from src.ai_services.realtime_connection import RealtimeWebSocketConnection


class GrokRealtimeConnection(RealtimeWebSocketConnection):
    endpoint = "wss://api.x.ai/v1/realtime"
