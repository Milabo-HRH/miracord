from src.bot.session.conversation_router import (
    GuildConversationRouter,
    HeldTurn,
    TurnDisposition,
)


def make_router(
    *, active_policy: str = "barge_in", new_policy: str = "hold",
    cross_user_wake_required: bool = False,
) -> GuildConversationRouter:
    return GuildConversationRouter(
        active_participant_policy=active_policy,
        new_participant_policy=new_policy,
        idle_timeout_seconds=10,
        held_turn_max_seconds=1,
        held_turn_queue_max=2,
        source_bytes_per_second=10,
        cross_user_wake_required=cross_user_wake_required,
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


def test_floor_owner_can_continue_and_other_active_user_requires_explicit_wake():
    router = make_router(cross_user_wake_required=True)
    assert router.route_speech(1, via_wake_word=True, agent_speaking=False) == TurnDisposition.SEND
    assert router.route_speech(2, via_wake_word=True, agent_speaking=True) == TurnDisposition.BARGE_IN
    assert router.floor_owner_id == 2
    router.touch(now=100)
    assert router.route_speech(1, via_wake_word=False, agent_speaking=False) == TurnDisposition.IGNORE
    assert router.last_activity_at == 100
    assert router.route_speech(2, via_wake_word=False, agent_speaking=True) == TurnDisposition.BARGE_IN
    assert router.route_speech(1, via_wake_word=True, agent_speaking=False) == TurnDisposition.BARGE_IN
    assert router.floor_owner_id == 1


def test_explicit_floor_takeover_overrides_hold_but_preserves_pending_owner_audio():
    router = make_router(cross_user_wake_required=True, new_policy="hold")
    router.route_speech(1, via_wake_word=True, agent_speaking=False)
    router.enqueue_held_turn(HeldTurn(1, "one", b"pcm", 1))
    assert router.route_speech(2, via_wake_word=True, agent_speaking=True) == TurnDisposition.BARGE_IN
    assert router.has_held_turns
    assert router.pop_held_turn() is None
    router.enqueue_held_turn(HeldTurn(1, "one", b"old", 1))
    router.route_speech(1, via_wake_word=True, agent_speaking=True)
    assert router.pop_held_turn().audio_data == b"pcm"
    assert router.pop_held_turn() is None


def test_floor_resets_on_stop_and_idle_without_reviving_previous_owner():
    router = make_router(cross_user_wake_required=True)
    router.route_speech(1, via_wake_word=True, agent_speaking=False)
    router.route_speech(2, via_wake_word=True, agent_speaking=False)
    router.remove_participant(2)
    assert router.floor_owner_id is None
    assert router.route_speech(1, via_wake_word=False, agent_speaking=False) == TurnDisposition.IGNORE
    router.route_speech(1, via_wake_word=True, agent_speaking=False)
    router.touch(now=100)
    assert router.release_if_idle(busy=False, now=110)
    assert router.floor_owner_id is None
