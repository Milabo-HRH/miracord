"""One-shot localhost form for configuring VoiceCordAI without an IDE."""

from __future__ import annotations

import argparse
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>VoiceCordAI 本机配置</title>
  <style>
    body { font: 16px system-ui; max-width: 640px; margin: 48px auto; padding: 0 20px;
           color: #e8e8ee; background: #11131a; }
    form { display: grid; gap: 16px; padding: 24px; border: 1px solid #343746;
           border-radius: 14px; background: #1a1d27; }
    label { display: grid; gap: 7px; font-weight: 650; }
    input { font: inherit; padding: 11px 12px; color: white; background: #10121a;
            border: 1px solid #4d5266; border-radius: 8px; }
    button { font: inherit; font-weight: 700; padding: 12px; border: 0; border-radius: 8px;
             color: white; background: #5865f2; cursor: pointer; }
    .note { color: #b8bdcb; line-height: 1.55; }
    .error { color: #ff9d9d; }
  </style>
</head>
<body>
  <h1>VoiceCordAI 本机配置</h1>
  <p class="note">内容只会发送到本机 127.0.0.1，并写入被 Git 忽略的 <code>.env</code>。
  页面不加载任何外部资源，也不会回显密钥。</p>
  {message}
  <form method="post" action="/configure" autocomplete="off">
    <label>Discord Bot Token
      <input name="discord_token" type="password" required minlength="20" autocomplete="new-password">
    </label>
    <label>Gemini API Key
      <input name="gemini_key" type="password" required minlength="20" autocomplete="new-password">
    </label>
    <button type="submit">保存到本机并关闭配置服务</button>
  </form>
</body>
</html>"""


SUCCESS_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>配置完成</title>
<style>body{font:18px system-ui;max-width:620px;margin:64px auto;padding:0 20px;
color:#e8e8ee;background:#11131a}main{padding:28px;border:1px solid #343746;border-radius:14px;
background:#1a1d27}h1{color:#80e1a1}</style></head><body><main><h1>配置已保存</h1>
<p>密钥没有被显示。你可以关闭此标签页，然后回到 Codex 告诉我“配置完成”。</p>
</main></body></html>"""


def _valid_secret(value: str) -> bool:
    return len(value) >= 20 and not any(character.isspace() for character in value)


def _write_env(discord_token: str, gemini_key: str) -> None:
    values = {
        "DISCORD_TOKEN": discord_token,
        "OPENAI_API_KEY": "",
        "GEMINI_API_KEY": gemini_key,
        "XAI_API_KEY": "",
        "AI_SERVICE_PROVIDER": "gemini",
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
    ENV_PATH.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


class ConfigureHandler(BaseHTTPRequestHandler):
    server_version = "VoiceCordAIConfig/1"

    def log_message(self, _format: str, *args: object) -> None:
        return

    def _send(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/":
            self._send("Not found", 404)
            return
        self._send(PAGE.replace("{message}", ""))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/configure":
            self._send("Not found", 404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send("Invalid request", 400)
            return
        if content_length <= 0 or content_length > 16_384:
            self._send("Invalid request", 400)
            return

        form = parse_qs(self.rfile.read(content_length).decode("utf-8"))
        discord_token = form.get("discord_token", [""])[0].strip()
        gemini_key = form.get("gemini_key", [""])[0].strip()
        if not _valid_secret(discord_token) or not _valid_secret(gemini_key):
            message = '<p class="error">两项都必须填写，且不能包含空格。请重试。</p>'
            self._send(PAGE.replace("{message}", message))
            return

        _write_env(discord_token, gemini_key)
        self._send(SUCCESS_PAGE)
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ConfigureHandler)
    print(f"Local configuration page: http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()
    print("Configuration saved; local server stopped.", flush=True)


if __name__ == "__main__":
    main()
