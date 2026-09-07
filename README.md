# MIRA.CORD — Multi-user Interface for Realtime AI

[English](README.md) | [简体中文](README.zh-CN.md)

Gameplay interaction diagnostics and frozen prompt/tool evaluations are described
in [OBSERVABILITY.md](OBSERVABILITY.md). Structured local logs are enabled by
default; conversation capture requires `VOICE_DIAGNOSTIC_CAPTURE=true` and a bot
restart. [KEYWORD_RESEARCH.md](KEYWORD_RESEARCH.md) documents the Chinese sherpa
model and per-user `闭嘴` / `结束` stop gate. A stopped speaker remains
blocked until their next wake phrase. `结束` mutes only that user's input
and lets the agent continue answering; `闭嘴` also cancels that user's current
answer. Audio sent before detection cannot be retracted.

**MIRA** stands for **Multi-user Interface for Realtime AI**. MIRA.CORD is a
Discord bot that enables a shared voice conversation with OpenAI Realtime,
xAI Grok Speech-to-Speech, Google Gemini Live, or an already signed-in ChatGPT/Codex desktop Voice
session directly inside a Discord voice channel.

Current voice pipeline: **Gemini Transcribe → Flash → interchangeable TTS** via
the optional Pipecat backend, with Fish streaming text/audio, warm connections,
and bounded context at conversation boundaries. See [PIPECAT.md](PIPECAT.md)
for its separate environment and configuration. Paraformer provides configurable
Chinese wake/stop detection. [SENSEVOICE.md](SENSEVOICE.md) records earlier ASR
experiments. The legacy default remains Gemini Live unless `.env` selects
`AI_SERVICE_PROVIDER=pipecat`. Gemini, OpenAI and Grok support local League
context, read-only Mayhem tools and structured interaction traces. Gemini uses
ordered speaker context and automatic provider VAD; its Google Search is a separate opt-in
that defaults off. OpenAI/Grok remain available alternatives. Existing `.env`
settings override defaults; editing documentation does not switch a running bot.
See [the diagnostic and replay workflow](OBSERVABILITY.md).

Mayhem identity lookup returns multilingual names across all colors when rarity
is unknown. Comparison evaluates only the player's supplied choices, querying
OP.GG before the ARAM Mayhem database fallback. Performance ranks compare only
the same champion and augment color. [NAME_GROUNDING.md](NAME_GROUNDING.md)
describes regional names; [release notes](RELEASE_NOTES.md) summarize this batch.

## Features

- Voice interaction with AI using wake-word and push-to-talk.
- Optional provider-native search; Gemini Google Search defaults off independently.
- Gemini/GPT/Grok receive compact, current game context before admitted audio, with
  cached OP.GG tools for missing details. No speaker-to-champion guesswork.
- Optional `desktop_voice` backend that uses a personal ChatGPT/Codex Voice
  subscription through an isolated Windows audio bridge instead of an API key.
- One shared AI conversation per guild with per-user local wake gates.
- Configurable `barge_in`, `hold`, or `ignore` behavior for interruptions.
- The current speaker can continue without waking; a different speaker must wake to take over.
- Simple, reaction-based controls for interacting with the bot.
- Configurable voice access: automatic for everyone in the connected voice
  channel by default, or explicit reaction-based consent when desired.
- Switch between AI providers on-the-fly with a command.

## Technical Highlights

- **Concurrent Session Management**: Per-guild isolation with dedicated `GuildSession` instances prevents state leaks between Discord servers
- **Thread-Safe Audio Pipeline**: Bridges Discord's synchronous audio thread with asyncio using `run_coroutine_threadsafe` and atomic state capture to prevent race conditions
- **Provider-Agnostic AI Interface**: Runtime switching among Gemini, Grok,
  OpenAI GA Realtime, and a local desktop Voice bridge
- **Resilient Connections**: Exponential backoff reconnection (1s→30s cap) with graceful shutdown handling
- **Dual Audio Processing**: Strategy pattern with stateful streaming (audioop) and batch quality (pydub) processors
- **Containerized Deployment**: Docker support with Python runtime and FFmpeg bundled in the image

## Getting Started

Follow these steps to set up and run the bot on your local machine or a virtual machine (VM).

