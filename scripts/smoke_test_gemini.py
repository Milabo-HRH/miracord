"""Establish and immediately close a Gemini Live session without sending media."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types


async def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
    api_key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is not configured")

    client = genai.Client(api_key=api_key)
    config = types.LiveConnectConfig(response_modalities=["AUDIO"])
    async with client.aio.live.connect(model=model, config=config):
        print("Gemini Live handshake: OK")


if __name__ == "__main__":
    asyncio.run(main())
