# Voice gameplay assistant update

## Voice and conversation lifecycle

- Add an optional Pipecat cascade: Gemini Transcribe Live → Flash with tools →
  interchangeable Fish, Doubao, Gemini or MiniMax TTS adapters.
- Stream normal model text to Fish immediately after a short leading control-marker
  check. Keep generated-answer history independently of spoken-token aggregation.
- Prewarm and reuse voice services for 45 minutes, refreshing when idle. Keep the
  10-second input gate independent of transport lifetime.
- Replace obsolete game snapshots; preserve small historical speaker markers.
  Explicit shut-up clears history, while idle boundaries retain bounded follow-up
  memory without another model request. Input-only end commands allow replies.
- Add configurable Paraformer keyword detection, per-user gates, takeover handling,
  optional noise gating and opt-in local raw-audio diagnostics. Improve output pacing.

## Game knowledge and tools

- Add read-only League live context and an MCP server with OP.GG data and an
  ARAM Mayhem database fallback.
- Resolve multilingual champion/augment names and regional translations.
- Separate identity browsing from comparison of the player's supplied choices.
  Unknown rarity can browse all colors in one call; retain usage filtering.
- Rank augments only against the same champion's same-color candidates. Team
  comparisons use player-relative labels and do not invent match win probability.

## Observability and evaluation

- Add structured, redacted lifecycle, transcript, tool and latency events, with
  conversation-body capture and raw recording explicitly configurable.
- Add frozen interaction fixtures, replay/evaluation commands and opt-in synthetic
  API smoke tests. Private recordings, traces, cache files and operational handoffs
  are not part of the repository.
- Keep legacy Gemini/OpenAI/Grok providers and the desktop bridge available.

## Verification and limits

Local full-suite result in the optional Pipecat environment: **665 passed,
1 expected failure**. The test process disables the deployment's input noise gate
for synthetic byte fixtures. GitHub CI covers both the legacy and Pipecat
dependency environments; its results are reported separately from this local run.

The optional Pipecat environment has been exercised with real synthetic STT/LLM/TTS
calls and mocked-service framework integration. Tests cover streaming before the
model response ends, repeated turns, context boundaries, identity ordering and
late-output suppression. Synthetic checks do not establish gaming speech accuracy
or a fixed latency improvement.

The legacy Gemini provider retains an expected-failure regression for unsolicited
output after cancellation; it is not covered by the Pipecat suppression guarantee.
Game-answer quality still needs human review. Frozen older evaluation scenarios
may describe historical tool contracts; use the offered-choice scenarios for the
current split identity/comparison contract.

See [PIPECAT.md](PIPECAT.md), [OBSERVABILITY.md](OBSERVABILITY.md), and
[NAME_GROUNDING.md](NAME_GROUNDING.md) for setup and reproducible workflows.
