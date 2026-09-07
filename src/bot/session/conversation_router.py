"""Pure guild-scoped routing decisions for a shared realtime conversation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import time
from typing import Deque, Optional


class SpeechPolicy(str, Enum):
    """How participant speech is handled while the agent is speaking."""

    BARGE_IN = "barge_in"
    HOLD = "hold"
    IGNORE = "ignore"


class TurnDisposition(str, Enum):
    """The action selected for one detected user utterance."""

    SEND = "send"
    BARGE_IN = "barge_in"
    HOLD = "hold"
    IGNORE = "ignore"


@dataclass(frozen=True)
class HeldTurn:
    """A bounded local utterance waiting for the current response to finish."""

    user_id: int
    display_name: str
    audio_data: bytes
    speech_started_at: float


class GuildConversationRouter:
    """Routes users into one shared, serial conversation per Discord guild."""

    def __init__(
        self,
        *,
        active_participant_policy: str,
        new_participant_policy: str,
        idle_timeout_seconds: float,
        held_turn_max_seconds: float,
        held_turn_queue_max: int,
        source_bytes_per_second: int,
        cross_user_wake_required: bool = True,
    ) -> None:
        self.active_participant_policy = SpeechPolicy(active_participant_policy)
        self.new_participant_policy = SpeechPolicy(new_participant_policy)
        self.idle_timeout_seconds = idle_timeout_seconds
        self.held_turn_max_bytes = int(held_turn_max_seconds * source_bytes_per_second)
        self.held_turn_queue_max = held_turn_queue_max
        self._active_participants: set[int] = set()
        self.cross_user_wake_required = cross_user_wake_required
        self._floor_owner_id: Optional[int] = None
        self._held_turns: Deque[HeldTurn] = deque()
        self._last_activity_at = time.monotonic()

    @property
    def active_participants(self) -> set[int]:
        return self._active_participants.copy()

    @property
    def floor_owner_id(self) -> Optional[int]:
        """The only participant allowed to continue without a new wake phrase."""
        return self._floor_owner_id

    @property
    def has_held_turns(self) -> bool:
        return bool(self._held_turns)

    @property
    def last_activity_at(self) -> float:
        return self._last_activity_at

    def touch(self, now: Optional[float] = None) -> None:
        self._last_activity_at = time.monotonic() if now is None else now

    def remove_participant(self, user_id: int) -> None:
        self._active_participants.discard(user_id)
        if user_id == self._floor_owner_id:
            self._floor_owner_id = None
        self._held_turns = deque(
            turn for turn in self._held_turns if turn.user_id != user_id
        )

    def release_participants(self) -> None:
        self._active_participants.clear()
        self._floor_owner_id = None
        self._held_turns.clear()
        self.touch()

    def route_speech(
        self,
        user_id: int,
        *,
        via_wake_word: bool,
        agent_speaking: bool,
    ) -> TurnDisposition:
        """Authorize one utterance and choose send/barge-in/hold/ignore."""
        if self.cross_user_wake_required and user_id != self._floor_owner_id:
            if not via_wake_word:
                return TurnDisposition.IGNORE
            taking_over = self._floor_owner_id is not None
            self._floor_owner_id = user_id
            self._active_participants.add(user_id)
            self.touch()
            if taking_over:
                return TurnDisposition.BARGE_IN
            if not agent_speaking:
                return TurnDisposition.SEND
            return TurnDisposition(self.new_participant_policy.value)
        is_active = user_id in self._active_participants
        if not is_active:
            if not via_wake_word:
                return TurnDisposition.IGNORE
            self._active_participants.add(user_id)
            policy = self.new_participant_policy
        else:
            policy = self.active_participant_policy

        self._floor_owner_id = user_id

        self.touch()
        if not agent_speaking:
            return TurnDisposition.SEND
        return TurnDisposition(policy.value)

    def enqueue_held_turn(self, turn: HeldTurn) -> None:
        if self.cross_user_wake_required and turn.user_id != self._floor_owner_id:
            return
        audio_data = turn.audio_data[: self.held_turn_max_bytes]
        bounded_turn = HeldTurn(
            user_id=turn.user_id,
            display_name=turn.display_name,
            audio_data=audio_data,
            speech_started_at=turn.speech_started_at,
        )
        if len(self._held_turns) >= self.held_turn_queue_max:
            self._held_turns.popleft()
        self._held_turns.append(bounded_turn)
        self.touch()

    def pop_held_turn(self) -> Optional[HeldTurn]:
        if not self._held_turns:
            return None
        if self.cross_user_wake_required:
            for turn in self._held_turns:
                if turn.user_id == self._floor_owner_id:
                    self._held_turns.remove(turn)
                    self.touch()
                    return turn
            return None
        self.touch()
        return self._held_turns.popleft()

    def release_if_idle(self, *, busy: bool, now: Optional[float] = None) -> bool:
        """Close the upload gate after bilateral silence while keeping AI warm."""
        current = time.monotonic() if now is None else now
        if busy:
            self.touch(current)
            return False
        if not self._active_participants:
            return False
        if current - self._last_activity_at < self.idle_timeout_seconds:
            return False
        self.release_participants()
        return True
