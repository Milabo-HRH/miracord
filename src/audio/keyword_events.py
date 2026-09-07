"""Track rolling ASR keyword events using audio offsets, not wall-clock cooldowns."""


class KeywordEventTracker:
    """One tracker per user's audio stream; offsets are exclusive sample indices.

    After a stop, the caller must decode only audio at/after minimum_start.
    Merely filtering text from an overlapping old window cannot distinguish a
    new wake from a revised transcription of the old wake.
    """

    STOP_WORDS = frozenset({'闭嘴', '结束'})

    def __init__(self):
        self.minimum_start = 0
        self.previous = set()

    def consume(self, start, end, candidates):
        if start < self.minimum_start or end <= start:
            return []
        stops = [word for word in candidates if word in self.STOP_WORDS]
        if stops:
            self.minimum_start = end
            self.previous.clear()
            # With no word alignment, stop takes priority over wake in the
            # same window. These events do not decide whether to mute output.
            return stops
        events = [word for word in candidates if word not in self.previous]
        self.previous = set(candidates)
        return events