### 1. Prerequisites

Before you begin, ensure you have the following software installed:

- **Python 3.11 or newer** (tested on 3.13)
- **FFmpeg**:
  - **Windows**: `winget install -e --id Gyan.FFmpeg`
  - **macOS**: `brew install ffmpeg`
  - **Debian/Ubuntu**: `sudo apt-get install ffmpeg`

The `desktop_voice` provider additionally requires Windows, an installed and
signed-in ChatGPT or Codex desktop app with Voice, and
[Voicemeeter Banana](https://vb-audio.com/Voicemeeter/banana.htm). Voicemeeter
is donationware and its installer requests a Windows restart.

You will also need to gather API keys and set up your Discord bot.

#### Setting Up API Keys and Discord Bot

1.  **Discord Bot Token:**
    - Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a "New Application".
    - Go to the "Installation" tab and set the "Install Link" to "None". This ensures you will use a custom-generated invite URL with the correct permissions.
    - Navigate to the "Bot" tab, in "Token" section, click "Reset Token" to obtain a private token and keep it secured.
    - Within "Bot" tab, disable the "Public Bot" option. This is a crucial security step to prevent others from inviting your bot to their servers.
    - Message Content Intent is not required for the default slash-command setup. Enable it only if you set `ENABLE_PREFIX_COMMANDS=true` for legacy text commands.
    - **Invite the bot to your server:** 
      - Go to the "OAuth2" tab and go to the "OAuth2 URL Generator" section.
      - Select both `bot` and `applications.commands` as scopes and ensure `Guild Install` is the selected integration type.
      - In the "Bot Permissions" section, grant the following permissions:
        - `View Channels`
        - `Send Messages`
        - `Add Reactions`
        - `Connect`
        - `Speak`
      - Make sure you have sufficient permission to invite bot to the server you want to use the bot within.
      - Copy the generated URL and paste it into your browser to add the bot to your server.

2.  **Google Gemini API Key:**
    - Obtain your key from [Google AI Studio](https://aistudio.google.com/app/apikey).

3.  **xAI API Key:**
    - Obtain your key from the [xAI Console](https://console.x.ai/).

### 2. Installation and Configuration

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/Milabo-HRH/miracord.git
    cd miracord
    ```

2.  **Set Up a Virtual Environment**
    Create and activate a virtual environment in the project directory.

    **On Windows:**
    ```bash
    # Create the environment
    py -m venv .venv
    # Activate it
    .\.venv\Scripts\activate
    ```

    **On macOS/Linux:**
    ```bash
    # Create the environment
    python -m venv .venv
    # Activate it
    source .venv/bin/activate
    ```
    *You should see `(.venv)` at the beginning of your command prompt.*

3.  **Install Dependencies**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure API Keys**
    - For a no-IDE setup, run `py scripts/configure.py`. It prompts for the
      Discord token and one provider key without echoing either value, then
      writes the Git-ignored `.env` file for you.
    - Create a file named `.env` in the project's root directory.
    - Add your credentials to it like this:
      ```
      DISCORD_TOKEN=your_discord_token
      GEMINI_API_KEY=your_gemini_api_key
      XAI_API_KEY=your_xai_api_key
      AI_SERVICE_PROVIDER=gemini
      ```
    *Note: Set the key for the provider selected by `AI_SERVICE_PROVIDER`.
    `desktop_voice` uses the signed-in desktop app and does not require an
    OpenAI API key.*

### 3. Running the Bot

This project can be run locally or on a server/VM. With your virtual environment still activated, start the bot.

**On Windows:**
```bash
py main.py
```

**On macOS/Linux:**
```bash
python main.py
```

## Usage

### Bot Commands

| Command | Description | Example |
|---|---|---|
| `/connect` | Joins your voice channel and enters standby mode. | `/connect` |
| `/disconnect` | Leaves the voice channel and resets the bot. | `/disconnect` |
| `/set` | Sets the provider (`gemini`, `openai`, `grok`, or `desktop_voice`). | `/set desktop_voice` |

These are registered Discord slash commands. Global command registration can take a few minutes to appear after the bot starts.

### ChatGPT/Codex desktop Voice bridge

This is an OS-audio bridge, not a hidden subscription API:

```text
Discord -> Voicemeeter Input -> B1 -> desktop Voice microphone
desktop Voice speaker -> Voicemeeter AUX Input -> B2 -> Discord
```

One-time Windows setup:

1. Install Voicemeeter Banana and restart Windows when practical.
2. In Windows **Volume mixer**, route only ChatGPT/Codex input to
   `Voicemeeter Out B1` and its output to `Voicemeeter AUX Input`.
3. In Discord **Voice & Video**, explicitly select the real microphone and
   headphones/speakers. Do not leave Discord on a Voicemeeter B1/B2 device.
4. Start one Voice conversation in ChatGPT or Codex.
5. Use `/connect`, then `/set desktop_voice` in Discord.

Do not make B1/B2 the global Windows recording device on a computer that is
also running your personal Discord client. Doing so replaces Discord's real
microphone and can create a playback loop. On a dedicated bot computer with no
personal Discord client, using B1/AUX as the system defaults is acceptable.

The provider starts Voicemeeter Banana, routes virtual strip 3 only to B1 and
virtual strip 4 only to B2, and captures only B2. It locally detects the start
and end of the desktop model's speech before forwarding PCM to Discord. This
prevents the model's answer from being fed back into its own microphone.

The bundled Mandarin keyword-spotting files are the minimal INT8 runtime subset
of `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`. They are distributed
under Apache-2.0; see the model directory's README and license file for source
and attribution details.

The current Windows unified Voicemeeter driver is configured with:

```env
DESKTOP_VOICE_SEND_DEVICE=Voicemeeter Input
DESKTOP_VOICE_RECEIVE_DEVICE=Voicemeeter Out B2
DESKTOP_VOICE_HOST_API=MME
DESKTOP_VOICE_AUTO_ROUTE=true
```

Only one desktop Voice conversation can be bridged at a time, which matches
the V1 `guild_serial` shared-session design. The desktop app must remain open;
if Voice ends or requests user attention, start/reconnect it in the app.

### Voice Controls

After using `/connect`, the bot posts a status message. Use reactions on that message to control it. There are two ways to start a recording:

1.  **Push-to-Talk:**
    - **Add 🎙️ Reaction:** Start recording your voice.
    - **Remove 🎙️ Reaction:** Stop recording and send your audio to the AI.

2.  **Wake Word:**
    - Everyone in the connected voice channel is enabled automatically by default; no 👂 reaction is required.
    - Say the wake word ("豆包") to start recording.
    - The bot will automatically detect when you stop speaking and send your audio to the AI.
    - The current speaker can continue speaking without repeating the wake word.
    - With `CROSS_USER_WAKE_REQUIRED=true`, a different user must wake to take over, including a previously active participant.
    - The default `sherpa_onnx` detector runs locally and uses the configurable Mandarin phrase `WAKE_WORD_PHRASE=豆包`.

Set `VOICE_ACCESS_MODE=explicit` to restore the optional 👂 reaction-based consent flow.

### Shared conversation and interruption settings

Each participant's automatic speech trigger fires once per utterance. Continuous speech is tracked even while another user owns the input turn; returning to standby does not turn it into a new interruption. `ACTIVE_SPEECH_REARM_SILENCE_MS=350` rearms only that user's trigger after silence (including a Discord RTP gap). It does not change the wake-word threshold or disable same-user barge-in.

The default `guild_serial` mode keeps one provider session and one output stream per guild. `CROSS_USER_WAKE_REQUIRED=true` requires a wake phrase whenever the speaker changes; that explicit takeover interrupts the previous turn. Set it to `false` to restore automatic speech admission for all active participants. Same-speaker interruptions and newly joining participants also have configurable policies:

```env
CROSS_USER_WAKE_REQUIRED=true
ACTIVE_PARTICIPANT_SPEECH_POLICY=barge_in
NEW_PARTICIPANT_WAKE_POLICY=hold
GEMINI_GOOGLE_SEARCH_ENABLED=false
CONVERSATION_IDLE_TIMEOUT_SECONDS=10
LIVE_INPUT_SILENCE_TIMEOUT_MS=10000
NATIVE_WEB_SEARCH_MODE=auto
PREFERRED_SEARCH_SOURCES=op.gg,leagueoflegends.com,wiki.leagueoflegends.com,reddit.com/r/ARAM
VOICE_ACCESS_MODE=implicit
WAKE_WORD_ENGINE=sherpa_onnx
WAKE_WORD_PHRASE=豆包
GROK_X_SEARCH_ENABLED=false
```

`hold` records the full local utterance and submits it after the current answer; `ignore` drops it. Gemini uses automatic provider VAD with provider-default timing to decide when a spoken sentence ends. `LIVE_INPUT_SILENCE_TIMEOUT_MS=10000` is a local stuck-input safety timeout, and the ten-second conversation idle timeout handles idle admission; neither is the normal Gemini response endpoint. Floor ownership and cross-user wake requirements are separate routing rules. Gemini Google Search defaults off via `GEMINI_GOOGLE_SEARCH_ENABLED=false`. Grok retains its native search; `desktop_voice` uses the active ChatGPT/Codex Voice conversation's search capability. V1 does not require MCP.

### League Live Game MCP tools

MIRA.CORD includes an optional, read-only MCP server for live League of
Legends match context. It connects directly to Riot's loopback-only Live Client
Data API, so it must run on the same computer as the game client. It does not
need a Riot API key, the capture script, or previously recorded logs.

Start it from the repository root:

```powershell
py scripts/run_lol_mcp.py
```

To add it to an MCP client, configure a stdio server using an absolute script
path. For example, replace the path below with the location of your clone:

```json
{
  "mcpServers": {
    "miracord-league": {
      "command": "py",
      "args": ["C:\\absolute\\path\\to\\miracord\\scripts\\run_lol_mcp.py"]
    }
  }
}
```

The server exposes six tools:

| Tool | Result |
|---|---|
| `get_live_game_state` | Current mode, teams, champions, levels, scores, builds, summoner spells, Mayhem augments, and recent events |
| `get_live_game_events` | A focused list of the latest match events; accepts a `limit` from 1 to 50 |
| `get_live_game_status` | A lightweight check for an active match |
| `get_mayhem_build` | Cached OP.GG Mayhem starter items, boots, and core build alternatives |
| `get_mayhem_augments` | All-tier Mayhem augments, with ID/name filtering and pagination |
| `get_mayhem_champion_tier` | A champion's Mayhem rating and source ranking, separate from augment tiers |

Every state or event request first fetches a fresh
`/liveclientdata/allgamedata` snapshot. If the live port has closed, it reads
only the capture service's `logs/lol_live_capture/latest.json` snapshot; it
does not load the polling history. ARAM Mayhem is identified by Riot's internal
`gameMode=KIWI` plus `mapNumber=12`. Mayhem augments are parsed from augment
spell metadata, including observed base, stage 2, and stage 3 variants.

Player names, Riot IDs, account IDs, PUUIDs, and unknown event participants are
never returned. Known participants are represented by temporary slots such as
`order-01` and `chaos-01`. When neither a live match nor a captured snapshot is
available, tools return a stable `not_in_game` result instead of raising an MCP
error.

The Live Client API is available only while a match exposes port 2999, and Riot
may omit mode-specific information. In particular, only augments present in the
current API snapshot can be reported. The MCP tools provide model context, but
the `desktop_voice` audio bridge does not yet inject MCP results into the
ChatGPT/Codex Voice conversation automatically.

### Cached OP.GG Mayhem MCP adapter

The Gemini/GPT/Grok voice prompts distinguish **augments** (`海克斯` / `强化`) from
shop items. They act as a **database reader**, not a strategy coach: look up and
report the requested names, scores or effects in one short answer. Default choices
follow source order, explicitly labeled as such, with no custom strength ranking.
`sourceOrder` preserves the original list position across filtered augment queries.
Descriptions are opt-in for effect/explanation questions; routine voice and MCP
queries default to `include_descriptions=false`. Ask only for missing lookup fields.
For this low-latency mode on `gpt-realtime-2.1`, use `OPENAI_REASONING_EFFORT=minimal`.

For a **paid**, isolated prompt check, run
`python -m scripts.eval_voice_questions --label lookup`. It uses the configured
OpenAI model and actual OP.GG tools with synthetic game context and text questions;
it records the returned speech transcript and tool results under
`logs/prompt-eval-lookup.{md,json}`. It never joins Discord or uses audio
devices. Inspect the answers yourself: routing checks are not strategy, ASR,
wake-word, or live-channel acceptance tests. Keep the bot offline during review.

This is a local compatibility repair, not a change to OP.GG's hosted server.
It reads the public **ARAM Mayhem** pages: the remote augment tool was observed
to omit tiers 0–2, and its champion analysis tool did not accept Mayhem mode.
The adapter retains every record, even when metadata is missing, and never
silently substitutes normal ARAM.

Both tools accept an English champion name or OP.GG slug, such as `Samira`,
`Ezreal`, or `Kai'Sa`. For `get_mayhem_build`, pass `{"champion":"samira"}`.
For `get_mayhem_augments`, prefer just the offered IDs:

```json
{"champion": "samira", "augment_ids": [1077, 1336, 1356], "include_descriptions": true}
```

Alternatively use `query` to match a name or internal key. Results default to
12 records without descriptions. Use `offset` and `limit` (1–200), following
`nextOffset` until null, to read everything. Pagination preserves source
order, not a tier filter. `totalAvailable`, `totalMatched`, and `missingIds`
make omissions explicit. Performance is **not** a win rate; no sample counts
or claims of optimality are invented. There is no additional LLM or search
loop in this data path.

The six-hour cache lives in memory and the Git-ignored `logs/opgg_cache/`;
new MCP processes reuse it. Concurrent requests for the same page are
coalesced within a process. On upstream failure, snapshots up to 24 hours
old may be returned with `cache.stale=true`, original patch and fetch time.
Older data is not served, failures back off for 60 seconds, and unrecognized
page schemas fail explicitly.

Existing `miracord-league` registrations expose the new tools after a client
MCP restart/refresh. In Codex, restart this server in MCP settings; the
Discord bot does not need restarting. For a new Codex installation:

```powershell
codex mcp add miracord-league -- python "C:\absolute\path\to\miracord\scripts\run_lol_mcp.py"
```

The MCP client starts the stdio process itself. No exposed port, OP.GG account,
or API key is needed for these data tools. Gemini/GPT/Grok API adapters call the
same Python adapters directly, without a separate MCP subprocess or remote MCP
endpoint. Desktop Voice does not receive this automatic injection.

### Gemini Live voice with game context

The default configuration is:

```dotenv
AI_SERVICE_PROVIDER=gemini
GEMINI_MODEL=gemini-3.1-flash-live-preview
GEMINI_GOOGLE_SEARCH_ENABLED=false
CROSS_USER_WAKE_REQUIRED=true
LEAGUE_CONTEXT_ENABLED=true
LEAGUE_TOOLS_ENABLED=true
```

Configure `GEMINI_API_KEY` locally. Google Search is a separate opt-in; keeping it
off leaves the local League/OP.GG functions available. API requests consume quota
and may incur charges; eligible free-tier quota depends on the account and model.
Gemini automatic VAD is enabled with provider-default timing. Speaker/game
metadata is sent before microphone PCM. Any metadata-only model reply is
suppressed; the bot waits up to five seconds for its `turn_complete` before
uploading microphone audio, to keep an acknowledgement from leaking into the
channel. A timeout or failed context send blocks upload. This adds latency and
confirms a response boundary, not that the model understood the context correctly.
The old `GEMINI_LOCAL_VAD_SILENCE_MS=650` setting is retained only for legacy
manual-VAD snapshots, not normal Gemini sentence endings.

`结束` close only that speaker's input; `audio_stream_end` closes the
already-delivered input without cancelling the agent's answer. `闭嘴` additionally
cancels that speaker's current answer and stale tool work. Gemini does not provide
an explicit server cancellation RPC in this integration.

The same [evaluation CLI](OBSERVABILITY.md) freezes Gemini's native prompt/tool
configuration, uses fixed local tool results, checks routes and answer assertions,
and writes a complete companion JSONL trace. Offline replay does not call the
model. Explicit `--live` sends text through Gemini's supported realtime text API
with the frozen context/history prefix; it does not replay Discord audio.
Before the automatic-VAD migration, a bounded synthetic Gemini live fixture with
frozen tools passed, and an isolated manual-activity audio probe completed. Those
historical checks do not validate the new automatic-VAD path, live multi-user
Discord behavior or acoustic keyword accuracy.

### GPT / Grok API voice with game context

Choose `AI_SERVICE_PROVIDER=openai` or `grok` and configure only that provider's
API key locally. ChatGPT/Codex subscriptions are not API credentials. Defaults:
`OPENAI_REALTIME_MODEL_NAME=gpt-realtime`, `OPENAI_VOICE=marin`,
`GROK_MODEL=grok-voice-think-fast-2.0`, `GROK_REASONING_EFFORT=none` (use `high`
if preferred). Existing model overrides are respected. OpenAI uses the GA
24 kHz PCM protocol; Grok uses 16 kHz input / 24 kHz output.

For reasoning-capable OpenAI voice, set `OPENAI_REALTIME_MODEL_NAME=gpt-realtime-2.1`
and `OPENAI_REASONING_EFFORT=medium`. Supported efforts are `minimal`, `low`,
`medium`, `high`, and `xhigh`; leave the setting empty for legacy models.
This opt-in keeps the same audio transport and tools. Higher effort can add latency
and output-token cost. Published audio rates are $32/M input and $64/M output tokens,
the same as `gpt-realtime`; text output is $24/M rather than $16/M.
See the [OpenAI model card](https://developers.openai.com/api/docs/models/gpt-realtime-2.1).

- `REALTIME_SERVER_VAD=true`: stream admitted audio immediately; the provider
  detects speech end (600 ms silence configured), while the local 10-second
  safety gate remains. `hold` stays local until released, then uses manual
  commit. Set this option to `false` for local-VAD buffered turns instead.
- `LEAGUE_CONTEXT_ENABLED=true`: poll local `/allgamedata` every two seconds.
  Each admitted input turn gets one bounded, self-contained context message,
  including repeated turns from the same speaker. No HTTP call is awaited in
  the audio send path. Snapshots older than six seconds, closed game ports and
  `GameEnd` are unavailable; the previous game's final capture is never reused.
- `OPGG_PREFETCH_ENABLED=true`: warm builds and augment tables for up to ten
  champions, with two concurrent background requests and the existing six-hour
  cache. Only compact build alternatives are pushed; augment tables stay cached
  for queries. A cold cache never delays audio. Disable this to avoid proactive
  OP.GG requests.
- `LEAGUE_TOOLS_ENABLED=true`: enable `get_live_game_state`, `get_mayhem_build`,
  `get_mayhem_augments` and `get_mayhem_champion_tier` as API function tools. Calls are whitelisted, bounded
  and run off the audio loop. Parallel results are returned before one response
  continuation. Interruption/reconnection invalidates late tool results.

Ask "What tier is Ambessa in OP.GG Mayhem?" to use the champion-rating tool.
It returns `championTier`, `tierLabel` and `championRank` from a shared cached
champion table, with the source patch and freshness flags. An augment's `tier`
is a different field. Neither tool invents win rates, sample counts or player-rank
filters. Champion lookup is available through both voice API functions and MCP.

The local match is explicitly a **shared reference from the bot's computer**,
not proof of any Discord speaker's game, team or champion. Account/Riot IDs are
omitted; Discord ID and display name identify the voice turn. This data is sent
to the selected API when a participant speaks. Disable League context and
prefetch if the bot's computer should not supply match data. Turning native web
search off does not disable separately configured OP.GG functions.

Grok retains native web/X search. GPT has a `search_web` function backed by the
OpenAI Responses API's hosted web search, using the existing `OPENAI_API_KEY`.
It does not replace the Realtime voice model or require a separate MCP server.
`NATIVE_WEB_SEARCH_MODE=auto` enables it; `off` removes it independently of OP.GG.
Ask "search the web for..." or "look on Reddit for..."; latest patch/news/community
requests can also trigger it. Routine Mayhem data stays on the cached OP.GG path;
a database miss alone does not trigger broader research. Source-specific requests
can filter to Reddit, Riot, OP.GG, League Wiki or arammayhem.com. In `auto` mode,
`PREFERRED_SEARCH_SOURCES` is a preference, not a strict domain allowlist.

The search helper defaults to `OPENAI_WEB_SEARCH_MODEL=gpt-5.4-mini` with
`OPENAI_WEB_SEARCH_REASONING_EFFORT=none` (leave empty for a non-reasoning model),
one hosted search per voice turn, at most 700 output tokens, no automatic retry,
`OPENAI_WEB_SEARCH_TIMEOUT_SECONDS=15`, and a 60-second in-memory cache configured
by `OPENAI_WEB_SEARCH_CACHE_SECONDS` (0 disables caching). Search costs are separate
from Realtime usage and add latency; no search is performed on connection or idle.
Only the focused query is sent to the search helper, not the full voice session;
Responses storage is disabled (`store=false`). This is not a zero-retention guarantee.
Returned web content is untrusted evidence. Reddit is labeled as community opinion;
timeouts/auth failures are not misreported as evidence that data does not exist.
Search summaries with clickable citations are posted to the session's text channel
(Send Messages permission required), while voice reads a short answer and source name.
Search status/count/timing logs omit queries, keys and full results.

References: [OpenAI web search](https://developers.openai.com/api/docs/guides/tools-web-search),
[Realtime function tools](https://developers.openai.com/api/docs/guides/realtime-mcp).
Changes take effect after restarting the bot. The isolated, paid smoke evaluation is
`python -m scripts.eval_web_search`; it uses text input and real voice output, never
Discord, your microphone or speakers. It is not a live ASR/wake-word test.

Compatibility note: the live API rejected domain `filters` for `gpt-4.1-mini`;
the default search helper therefore uses `gpt-5.4-mini`, not the voice model.

Screenshots and explicit player-account binding are not implemented.
Each turn's context is billed as model input; Grok text-event
charges also apply. Connection readiness requires `session.updated`; standby
alone does not submit a question or Discord audio.

OpenAI interruptions truncate the conversation at PCM consumed by Discord's
playout thread, rather than at all received/queued audio. This is not a remote
listener's exact audible position. Grok cancels generation and discards late
audio; equivalent transcript truncation has not been verified for Grok.

Run offline protocol tests (loopback only; no paid API):

```powershell
python -m pytest tests/test_realtime_context.py tests/test_grok_provider.py tests/test_ai_service_coordinator.py -q
```

These tests simulate model responses; they do not validate actual recognition,
answer quality, billing entitlement or voice-channel playback. Complete a live
acceptance test after funding/configuring the chosen provider. Restart the bot
to load code/config changes; no running bot is switched automatically.

Live protocol tests require internet but no model API:

```powershell
python scripts/smoke_test_opgg_mcp.py
python scripts/smoke_test_opgg_mcp.py --champion ezreal
```

## Running with Docker

If you prefer containerized deployment for API providers:

1. **Build the image:**
   ```bash
   docker build -t miracord .
   ```

2. **Run the container:**
   ```bash
   docker run --env-file .env miracord
   ```

   Or with individual environment variables:
   ```bash
   docker run -e DISCORD_TOKEN=xxx -e GEMINI_API_KEY=xxx miracord
   ```

`desktop_voice` is intentionally not supported in Docker because it requires
the interactive Windows audio session and desktop Voice UI.

## Troubleshooting

If you encounter issues, please verify that:
- Your virtual environment is active (you see `(.venv)` in your terminal).
- All dependencies from `requirements.txt` are installed.
- The `.env` file exists and contains valid API keys.
- The bot was invited to your server with the correct permissions (see Step 1).
- You are in the project's root directory when running the bot.

## Acknowledgments

- [Google Gemini](https://ai.google.dev/)
- [xAI](https://x.ai/)
- [OpenAI Realtime](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [VB-Audio Voicemeeter](https://vb-audio.com/Voicemeeter/banana.htm)
- [discord.py](https://github.com/Rapptz/discord.py)
- [discord-ext-voice-recv](https://github.com/imayhaveborkedit/discord-ext-voice-recv)
- [openWakeWord](https://github.com/dscripka/openWakeWord)
- [webrtcvad-wheels](https://github.com/wiseman/py-webrtcvad-wheels)
- [NumPy](https://numpy.org/)

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.
