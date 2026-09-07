"""OpenAI GA Realtime voice manager with application-side League tools."""

from src.ai_services.tool_realtime_manager import ToolRealtimeManager

from .connection import OpenAIRealtimeConnection


class OpenAIRealtimeManager(ToolRealtimeManager):
    provider = "openai"
    connection_class = OpenAIRealtimeConnection
