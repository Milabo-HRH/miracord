"""Boundary acknowledgement using Pipecat's own interruption propagation."""
import asyncio
from dataclasses import dataclass, field
from pipecat.frames.frames import InterruptionFrame
from pipecat.processors.frame_processor import FrameProcessor


async def reset_user_turn(user):
    """Discard unfinished aggregation/timers after an acknowledged interruption.

    Pipecat 1.8.1's aggregator.reset() only clears text. Its controller must
    also leave the old turn before we admit a new speaker. Keep this pinned
    framework integration here, rather than emitting unacknowledged stop frames.
    """
    from pipecat.frames.frames import UserStoppedSpeakingFrame
    from pipecat.turns.user_stop import UserTurnStoppedParams
    await user.reset()
    controller = user._user_turn_controller
    await controller.process_frame(UserStoppedSpeakingFrame())
    await controller._trigger_user_turn_stop(
        None, UserTurnStoppedParams(enable_user_speaking_frames=False))
    # Noise can arm VAD timers without ever creating a transcript/user turn.
    for strategy in controller.user_turn_strategies.stop or []:
        await strategy.handle_user_turn_stopped()


@dataclass
class ResetConversationFrame(InterruptionFrame):
    acknowledged: asyncio.Event = field(default_factory=asyncio.Event)


class BoundaryAck(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, ResetConversationFrame):
            frame.acknowledged.set()
        await self.push_frame(frame, direction)
