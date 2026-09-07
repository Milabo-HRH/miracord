"""
Application configuration module.

This module defines the Config class, which centralizes all
application-wide settings, such as API keys, server URLs,
audio processing parameters, and logging configurations.
It loads values from environment variables and provides
validation for required settings.
"""

import logging
import os
from pathlib import Path
from typing import Optional, Union

from dotenv import load_dotenv

from src.exceptions import ConfigurationError

load_dotenv()


class Config:
    """
    Centralized configuration settings for the application.

    This class holds all static configuration parameters, loaded primarily
    from environment variables. It includes settings for API keys,
    server connections, audio processing, bot behavior, and logging.
    The `validate` method ensures that critical configurations are present.
    """

    # Supported AI service providers
    SUPPORTED_AI_PROVIDERS: frozenset[str] = frozenset(
        {"gemini", "grok", "openai", "desktop_voice"}
    )
    SUPPORTED_SPEECH_POLICIES: frozenset[str] = frozenset(
        {"barge_in", "hold", "ignore"}
    )
    SUPPORTED_SEARCH_MODES: frozenset[str] = frozenset({"auto", "off"})
    SUPPORTED_VOICE_ACCESS_MODES: frozenset[str] = frozenset({"implicit", "explicit"})
    SUPPORTED_WAKE_WORD_ENGINES: frozenset[str] = frozenset(
        {"openwakeword", "sherpa_onnx", "paraformer"}
    )

    BASE_DIR: Path = (
        Path(__file__).resolve().parent.parent.parent
    )  # Base directory of the project

    # --- Core Bot Settings ---
    DISCORD_TOKEN: Optional[str] = os.getenv("DISCORD_TOKEN")
    _DISCORD_SYNC_GUILD_ID_RAW: str = os.getenv("DISCORD_SYNC_GUILD_ID", "").strip()
    DISCORD_SYNC_GUILD_ID: Optional[int] = (
        int(_DISCORD_SYNC_GUILD_ID_RAW) if _DISCORD_SYNC_GUILD_ID_RAW else None
    )
    AUTO_CONNECT_ENABLED: bool = os.getenv(
        "AUTO_CONNECT_ENABLED", "false"
    ).lower() in {"1", "true", "yes", "on"}
    _AUTO_CONNECT_GUILD_ID_RAW: str = os.getenv(
        "AUTO_CONNECT_GUILD_ID", ""
    ).strip()
    AUTO_CONNECT_GUILD_ID: Optional[int] = (
        int(_AUTO_CONNECT_GUILD_ID_RAW)
        if _AUTO_CONNECT_GUILD_ID_RAW
        else DISCORD_SYNC_GUILD_ID
    )
    _AUTO_CONNECT_VOICE_CHANNEL_ID_RAW: str = os.getenv(
        "AUTO_CONNECT_VOICE_CHANNEL_ID", ""
    ).strip()
    AUTO_CONNECT_VOICE_CHANNEL_ID: Optional[int] = (
        int(_AUTO_CONNECT_VOICE_CHANNEL_ID_RAW)
        if _AUTO_CONNECT_VOICE_CHANNEL_ID_RAW
        else None
    )
    _AUTO_CONNECT_TEXT_CHANNEL_ID_RAW: str = os.getenv(
        "AUTO_CONNECT_TEXT_CHANNEL_ID", ""
    ).strip()
    AUTO_CONNECT_TEXT_CHANNEL_ID: Optional[int] = (
        int(_AUTO_CONNECT_TEXT_CHANNEL_ID_RAW)
        if _AUTO_CONNECT_TEXT_CHANNEL_ID_RAW
        else None
    )
    COMMAND_PREFIX: str = os.getenv("COMMAND_PREFIX", "/")  # Prefix for bot commands
    ENABLE_PREFIX_COMMANDS: bool = os.getenv(
        "ENABLE_PREFIX_COMMANDS", "false"
    ).lower() in {"1", "true", "yes", "on"}
    # GPT and Grok are first-class. Preserve existing deployments' provider choice.
    AI_SERVICE_PROVIDER: str = os.getenv(
        "AI_SERVICE_PROVIDER", os.getenv("AI_PROVIDER", "gemini")
    ).lower()

    # --- Voice & Connection Settings ---
    CONNECTION_TIMEOUT: int = int(os.getenv("CONNECTION_TIMEOUT_SECONDS", "900"))
    CONNECTION_CHECK_INTERVAL: float = float(
        os.getenv("CONNECTION_CHECK_INTERVAL", "10.0")
    )
    AI_SERVICE_CONNECTION_TIMEOUT: float = 30.0  # Timeout for AI service connections
    CHUNK_DURATION_MS: int = 500  # Duration of audio chunks in milliseconds

    # --- Shared Conversation Settings ---
    SESSION_ROUTING_MODE: str = os.getenv(
        "SESSION_ROUTING_MODE", "guild_serial"
    ).lower()
    ACTIVE_PARTICIPANT_SPEECH_POLICY: str = os.getenv(
        "ACTIVE_PARTICIPANT_SPEECH_POLICY", "barge_in"
    ).lower()
    NEW_PARTICIPANT_WAKE_POLICY: str = os.getenv(
        "NEW_PARTICIPANT_WAKE_POLICY", "barge_in"
    ).lower()
    CROSS_USER_WAKE_REQUIRED: bool = os.getenv(
        "CROSS_USER_WAKE_REQUIRED", "true"
    ).lower() in {"1", "true", "yes", "on"}
    CONVERSATION_IDLE_TIMEOUT_SECONDS: float = float(
        os.getenv("CONVERSATION_IDLE_TIMEOUT_SECONDS", "10")
    )
    LIVE_INPUT_SILENCE_TIMEOUT_MS: int = int(
        os.getenv("LIVE_INPUT_SILENCE_TIMEOUT_MS", "10000")
    )
    VOICE_INPUT_GATE_ENABLED: bool = os.getenv(
        "VOICE_INPUT_GATE_ENABLED", "false"
    ).lower() in {"1", "true", "yes", "on"}
    VOICE_INPUT_GATE_DBFS: float = float(os.getenv("VOICE_INPUT_GATE_DBFS", "-42"))
    HELD_TURN_MAX_SECONDS: float = float(os.getenv("HELD_TURN_MAX_SECONDS", "30"))
    HELD_TURN_QUEUE_MAX: int = int(os.getenv("HELD_TURN_QUEUE_MAX", "4"))
    VOICE_ACCESS_MODE: str = os.getenv("VOICE_ACCESS_MODE", "implicit").lower()
    ACTIVE_SPEECH_VAD_AGGRESSIVENESS: int = int(
        os.getenv("ACTIVE_SPEECH_VAD_AGGRESSIVENESS", "2")
    )
    ACTIVE_SPEECH_MIN_DURATION_MS: int = int(
        os.getenv("ACTIVE_SPEECH_MIN_DURATION_MS", "180")
    )
    ACTIVE_SPEECH_MAX_GAP_MS: int = int(
        os.getenv("ACTIVE_SPEECH_MAX_GAP_MS", "60")
    )
    ACTIVE_SPEECH_COOLDOWN_MS: int = int(
        os.getenv("ACTIVE_SPEECH_COOLDOWN_MS", "750")
    )
    ACTIVE_SPEECH_REARM_SILENCE_MS: int = int(
        os.getenv("ACTIVE_SPEECH_REARM_SILENCE_MS", "350")
    )

    # League adapters are shared by MCP and the first-class voice providers.
    LEAGUE_CONTEXT_ENABLED: bool = os.getenv("LEAGUE_CONTEXT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    LEAGUE_TOOLS_ENABLED: bool = os.getenv("LEAGUE_TOOLS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    OPGG_PREFETCH_ENABLED: bool = os.getenv("OPGG_PREFETCH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    REALTIME_SERVER_VAD: bool = os.getenv("REALTIME_SERVER_VAD", "true").lower() in {"1", "true", "yes", "on"}
    GEMINI_LOCAL_VAD_SILENCE_MS: int = int(os.getenv("GEMINI_LOCAL_VAD_SILENCE_MS", "650"))

    # Web search (native for Grok/Gemini, Responses-backed for GPT) is separate from OP.GG.
    NATIVE_WEB_SEARCH_MODE: str = os.getenv("NATIVE_WEB_SEARCH_MODE", "auto").lower()
    PREFERRED_SEARCH_SOURCES: tuple[str, ...] = tuple(
        source.strip()
        for source in os.getenv(
            "PREFERRED_SEARCH_SOURCES",
            ("op.gg,leagueoflegends.com,wiki.leagueoflegends.com," "reddit.com/r/ARAM"),
        ).split(",")
        if source.strip()
    )
    ASSISTANT_SYSTEM_INSTRUCTIONS: str = os.getenv(
        "ASSISTANT_SYSTEM_INSTRUCTIONS", ""
    ).strip() or (
        "You are a fast game-data lookup voice interface for a shared Discord channel. "
        "Answer briefly in the speaker's language without a spoken preamble. "
        "When asked to choose, use verified effects and known match facts for a concise, "
        "conditional recommendation; distinguish evidence from advice. "
        "For League of Legends, ARAM, ARAM Mayhem, patches, "
        "champions, augments (海克斯/强化), items, builds, mechanics, or any fact that may have changed, "
        "prefer fresh supplied context and cached tools. Report missing data briefly. "
        "Use broader web search when requested, including latest news, patch changes "
        "and community discussion. Prefer these "
        "sources in order when relevant: "
        + ", ".join(PREFERRED_SEARCH_SOURCES)
        + ". Prefer official patch notes for rules and patch behavior, current data "
        "sites for statistics, and label Reddit claims as community discussion rather "
        "than verified fact. If reliable sources conflict, say so briefly. Never claim "
        "to have searched when no search was performed."
    )
    GROK_X_SEARCH_ENABLED: bool = os.getenv(
        "GROK_X_SEARCH_ENABLED", "false"
    ).lower() in {"1", "true", "yes", "on"}

    # Desktop Voice bridge.  The bot writes Discord audio to the first
    DESKTOP_VOICE_ALWAYS_FORWARD: bool = os.getenv(
        "DESKTOP_VOICE_ALWAYS_FORWARD", "false"
    ).lower() in {"1", "true", "yes", "on"}

    # Voicemeeter virtual input and captures ChatGPT/Codex Voice from AUX.
    DESKTOP_VOICE_SEND_DEVICE: str = os.getenv(
        "DESKTOP_VOICE_SEND_DEVICE", "Voicemeeter Input"
    ).strip()
    DESKTOP_VOICE_RECEIVE_DEVICE: str = os.getenv(
        "DESKTOP_VOICE_RECEIVE_DEVICE", "Voicemeeter Out B2"
    ).strip()
    DESKTOP_VOICE_HOST_API: str = os.getenv("DESKTOP_VOICE_HOST_API", "MME").strip()
    DESKTOP_VOICE_SAMPLE_RATE: int = int(
        os.getenv("DESKTOP_VOICE_SAMPLE_RATE", "48000")
    )
    DESKTOP_VOICE_CHANNELS: int = int(os.getenv("DESKTOP_VOICE_CHANNELS", "2"))
    DESKTOP_VOICE_FRAME_MS: int = int(os.getenv("DESKTOP_VOICE_FRAME_MS", "20"))
    DESKTOP_VOICE_RESPONSE_START_MS: int = int(
        os.getenv("DESKTOP_VOICE_RESPONSE_START_MS", "60")
    )
    DESKTOP_VOICE_RESPONSE_SILENCE_MS: int = int(
        os.getenv("DESKTOP_VOICE_RESPONSE_SILENCE_MS", "900")
    )
    DESKTOP_VOICE_RESPONSE_PREROLL_MS: int = int(
        os.getenv("DESKTOP_VOICE_RESPONSE_PREROLL_MS", "200")
    )
    DESKTOP_VOICE_VAD_AGGRESSIVENESS: int = int(
        os.getenv("DESKTOP_VOICE_VAD_AGGRESSIVENESS", "1")
    )
    DESKTOP_VOICE_AUTO_ROUTE: bool = os.getenv(
        "DESKTOP_VOICE_AUTO_ROUTE", "true"
    ).lower() in {"1", "true", "yes", "on"}
    VOICEMEETER_REMOTE_DLL: Path = Path(
        os.getenv(
            "VOICEMEETER_REMOTE_DLL",
            r"C:\Program Files (x86)\VB\Voicemeeter\VoicemeeterRemote64.dll",
        )
    )

    # --- UI/UX Settings ---
    REACTION_GRANT_CONSENT: str = os.getenv("REACTION_GRANT_CONSENT", "👂")
    REACTION_TRIGGER_PTT: str = os.getenv("REACTION_TRIGGER_PTT", "🎙️")

    # --- Audio Cue Paths ---
    AUDIO_CUE_START_RECORDING: Path = BASE_DIR / "assets/audio_cues/start_recording.mp3"
    AUDIO_CUE_END_RECORDING: Path = BASE_DIR / "assets/audio_cues/end_recording.mp3"

    # --- Audio Processing Settings ---
    # General Audio
    SAMPLE_WIDTH: int = 2  # Sample width in bytes (e.g., 2 for 16-bit audio)
    FFMPEG_PCM_FORMAT: str = (
        "s16le"  # FFmpeg format string for PCM data (signed 16-bit little-endian)
    )

    # Discord Audio Format (for audio received from and sent to Discord)
    DISCORD_AUDIO_FRAME_RATE: int = 48000  # Samples per second, per channel
    DISCORD_AUDIO_CHANNELS: int = 2  # Number of audio channels (e.g., 2 for stereo)

    # Audio chunk sizes for different processing stages
    DISCORD_CHUNK_SIZE: int = 3840  # 20ms of 48kHz stereo (48000 * 2 * 2 / 50)
    WAKE_WORD_CHUNK_SIZE: int = 1280  # 80ms of 16kHz mono (16000 * 2 / 25)
    VAD_PROCESSING_CHUNK: int = 7680  # Minimum chunk size for resample operations

    # Buffer size limits for audio processing
    MAX_AUTHORITY_BUFFER_SIZE: int = 1024 * 1024  # 1MB limit for authority buffers
    MAX_STANDBY_BUFFER_SIZE: int = 512 * 1024  # 512KB limit for standby buffers

    # Audio playback and processing constants
    AUDIO_PLAYBACK_QUEUE_SIZE: int = 1000  # Max audio chunks in playback queue
    AUDIO_LOG_SAMPLING_RATE: int = 100  # Log ~1% of frames (1 in 100)
    FFMPEG_PROCESS_CLEANUP_TIMEOUT: float = 2.0  # Seconds to wait for FFmpeg cleanup

    # --- Wake Word & VAD Settings ---
    WAKE_WORD_ENGINE: str = os.getenv("WAKE_WORD_ENGINE", "sherpa_onnx").lower()
    WAKE_WORD_PHRASE: str = os.getenv("WAKE_WORD_PHRASE", "豆包").strip()
    # openWakeWord settings
    WAKE_WORD_MODEL_PATH: Path = BASE_DIR / "assets/wakeword_models/alexa_v0.1.onnx"
    WAKE_WORD_THRESHOLD: float = 0.5  # Confidence threshold for detection
    # VAD inside openWakeWord to improve ww accuracy.
    WAKE_WORD_VAD_THRESHOLD: float = 0.5
    WAKE_WORD_SAMPLE_RATE: int = 16000  # Sample rate for wake word model (Hz)
    SHERPA_WAKE_WORD_MODEL_DIR: Path = Path(os.getenv(
        "SHERPA_WAKE_WORD_MODEL_DIR", str(BASE_DIR / (
            "assets/wakeword_models/"
            "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
        ))
    ))
    SHERPA_WAKE_WORD_KEYWORDS_PATH: Path = Path(os.getenv(
        "SHERPA_WAKE_WORD_KEYWORDS_PATH",
        str(SHERPA_WAKE_WORD_MODEL_DIR / "keywords_control.txt"),
    ))
    STOP_WORD_ENABLED: bool = os.getenv("STOP_WORD_ENABLED", "true").lower() in {
        "1", "true", "yes", "on"
    }
    STOP_WORD_PHRASES: tuple[str, ...] = tuple(
        phrase.strip().casefold()
        for phrase in os.getenv("STOP_WORD_PHRASES", "闭嘴,结束").split(",")
        if phrase.strip()
    )
    SHERPA_WAKE_WORD_SCORE: float = float(os.getenv("SHERPA_WAKE_WORD_SCORE", "3.0"))
    SHERPA_WAKE_WORD_THRESHOLD: float = float(
        os.getenv("SHERPA_WAKE_WORD_THRESHOLD", "0.1")
    )

    # webrtcvad settings for end-of-speech detection
    VAD_SAMPLE_RATE: int = 16000  # Sample rate for VAD processing (Hz)
    VAD_FRAME_DURATION_MS: int = 30  # Frame duration for VAD (10, 20, or 30)
    VAD_AGGRESSIVENESS: int = 1  # VAD aggressiveness (0-3), 1 is a good balance
    VAD_GRACE_PERIOD_MS: int = (
        3000  # VAD will be ignored for this long after recording starts
    )
    VAD_MIN_SPEECH_DURATION_MS: int = 250  # Min speech to trigger recording stop
    VAD_SILENCE_TIMEOUT_MS: int = 1000  # Silence after speech to stop recording

    # --- Logging Configuration ---
    LOG_LEVEL: Union[int, str] = os.getenv(
        "LOG_LEVEL", "INFO"
    )  # General log level for file logs
    LOG_CONSOLE_LEVEL: Union[int, str] = os.getenv(
        "LOG_CONSOLE_LEVEL", "INFO"
    )  # Log level for console output
    LOG_MAX_SIZE: int = (
        5 * 1024 * 1024
    )  # Max size of a log file before rotation (in bytes)
    LOG_BACKUP_COUNT: int = 3  # Number of backup log files to keep

    # --- API Keys ---
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
    GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
    XAI_API_KEY: Optional[str] = os.getenv("XAI_API_KEY")

    @classmethod
    def validate(cls) -> None:
        """Validate required configuration variables.

        This method checks that all critical configuration variables are set
        and have valid values. It is called automatically when the module
        is imported.

        Returns:
            None.

        Raises:
            ConfigurationError: If a required configuration is missing or invalid.
        """
        if not cls.DISCORD_TOKEN:
            raise ConfigurationError(
                "DISCORD_TOKEN environment variable not set or empty."
            )
        if cls.DISCORD_SYNC_GUILD_ID is not None and cls.DISCORD_SYNC_GUILD_ID <= 0:
            raise ConfigurationError(
                "DISCORD_SYNC_GUILD_ID must be a positive integer."
            )

        # Validate that AI_SERVICE_PROVIDER has a recognized value
        if cls.AI_SERVICE_PROVIDER not in cls.SUPPORTED_AI_PROVIDERS:
            raise ConfigurationError(
                f"Unsupported AI_SERVICE_PROVIDER: '{cls.AI_SERVICE_PROVIDER}'. "
                f"Must be one of: {', '.join(sorted(cls.SUPPORTED_AI_PROVIDERS))}"
            )

        if cls.SESSION_ROUTING_MODE != "guild_serial":
            raise ConfigurationError(
                "V1 supports only SESSION_ROUTING_MODE='guild_serial'."
            )

        for setting_name, policy in (
            (
                "ACTIVE_PARTICIPANT_SPEECH_POLICY",
                cls.ACTIVE_PARTICIPANT_SPEECH_POLICY,
            ),
            ("NEW_PARTICIPANT_WAKE_POLICY", cls.NEW_PARTICIPANT_WAKE_POLICY),
        ):
            if policy not in cls.SUPPORTED_SPEECH_POLICIES:
                raise ConfigurationError(
                    f"Unsupported {setting_name}: '{policy}'. Must be one of: "
                    f"{', '.join(sorted(cls.SUPPORTED_SPEECH_POLICIES))}"
                )

        if cls.NATIVE_WEB_SEARCH_MODE not in cls.SUPPORTED_SEARCH_MODES:
            raise ConfigurationError(
                f"Unsupported NATIVE_WEB_SEARCH_MODE: "
                f"'{cls.NATIVE_WEB_SEARCH_MODE}'. Must be one of: "
                f"{', '.join(sorted(cls.SUPPORTED_SEARCH_MODES))}"
            )
        if cls.VOICE_ACCESS_MODE not in cls.SUPPORTED_VOICE_ACCESS_MODES:
            raise ConfigurationError(
                f"Unsupported VOICE_ACCESS_MODE: '{cls.VOICE_ACCESS_MODE}'. "
                "Must be one of: "
                f"{', '.join(sorted(cls.SUPPORTED_VOICE_ACCESS_MODES))}"
            )
        if cls.WAKE_WORD_ENGINE not in cls.SUPPORTED_WAKE_WORD_ENGINES:
            raise ConfigurationError(
                f"Unsupported WAKE_WORD_ENGINE: '{cls.WAKE_WORD_ENGINE}'. "
                "Must be one of: "
                f"{', '.join(sorted(cls.SUPPORTED_WAKE_WORD_ENGINES))}"
            )
        if not cls.WAKE_WORD_PHRASE:
            raise ConfigurationError("WAKE_WORD_PHRASE must not be empty.")
        if not 200 <= cls.GEMINI_LOCAL_VAD_SILENCE_MS <= 3000:
            raise ConfigurationError("GEMINI_LOCAL_VAD_SILENCE_MS must be between 200 and 3000.")
        if cls.CONVERSATION_IDLE_TIMEOUT_SECONDS <= 0:
            raise ConfigurationError(
                "CONVERSATION_IDLE_TIMEOUT_SECONDS must be greater than zero."
            )
        if cls.LIVE_INPUT_SILENCE_TIMEOUT_MS <= 0:
            raise ConfigurationError(
                "LIVE_INPUT_SILENCE_TIMEOUT_MS must be greater than zero."
            )
        if not -80 <= cls.VOICE_INPUT_GATE_DBFS <= -6:
            raise ConfigurationError("VOICE_INPUT_GATE_DBFS must be between -80 and -6.")
        if cls.HELD_TURN_MAX_SECONDS <= 0 or cls.HELD_TURN_QUEUE_MAX <= 0:
            raise ConfigurationError(
                "HELD_TURN_MAX_SECONDS and HELD_TURN_QUEUE_MAX must be greater than zero."
            )
        if cls.DESKTOP_VOICE_SAMPLE_RATE not in (8000, 16000, 32000, 48000):
            raise ConfigurationError(
                "DESKTOP_VOICE_SAMPLE_RATE must be 8000, 16000, 32000, or 48000."
            )
        if cls.DESKTOP_VOICE_CHANNELS not in (1, 2):
            raise ConfigurationError("DESKTOP_VOICE_CHANNELS must be 1 or 2.")
        if cls.DESKTOP_VOICE_FRAME_MS not in (10, 20, 30):
            raise ConfigurationError("DESKTOP_VOICE_FRAME_MS must be 10, 20, or 30.")
        if cls.DESKTOP_VOICE_VAD_AGGRESSIVENESS not in (0, 1, 2, 3):
            raise ConfigurationError(
                "DESKTOP_VOICE_VAD_AGGRESSIVENESS must be an integer from 0 to 3."
            )
        if cls.ACTIVE_SPEECH_VAD_AGGRESSIVENESS not in (0, 1, 2, 3):
            raise ConfigurationError(
                "ACTIVE_SPEECH_VAD_AGGRESSIVENESS must be an integer from 0 to 3."
            )
        if cls.ACTIVE_SPEECH_MIN_DURATION_MS <= 0:
            raise ConfigurationError(
                "ACTIVE_SPEECH_MIN_DURATION_MS must be greater than zero."
            )
        if cls.ACTIVE_SPEECH_MAX_GAP_MS < 0:
            raise ConfigurationError(
                "ACTIVE_SPEECH_MAX_GAP_MS cannot be negative."
            )
        if cls.ACTIVE_SPEECH_COOLDOWN_MS < 0:
            raise ConfigurationError(
                "ACTIVE_SPEECH_COOLDOWN_MS cannot be negative."
            )
        if cls.ACTIVE_SPEECH_REARM_SILENCE_MS <= cls.ACTIVE_SPEECH_MAX_GAP_MS:
            raise ConfigurationError(
                "ACTIVE_SPEECH_REARM_SILENCE_MS must exceed ACTIVE_SPEECH_MAX_GAP_MS."
            )
        if cls.AUTO_CONNECT_ENABLED and cls.AUTO_CONNECT_GUILD_ID is None:
            raise ConfigurationError(
                "AUTO_CONNECT_GUILD_ID or DISCORD_SYNC_GUILD_ID is required when "
                "AUTO_CONNECT_ENABLED is true."
            )
        if cls.AUTO_CONNECT_ENABLED and cls.AUTO_CONNECT_VOICE_CHANNEL_ID is None:
            raise ConfigurationError(
                "AUTO_CONNECT_VOICE_CHANNEL_ID is required when "
                "AUTO_CONNECT_ENABLED is true."
            )
        if (
            min(
                cls.DESKTOP_VOICE_RESPONSE_START_MS,
                cls.DESKTOP_VOICE_RESPONSE_SILENCE_MS,
                cls.DESKTOP_VOICE_RESPONSE_PREROLL_MS,
            )
            <= 0
        ):
            raise ConfigurationError(
                "Desktop Voice response timing values must be greater than zero."
            )

        # Validate logging configuration
        if not isinstance(cls.LOG_MAX_SIZE, int):
            raise ConfigurationError(
                f"LOG_MAX_SIZE must be an int, but got {type(cls.LOG_MAX_SIZE).__name__}."
            )
        if not isinstance(cls.LOG_BACKUP_COUNT, int):
            raise ConfigurationError(
                f"LOG_BACKUP_COUNT must be an int, but got {type(cls.LOG_BACKUP_COUNT).__name__}."
            )

        if isinstance(cls.LOG_LEVEL, str):
            # Check if the string is a valid log level name
            if not isinstance(logging.getLevelName(cls.LOG_LEVEL.upper()), int):
                raise ConfigurationError(
                    f"Invalid log level string from Config: '{cls.LOG_LEVEL}'"
                )
        elif not isinstance(cls.LOG_LEVEL, int):
            raise ConfigurationError(
                f"LOG_LEVEL must be a str or int, but got {type(cls.LOG_LEVEL).__name__}."
            )

        # Validate VAD & Wake Word settings
        if cls.VAD_AGGRESSIVENESS not in (0, 1, 2, 3):
            raise ConfigurationError(
                "VAD_AGGRESSIVENESS must be an integer from 0 to 3."
            )
        if cls.VAD_FRAME_DURATION_MS not in (10, 20, 30):
            raise ConfigurationError("VAD_FRAME_DURATION_MS must be 10, 20, or 30.")
        if not (0.0 <= cls.WAKE_WORD_THRESHOLD <= 1.0):
            raise ConfigurationError("WAKE_WORD_THRESHOLD must be between 0.0 and 1.0.")
        if not (0.0 <= cls.WAKE_WORD_VAD_THRESHOLD <= 1.0):
            raise ConfigurationError(
                "WAKE_WORD_VAD_THRESHOLD must be between 0.0 and 1.0."
            )


Config.validate()  # Validate configuration on import
