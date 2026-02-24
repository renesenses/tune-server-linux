from __future__ import annotations

import pytest

from tune_server.models import RepeatMode, Track
from tune_server.playback.queue import PlayQueue


def _make_tracks(n: int) -> list[Track]:
    return [
        Track(id=i, title=f"Track {i}", file_path=f"/music/{i}.flac")
        for i in range(1, n + 1)
    ]


class TestBasic:
    def test_set_tracks(self):
        q = PlayQueue()
        tracks = _make_tracks(3)
        q.set_tracks(tracks)
        assert q.length == 3
        assert q.position == 0
        assert q.current.title == "Track 1"

    def test_empty_queue(self):
        q = PlayQueue()
        assert q.length == 0
        assert q.position == -1
        assert q.current is None

    def test_set_tracks_empty(self):
        q = PlayQueue()
        q.set_tracks([])
        assert q.length == 0
        assert q.position == -1


class TestNavigation:
    def test_next_advances(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(3))
        t = q.next()
        assert t.title == "Track 2"
        assert q.position == 1

    def test_previous_goes_back(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(3))
        q.next()  # position 1
        t = q.previous()
        assert t.title == "Track 1"
        assert q.position == 0

    def test_previous_at_start(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(3))
        t = q.previous()
        assert t.title == "Track 1"
        assert q.position == 0


class TestRepeatOff:
    def test_next_past_end_returns_none(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(2))
        q.next()  # Track 2
        result = q.next()  # past end
        assert result is None
        assert q.position == 2  # past last index

    def test_empty_queue_next(self):
        q = PlayQueue()
        assert q.next() is None

    def test_empty_queue_previous(self):
        q = PlayQueue()
        assert q.previous() is None


class TestRepeatAll:
    def test_wraps_around(self):
        q = PlayQueue()
        q.repeat = RepeatMode.ALL
        q.set_tracks(_make_tracks(2))
        q.next()  # Track 2
        t = q.next()  # wraps to Track 1
        assert t.title == "Track 1"
        assert q.position == 0

    def test_cycles_multiple_times(self):
        q = PlayQueue()
        q.repeat = RepeatMode.ALL
        q.set_tracks(_make_tracks(2))
        for _ in range(6):
            t = q.next()
            assert t is not None


class TestRepeatOne:
    def test_returns_same_track(self):
        q = PlayQueue()
        q.repeat = RepeatMode.ONE
        q.set_tracks(_make_tracks(3))
        first = q.current
        for _ in range(5):
            t = q.next()
            assert t.id == first.id


class TestShuffle:
    def test_shuffle_on(self):
        q = PlayQueue()
        q.shuffle = True
        tracks = _make_tracks(10)
        q.set_tracks(tracks)
        # Current track should still be at position 0 in shuffle
        assert q.current is not None
        assert q.current.id == tracks[0].id

    def test_shuffle_off(self):
        q = PlayQueue()
        q.shuffle = True
        q.set_tracks(_make_tracks(5))
        q.shuffle = False
        assert q._shuffle_order == []
        assert q._shuffle_index == -1

    def test_shuffle_repeat_all_continues(self):
        q = PlayQueue()
        q.shuffle = True
        q.repeat = RepeatMode.ALL
        q.set_tracks(_make_tracks(3))
        # Exhaust the shuffle
        for _ in range(3):
            t = q.next()
        # Should regenerate and continue
        t = q.next()
        assert t is not None


class TestAddTracks:
    def test_append(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(2))
        q.add_tracks([Track(id=99, title="New")])
        assert q.length == 3
        assert q.tracks[-1].title == "New"

    def test_insert_at_position(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(3))
        q.add_tracks([Track(id=99, title="Inserted")], position=1)
        assert q.length == 4
        assert q.tracks[1].title == "Inserted"


class TestRemoveTrack:
    def test_remove_before_current(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(3))
        q.next()  # position 1
        q.remove_track(0)  # remove before
        assert q.position == 0
        assert q.length == 2

    def test_remove_at_current(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(3))
        q.next()  # position 1
        q.remove_track(1)  # remove current
        assert q.position == 1  # clamped to len-1
        assert q.length == 2

    def test_remove_after_current(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(3))
        q.remove_track(2)  # remove after
        assert q.position == 0
        assert q.length == 2


class TestClear:
    def test_clear(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(5))
        q.clear()
        assert q.length == 0
        assert q.position == -1
        assert q.current is None


class TestPeekNext:
    def test_peek_without_advancing(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(3))
        peeked = q.peek_next()
        assert peeked.title == "Track 2"
        assert q.position == 0  # didn't advance

    def test_peek_at_end(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(2))
        q.next()  # position 1 = last
        assert q.peek_next() is None

    def test_peek_repeat_all(self):
        q = PlayQueue()
        q.repeat = RepeatMode.ALL
        q.set_tracks(_make_tracks(2))
        q.next()  # position 1
        peeked = q.peek_next()
        assert peeked.title == "Track 1"

    def test_peek_repeat_one(self):
        q = PlayQueue()
        q.repeat = RepeatMode.ONE
        q.set_tracks(_make_tracks(3))
        peeked = q.peek_next()
        assert peeked.id == q.current.id


class TestJumpTo:
    def test_valid_position(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(5))
        t = q.jump_to(3)
        assert t.title == "Track 4"
        assert q.position == 3

    def test_invalid_position(self):
        q = PlayQueue()
        q.set_tracks(_make_tracks(3))
        assert q.jump_to(99) is None
        assert q.jump_to(-1) is None
