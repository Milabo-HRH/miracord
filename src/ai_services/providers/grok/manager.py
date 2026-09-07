"""Grok voice manager with native search and application-side League tools."""

from src.ai_services.tool_realtime_manager import ToolRealtimeManager

from .connection import GrokRealtimeConnection


class GrokRealtimeManager(ToolRealtimeManager):
    provider = "grok"
    connection_class = GrokRealtimeConnection
