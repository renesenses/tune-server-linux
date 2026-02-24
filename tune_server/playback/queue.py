from __future__ import annotations

import random
from typing import Optional

import structlog

from tune_server.models import RepeatMode, Track

logger = structlog.get_logger()


class PlayQueue:
    """In-memory play queue with shuffle and repeat support."""

    def __init__(self) -> None:
        self._tracks: list[Track] = []
        self._position: int = -1
        self._shuffle: bool = False
        self._repeat: RepeatMode = RepeatMode.OFF
        self._shuffle_order: list[int] = []
        self._shuffle_index: int = -1

    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks)

    @property
    def position(self) -> int:
        return self._position

    @property
    def current(self) -> Optional[Track]:
        if 0 <= self._position < len(self._tracks):
            if self._shuffle and self._shuffle_order:
                idx = self._shuffle_order[self._shuffle_index]
                return self._tracks[idx]
            return self._tracks[self._position]
        return None

    @property
    def length(self) -> int:
        return len(self._tracks)

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @shuffle.setter
    def shuffle(self, value: bool) -> None:
        self._shuffle = value
        if value:
            self._regenerate_shuffle()
        else:
            self._shuffle_order = []
            self._shuffle_index = -1

    @property
    def repeat(self) -> RepeatMode:
        return self._repeat

    @repeat.setter
    def repeat(self, value: RepeatMode) -> None:
        self._repeat = value

    def set_tracks(self, tracks: list[Track], start_position: int = 0) -> None:
        self._tracks = list(tracks)
        self._position = start_position if tracks else -1
        if self._shuffle:
            self._regenerate_shuffle()
            self._shuffle_index = 0

    def add_tracks(self, tracks: list[Track], position: Optional[int] = None) -> None:
        if position is not None:
            for i, track in enumerate(tracks):
                self._tracks.insert(position + i, track)
        else:
            self._tracks.extend(tracks)

        if self._shuffle:
            self._regenerate_shuffle()

    def remove_track(self, position: int) -> None:
        if 0 <= position < len(self._tracks):
            self._tracks.pop(position)
            if position < self._position:
                self._position -= 1
            elif position == self._position:
                self._position = min(self._position, len(self._tracks) - 1)
            if self._shuffle:
                self._regenerate_shuffle()

    def move_track(self, from_position: int, to_position: int) -> bool:
        if not (0 <= from_position < len(self._tracks)):
            return False
        if not (0 <= to_position < len(self._tracks)):
            return False
        if from_position == to_position:
            return True

        track = self._tracks.pop(from_position)
        self._tracks.insert(to_position, track)

        # Adjust current position
        if self._position == from_position:
            self._position = to_position
        elif from_position < self._position <= to_position:
            self._position -= 1
        elif to_position <= self._position < from_position:
            self._position += 1

        if self._shuffle:
            self._regenerate_shuffle()
        return True

    def clear(self) -> None:
        self._tracks.clear()
        self._position = -1
        self._shuffle_order.clear()
        self._shuffle_index = -1

    def next(self) -> Optional[Track]:
        if not self._tracks:
            return None

        if self._repeat == RepeatMode.ONE:
            return self.current

        if self._shuffle:
            self._shuffle_index += 1
            if self._shuffle_index >= len(self._shuffle_order):
                if self._repeat == RepeatMode.ALL:
                    self._regenerate_shuffle()
                    self._shuffle_index = 0
                else:
                    return None
            self._position = self._shuffle_order[self._shuffle_index]
        else:
            self._position += 1
            if self._position >= len(self._tracks):
                if self._repeat == RepeatMode.ALL:
                    self._position = 0
                else:
                    self._position = len(self._tracks)
                    return None

        return self.current

    def previous(self) -> Optional[Track]:
        if not self._tracks:
            return None

        if self._shuffle:
            self._shuffle_index = max(0, self._shuffle_index - 1)
            self._position = self._shuffle_order[self._shuffle_index]
        else:
            self._position = max(0, self._position - 1)

        return self.current

    def peek_next(self) -> Optional[Track]:
        """Look at the next track without advancing."""
        if not self._tracks:
            return None

        if self._repeat == RepeatMode.ONE:
            return self.current

        if self._shuffle:
            next_idx = self._shuffle_index + 1
            if next_idx >= len(self._shuffle_order):
                if self._repeat == RepeatMode.ALL:
                    return self._tracks[0]  # Will reshuffle
                return None
            return self._tracks[self._shuffle_order[next_idx]]
        else:
            next_pos = self._position + 1
            if next_pos >= len(self._tracks):
                if self._repeat == RepeatMode.ALL:
                    return self._tracks[0]
                return None
            return self._tracks[next_pos]

    def jump_to(self, position: int) -> Optional[Track]:
        if 0 <= position < len(self._tracks):
            self._position = position
            if self._shuffle:
                try:
                    self._shuffle_index = self._shuffle_order.index(position)
                except ValueError:
                    pass
            return self.current
        return None

    def _regenerate_shuffle(self) -> None:
        self._shuffle_order = list(range(len(self._tracks)))
        # Keep current track at the start
        if 0 <= self._position < len(self._tracks):
            self._shuffle_order.remove(self._position)
            random.shuffle(self._shuffle_order)
            self._shuffle_order.insert(0, self._position)
            self._shuffle_index = 0
        else:
            random.shuffle(self._shuffle_order)
            self._shuffle_index = 0
