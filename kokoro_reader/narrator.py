"""Playback controller: generates segments ahead of playback and drives the highlight.

A background thread synthesizes segments in reading order, at most PREFETCH
segments (and MAX_AHEAD seconds) ahead of what is being heard. Every seek,
stop, voice or speed change starts a new "epoch": work from an older epoch is
discarded, the GPU run in progress is terminated, and the player's buffers are
dropped, so cancellation takes effect immediately.

A Qt timer polls the player clock and emits the PDF line being heard.
"""

import logging
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .audiocache import AudioCache
from .engine import SAMPLE_RATE, Cancelled, CancelToken, KokoroEngine
from .player import Player, PlayItem
from .segments import Segment

log = logging.getLogger(__name__)

PREFETCH = 4
MAX_AHEAD = 90.0  # seconds of audio queued beyond the current segment


class Narrator(QObject):
    lineChanged = Signal(int)       # line id being heard
    segmentChanged = Signal(int)
    stateChanged = Signal(str)      # stopped / playing / paused / buffering
    message = Signal(str)
    error = Signal(str)

    def __init__(self, engine: KokoroEngine, cache: AudioCache, player: Player | None = None):
        super().__init__()
        self.engine = engine
        self.cache = cache
        self.player = player or Player()
        self.voice = "af_heart"
        self.speed = 1.0
        self.segments: list[Segment] = []
        self.line_to_seg: dict[int, int] = {}
        self._cv = threading.Condition()
        self._epoch = 0
        self._token = CancelToken()
        self._next = 0          # next segment index to generate
        self._end = 0           # one past the last segment to generate
        self._start_line: int | None = None
        self._start_seg = 0
        self._active = False    # generation wanted
        self._closing = False
        self._gen_busy = False
        self._gen_done = True
        self.state = "stopped"
        self.current_seg = 0
        self.current_line: int | None = None
        self._settings_dirty = False
        self.synth_times: list[tuple[int, float, float]] = []  # (segment, seconds to make, audio seconds)
        self._thread = threading.Thread(target=self._worker, name="narration-generator", daemon=True)
        self._thread.start()
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ---- document --------------------------------------------------------------------------------------
    def set_segments(self, segments: list[Segment], reading_order: list[int]) -> None:
        """reading_order: every line id of the document in reading order, used to map clicks."""
        self.stop()
        self.segments = segments
        direct: dict[int, int] = {}
        for s in segments:
            for lid, _ in s.fragments:
                direct.setdefault(lid, s.index)
        self.line_to_seg = {}
        pending: list[int] = []
        for lid in reading_order:  # lines that are not narrated map to the next narrated segment
            if lid in direct:
                for p in pending:
                    self.line_to_seg[p] = direct[lid]
                pending = []
                self.line_to_seg[lid] = direct[lid]
            else:
                pending.append(lid)
        for p in pending:
            self.line_to_seg[p] = max(0, len(segments) - 1)
        self.current_seg = 0
        self.current_line = segments[0].first_line if segments else None

    def replace_segments(self, segments: list[Segment], reading_order: list[int]) -> None:
        """New segmentation after edits, keeping the reading position."""
        line = self.current_line
        was = self.state
        self.set_segments(segments, reading_order)
        if line is not None and line in self.line_to_seg:
            self.current_seg = self.line_to_seg[line]
            self.current_line = line if any(l == line for l, _ in segments[self.current_seg].fragments) \
                else segments[self.current_seg].first_line if segments else None
        if was in ("playing", "buffering") and segments:
            self.play(self.current_seg, self.current_line)

    def seek_line(self, line_id: int) -> None:
        """Move the reading position without starting playback."""
        seg = self.line_to_seg.get(line_id)
        if seg is None:
            return
        self.current_seg = seg
        frag_lines = [l for l, _ in self.segments[seg].fragments]
        self.current_line = line_id if line_id in frag_lines else self.segments[seg].first_line
        self.lineChanged.emit(self.current_line)
        self.segmentChanged.emit(seg)

    # ---- controls -----------------------------------------------------------------------------------------
    def play(self, seg: int | None = None, line: int | None = None) -> None:
        if not self.segments:
            return
        if seg is None:
            seg, line = self.current_seg, self.current_line
        seg = max(0, min(seg, len(self.segments) - 1))
        frag_lines = [l for l, _ in self.segments[seg].fragments]
        if line not in frag_lines or line == frag_lines[0]:
            line = None
        with self._cv:  # one critical section, so the worker never sees the new epoch with an old start
            self._bump_epoch()
            self._next = seg
            self._end = len(self.segments)
            self._start_seg = seg
            self._start_line = line
            self._active = True
            self._gen_done = False
        self.player.clear()
        self._settings_dirty = False
        self.player.resume()
        self.current_seg = seg
        self.current_line = line if line is not None else self.segments[seg].first_line
        self.lineChanged.emit(self.current_line)
        self.segmentChanged.emit(seg)
        self._set_state("buffering")

    def play_line(self, line_id: int) -> None:
        seg = self.line_to_seg.get(line_id)
        if seg is not None:
            self.play(seg, line_id)

    def pause(self) -> None:
        if self.state in ("playing", "buffering"):
            self.player.pause()
            self._set_state("paused")

    def resume(self) -> None:
        if self.state == "paused":
            if self._settings_dirty:
                self.play(self.current_seg, self.current_line)
            else:
                self.player.resume()
                self._set_state("playing")
        elif self.state == "stopped":
            self.play()

    def toggle(self) -> None:
        if self.state in ("playing", "buffering"):
            self.pause()
        else:
            self.resume()

    def stop(self) -> None:
        with self._cv:
            self._bump_epoch()
            self._active = False
            self._gen_done = True
        if self.player.paused:
            self.player.resume()
        self.player.clear()
        self._set_state("stopped")

    def next_sentence(self) -> None:
        self._jump(self.current_seg + 1)

    def prev_sentence(self) -> None:
        self._jump(self.current_seg - 1)

    def _jump(self, seg: int) -> None:
        if not self.segments:
            return
        seg = max(0, min(seg, len(self.segments) - 1))
        if self.state in ("playing", "buffering"):
            self.play(seg)
        else:  # paused or stopped: move the position, play from there on resume
            if self.state == "paused":
                self.player.clear()
                self.player.pause()
                self._settings_dirty = True
            self.current_seg = seg
            self.current_line = self.segments[seg].first_line
            self.lineChanged.emit(self.current_line)
            self.segmentChanged.emit(seg)

    def set_voice(self, voice: str) -> None:
        if voice != self.voice:
            self.voice = voice
            self._settings_changed()

    def set_speed(self, speed: float) -> None:
        speed = round(float(speed), 2)
        if speed != self.speed:
            self.speed = speed
            self._settings_changed()

    def _settings_changed(self) -> None:
        if self.state in ("playing", "buffering"):
            self.play(self.current_seg, self.current_line)  # restart at the line being heard
        elif self.state == "paused":
            self._settings_dirty = True

    def shutdown(self) -> None:
        self._timer.stop()
        with self._cv:
            self._bump_epoch()
            self._closing = True
        self.player.close()
        self._thread.join(timeout=3)

    # ---- internals ------------------------------------------------------------------------------------------
    def _bump_epoch(self) -> None:
        """Invalidate all queued and in-flight work. Call with self._cv held."""
        self._epoch += 1
        self._token.cancel()  # terminates a GPU/CPU run in progress
        self._token = CancelToken()
        self._cv.notify_all()

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.stateChanged.emit(state)

    def _ahead_ok(self) -> bool:
        """Whether the generator may make another segment now."""
        queued = self.player.queued_indices()
        if not queued:
            return True
        if self._next - self.current_seg > PREFETCH:
            return False
        ahead = sum(len(self.segments[i].text) for i in queued[1:] if i < len(self.segments)) / 14.0 / self.speed
        return ahead < MAX_AHEAD

    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._closing and (not self._active or self._next >= self._end or not self._ahead_ok()):
                    if self._active and self._next >= self._end:
                        self._gen_done = True
                    self._cv.wait(timeout=0.1)
                if self._closing:
                    return
                epoch, idx, token = self._epoch, self._next, self._token
                seg = self.segments[idx]
                voice, speed = self.voice, self.speed
                start_line = self._start_line if idx == self._start_seg else None
                self._gen_busy = True
            try:
                key = AudioCache.key(voice, speed, seg.pause, seg.fragments)
                synth = self.cache.get(key)
                if synth is None:
                    t = time.perf_counter()
                    synth = self.engine.synthesize(seg.fragments, voice, speed, seg.pause, token)
                    made = time.perf_counter() - t
                    self.synth_times.append((idx, made, len(synth.audio) / SAMPLE_RATE))
                    self.cache.put(key, synth)
            except Cancelled:
                with self._cv:
                    self._gen_busy = False
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("synthesis failed")
                self.error.emit(f"Speech generation failed: {e}")
                with self._cv:
                    self._gen_busy = False
                    if epoch == self._epoch:
                        self._next += 1  # skip the segment rather than stall
                continue
            with self._cv:
                self._gen_busy = False
                if epoch != self._epoch:
                    continue
                start = synth.start_of(start_line) if start_line is not None else 0
                self.player.enqueue(PlayItem(epoch, idx, synth, start))
                self._next += 1

    def _tick(self) -> None:
        if self.state not in ("playing", "buffering"):
            return
        pos = self.player.position()
        if pos is not None:
            item, sample = pos
            if item.epoch == self._epoch:
                line = item.synth.line_at(sample)
                if item.index != self.current_seg:
                    self.current_seg = item.index
                    self.segmentChanged.emit(item.index)
                    with self._cv:
                        self._cv.notify_all()
                if line is not None and line != self.current_line:
                    self.current_line = line
                    self.lineChanged.emit(line)
        if self.player.drained():
            with self._cv:
                done = self._gen_done and not self._gen_busy and self._next >= self._end
            if done:
                self.stop()
                self.message.emit("Finished reading.")
            else:
                self._set_state("buffering")
        else:
            self._set_state("playing")
