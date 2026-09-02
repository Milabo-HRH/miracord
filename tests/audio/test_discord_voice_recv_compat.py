from unittest.mock import MagicMock

import davey

from src.audio.discord_voice_recv_compat import DaveAwarePacketDecoder


def _decoder() -> DaveAwarePacketDecoder:
    decoder = DaveAwarePacketDecoder.__new__(DaveAwarePacketDecoder)
    decoder.router = MagicMock()
    decoder.router.sink.voice_client._connection.dave_protocol_version = 1
    decoder.router.sink.voice_client._connection.dave_session = MagicMock()
    decoder._decoder = MagicMock()
    decoder._cached_id = 42
    decoder.ssrc = 123
    return decoder


def test_decrypts_dave_before_opus() -> None:
    decoder = _decoder()
    packet = MagicMock()
    packet.__bool__.return_value = True
    packet.decrypted_data = b"dave ciphertext"
    decoder.router.sink.voice_client._connection.dave_session.decrypt.return_value = (
        b"opus payload"
    )
    decoder._decoder.decode.return_value = b"pcm"

    returned_packet, pcm = decoder._decode_packet(packet)

    decoder.router.sink.voice_client._connection.dave_session.decrypt.assert_called_once_with(
        42, davey.MediaType.audio, b"dave ciphertext"
    )
    decoder._decoder.decode.assert_called_once_with(b"opus payload", fec=False)
    assert returned_packet is packet
    assert packet.decrypted_data == b"opus payload"
    assert pcm == b"pcm"


def test_unresolved_dave_user_uses_packet_loss_concealment() -> None:
    decoder = _decoder()
    decoder._cached_id = None
    decoder.router.sink.voice_client._get_id_from_ssrc.return_value = None
    packet = MagicMock()
    packet.__bool__.return_value = True
    packet.decrypted_data = b"dave ciphertext"
    decoder._decoder.decode.return_value = b"silence"

    _, pcm = decoder._decode_packet(packet)

    decoder.router.sink.voice_client._connection.dave_session.decrypt.assert_not_called()
    decoder._decoder.decode.assert_called_once_with(None, fec=False)
    assert pcm == b"silence"


def test_accepts_plain_opus_during_dave_transition() -> None:
    decoder = _decoder()
    packet = MagicMock()
    packet.__bool__.return_value = True
    packet.decrypted_data = b"plain opus"
    decoder.router.sink.voice_client._connection.dave_session.decrypt.side_effect = (
        RuntimeError(
            "Failed to decrypt: "
            "DecryptionFailed(UnencryptedWhenPassthroughDisabled)"
        )
    )
    decoder._decoder.decode.return_value = b"pcm"

    returned_packet, pcm = decoder._decode_packet(packet)

    decoder._decoder.decode.assert_called_once_with(b"plain opus", fec=False)
    assert returned_packet is packet
    assert pcm == b"pcm"


def test_bad_plaintext_packet_uses_packet_loss_concealment() -> None:
    decoder = _decoder()
    packet = MagicMock()
    packet.__bool__.return_value = True
    packet.decrypted_data = b"not opus"
    decoder.router.sink.voice_client._connection.dave_session.decrypt.side_effect = (
        RuntimeError(
            "Failed to decrypt: "
            "DecryptionFailed(UnencryptedWhenPassthroughDisabled)"
        )
    )
    decoder._decoder.decode.side_effect = [ValueError("corrupt"), b"silence"]

    _, pcm = decoder._decode_packet(packet)

    assert decoder._decoder.decode.call_args_list[0].args == (b"not opus",)
    assert decoder._decoder.decode.call_args_list[0].kwargs == {"fec": False}
    assert decoder._decoder.decode.call_args_list[1].args == (None,)
    assert decoder._decoder.decode.call_args_list[1].kwargs == {"fec": False}
    assert pcm == b"silence"
