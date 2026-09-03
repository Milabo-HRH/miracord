"""Interactive, non-echoing setup for local MIRA.CORD secrets.

Run this from a terminal instead of pasting API keys into chat or source files.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def _secret(prompt: str) -> str:
    while True:
        value = getpass.getpass(prompt).strip()
        if value and not any(character.isspace() for character in value):
            return value
        print("Value cannot be empty or contain whitespace. Try again.")


def _choice(prompt: str, choices: set[str], default: str) -> str:
    rendered = "/".join(sorted(choices))
    while True:
        value = input(f"{prompt} [{rendered}] ({default}): ").strip().lower()
        value = value or default
        if value in choices:
            return value
        print(f"Choose one of: {rendered}.")


def main() -> None:
    print("MIRA.CORD local setup")
    print("Secrets are hidden while you type and are never printed back.")

    provider = _choice("Realtime provider", {"gemini", "grok"}, "gemini")
    discord_token = _secret("Discord bot token: ")
    provider_label = "Gemini API key" if provider == "gemini" else "xAI API key"
    provider_key = _secret(f"{provider_label}: ")

    if ENV_PATH.exists():
        overwrite = input(".env already exists. Replace it? [y/N]: ").strip().lower()
        if overwrite not in {"y", "yes"}:
            print("Cancelled; existing .env was not changed.")
            return

    values = {
        "DISCORD_TOKEN": discord_token,
        "OPENAI_API_KEY": "",
        "GEMINI_API_KEY": provider_key if provider == "gemini" else "",
        "XAI_API_KEY": provider_key if provider == "grok" else "",
        "AI_SERVICE_PROVIDER": provider,
        "GEMINI_MODEL": "gemini-3.1-flash-live-preview",
        "GROK_MODEL": "grok-voice-think-fast-2.0",
        "NATIVE_WEB_SEARCH_MODE": "auto",
        "PREFERRED_SEARCH_SOURCES": (
            "op.gg,leagueoflegends.com,wiki.leagueoflegends.com,reddit.com/r/ARAM"
        ),
        "GROK_X_SEARCH_ENABLED": "false",
        "SESSION_ROUTING_MODE": "guild_serial",
        "VOICE_ACCESS_MODE": "implicit",
        "ACTIVE_PARTICIPANT_SPEECH_POLICY": "barge_in",
        "NEW_PARTICIPANT_WAKE_POLICY": "barge_in",
        "CONVERSATION_IDLE_TIMEOUT_SECONDS": "10",
        "HELD_TURN_MAX_SECONDS": "30",
        "HELD_TURN_QUEUE_MAX": "4",
        "COMMAND_PREFIX": "/",
        "ENABLE_PREFIX_COMMANDS": "false",
        "CONNECTION_CHECK_INTERVAL": "10.0",
        "LOG_LEVEL": "INFO",
        "LOG_CONSOLE_LEVEL": "INFO",
        "REACTION_GRANT_CONSENT": "\U0001f442",
        "REACTION_TRIGGER_PTT": "\U0001f399\ufe0f",
    }
    content = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
    ENV_PATH.write_text(content, encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass

    print(f"Saved local configuration to {ENV_PATH.name}.")
    print("The file is excluded from Git. No secret values were displayed.")


if __name__ == "__main__":
    main()
