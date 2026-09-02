from src.bot.session.conversation_router import (
    GuildConversationRouter,
    HeldTurn,
    TurnDisposition,
)


def make_router(
    *, active_policy: str = "barge_in", new_policy: str = "hold"
) -> GuildConversationRouter:
    return GuildConversationRouter(
        active_participant_policy=active_policy,
        new_participant_policy=new_policy,
        idle_timeout_seconds=10,
        held_turn_max_seconds=1,
        held_turn_queue_max=2,
        source_bytes_per_second=10,
    )


def test_inactive_audio_never_passes_upload_gate_without_wake_word():
    router = make_router()

    disposition = router.route_speech(1, via_wake_word=False, agent_speaking=False)

    assert disposition is TurnDisposition.IGNORE
    assert router.active_participants == set()


def test_same_active_user_can_barge_in_without_repeating_wake_word():
    router = make_router(active_policy="barge_in")
    assert (
        router.route_speech(1, via_wake_word=True, agent_speaking=False)
        is TurnDisposition.SEND
    )

    disposition = router.route_speech(1, via_wake_word=False, agent_speaking=True)

    assert disposition is TurnDisposition.BARGE_IN
    assert router.active_participants == {1}


def test_new_participant_uses_separate_hold_policy():
    router = make_router(active_policy="barge_in", new_policy="hold")
    router.route_speech(1, via_wake_word=True, agent_speaking=False)

    disposition = router.route_speech(2, via_wake_word=True, agent_speaking=True)

    assert disposition is TurnDisposition.HOLD
    assert router.active_participants == {1, 2}


def test_held_turn_queue_is_bounded_by_duration_and_count():
    router = make_router()
    for user_id in (1, 2, 3):
        router.enqueue_held_turn(
            HeldTurn(user_id, str(user_id), b"x" * 20, float(user_id))
        )

    first = router.pop_held_turn()
    second = router.pop_held_turn()

    assert first and first.user_id == 2 and len(first.audio_data) == 10
    assert second and second.user_id == 3 and len(second.audio_data) == 10
    assert router.pop_held_turn() is None


def test_idle_release_waits_for_full_bilateral_silence_window():
    router = make_router()
    router.route_speech(1, via_wake_word=True, agent_speaking=False)
    router.touch(now=100)

    assert router.release_if_idle(busy=True, now=120) is False
    assert router.release_if_idle(busy=False, now=129.9) is False
    assert router.release_if_idle(busy=False, now=130) is True
    assert router.active_participants == set()
