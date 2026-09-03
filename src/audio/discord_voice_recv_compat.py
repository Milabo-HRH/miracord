"""DAVE receive compatibility for discord-ext-voice-recv.

discord.py 2.7 negotiates Discord's mandatory DAVE E2EE layer, but the current
voice receive extension forwards the still-DAVE-encrypted payload directly to
the Opus decoder. This adapter decrypts that inner layer first.
"""

from __future__ import annotations

import logging
from typing import Any

import davey
from discord.ext.voice_recv import opus as voice_opus
from discord.ext.voice_recv import router as voice_router


logger = logging.getLogger(__name__)


class DaveAwarePacketDecoder(voice_opus.PacketDecoder):
    """Decode transport encryption, then DAVE, then Opus."""

    _miracord_dave_aware = True

    def _decode_packet(self, packet: Any) -> tuple[Any, bytes]:
        connection = getattr(
            getattr(self.sink, "voice_client", None), "_connection", None
        )
        dave_session = getattr(connection, "dave_session", None)
        dave_active = bool(
            dave_session and getattr(connection, "dave_protocol_version", 0)
        )

        if not dave_active:
            return super()._decode_packet(packet)

        assert self._decoder is not None
        if not packet:
            # DAVE ciphertext cannot be used for Opus FEC. Packet-loss
            # concealment keeps the receive thread alive until the next packet.
            return packet, self._decoder.decode(None, fec=False)

        voice_client = self.sink.voice_client
        user_id = self._cached_id or voice_client._get_id_from_ssrc(self.ssrc)
        if user_id is None:
            logger.debug(
                "Dropping DAVE packet for unresolved SSRC %s until its user is known.",
                self.ssrc,
            )
            return packet, self._decoder.decode(None, fec=False)

        try:
            opus_payload = dave_session.decrypt(
                user_id, davey.MediaType.audio, packet.decrypted_data
            )
            if not opus_payload:
                raise ValueError("DAVE returned an empty audio payload")
        except Exception as exc:
            # Discord can briefly send ordinary Opus frames while a DAVE
            # transition is settling. davey deliberately rejects those once
            # passthrough is disabled, but they are already ready for Opus.
            # Treat only this explicit result as plaintext; all other failures
            # still use packet-loss concealment so ciphertext is never decoded
            # as audio.
            if "UnencryptedWhenPassthroughDisabled" in str(exc):
                try:
                    return packet, self._decoder.decode(
                        packet.decrypted_data, fec=False
                    )
                except Exception as opus_exc:
                    logger.debug(
                        "Skipping non-Opus plaintext RTP packet for user %s: %s",
                        user_id,
                        opus_exc,
                    )
                    return packet, self._decoder.decode(None, fec=False)
            logger.warning(
                "Could not decrypt DAVE audio for user %s; dropping one packet: %s",
                user_id,
                exc,
            )
            return packet, self._decoder.decode(None, fec=False)

        packet.decrypted_data = bytes(opus_payload)
        return packet, self._decoder.decode(packet.decrypted_data, fec=False)


def install_dave_voice_receive_compat() -> None:
    """Install the decoder adapter once for the pinned receive extension."""
    if getattr(voice_router.PacketDecoder, "_miracord_dave_aware", False):
        return
    voice_router.PacketDecoder = DaveAwarePacketDecoder
    logger.info("Installed Discord DAVE voice-receive compatibility adapter.")
