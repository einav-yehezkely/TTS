"""Audio output with a playback clock that tracks what is actually being heard.

The PortAudio callback copies queued audio to the device and records, for each
copied run, which device frame it starts at. The audible device frame is
derived from the DAC timestamps PortAudio reports (or the stream latency when a
host API gives none), so highlighting follows the sound coming out of the
speakers, not the data handed to the driver. Underruns, pauses and seeks are
handled by the same mapping: silence advances the device clock but no audio
position, so the highlight simply waits.

Pause and stop abort the stream, which drops the audio already buffered in the
device; pause then re-queues from the exact sample that was being heard.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass

import sounddevice as sd

from .engine import SAMPLE_RATE, Synth


@dataclass(eq=False)
class PlayItem:
    epoch: int
    index: int     # segment index
    synth: Synth
    start: int = 0  # first sample to play


class Player:
    def __init__(self, samplerate: int = SAMPLE_RATE, device=None):
        self.sr = samplerate
        self.device = device
        self._lock = threading.RLock()
        self._queue: deque[PlayItem] = deque()
        self._cur: PlayItem | None = None
        self._cur_pos = 0
        self._popped: deque[PlayItem] = deque(maxlen=8)  # items already sent, newest last
        self._dev = 0  # device frames produced since the stream (re)started
        self._runs: deque[tuple[int, PlayItem, int, int]] = deque()  # (device frame, item, item sample, n)
        self._clock: tuple[int, float] | None = None  # (device frame at buffer start, its DAC time)
        self._last_audible = 0
        self._paused = False
        self._started = False
        self.underruns = 0
        self.error = ""
        self._stream: sd.OutputStream | None = None

    # ---- stream management -------------------------------------------------------------------------
    def _open(self) -> None:
        self._stream = sd.OutputStream(samplerate=self.sr, channels=1, dtype="float32", device=self.device,
                                       callback=self._callback, blocksize=480, latency="low")

    def _restart_stream(self) -> None:
        """Drop everything buffered in the device and start a fresh clock."""
        if self._stream is None:
            self._open()
        else:
            try:
                self._stream.abort()
            except sd.PortAudioError:
                pass
        with self._lock:
            self._dev = 0
            self._runs.clear()
            self._clock = None
            self._last_audible = 0
        if not self._paused:
            self._stream.start()
            self._started = True

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except sd.PortAudioError:
                pass
            self._stream = None

    @property
    def latency(self) -> float:
        try:
            return float(self._stream.latency) if self._stream is not None else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    # ---- callback (PortAudio thread) -----------------------------------------------------------------
    def _callback(self, outdata, frames, time_info, status) -> None:
        out = outdata[:, 0]
        filled = 0
        with self._lock:
            if status.output_underflow:
                self.underruns += 1
            dac = getattr(time_info, "outputBufferDacTime", 0.0) or 0.0
            if dac > 0:
                self._clock = (self._dev, dac)
            if not self._paused:
                while filled < frames:
                    if self._cur is None:
                        if not self._queue:
                            break
                        self._cur = self._queue.popleft()
                        self._cur_pos = self._cur.start
                        self._popped.append(self._cur)
                    audio = self._cur.synth.audio
                    n = min(frames - filled, len(audio) - self._cur_pos)
                    if n > 0:
                        out[filled:filled + n] = audio[self._cur_pos:self._cur_pos + n]
                        self._runs.append((self._dev + filled, self._cur, self._cur_pos, n))
                        filled += n
                        self._cur_pos += n
                    if self._cur_pos >= len(audio):
                        self._cur = None
            out[filled:] = 0.0
            self._dev += frames
            horizon = self._dev - 10 * self.sr
            while len(self._runs) > 1 and self._runs[0][0] + self._runs[0][3] < horizon:
                self._runs.popleft()

    # ---- clock ---------------------------------------------------------------------------------------
    def _audible_frame(self) -> int:
        """Device frame being heard right now."""
        stream = self._stream
        audible = None
        if self._clock is not None and stream is not None:
            dev0, dac = self._clock
            try:
                now = stream.time
                if now > 0:
                    audible = dev0 + int((now - dac) * self.sr)
            except Exception:  # noqa: BLE001
                audible = None
        if audible is None:
            audible = self._dev - int(self.latency * self.sr)
        audible = max(self._last_audible, min(audible, self._dev))
        self._last_audible = audible
        return audible

    def position(self) -> tuple[PlayItem, int] | None:
        """The item and sample being heard, or the last one heard if output is silent."""
        with self._lock:
            if not self._runs:
                return None
            audible = self._audible_frame()
            last = None
            for dev, item, pos, n in reversed(self._runs):
                if dev <= audible < dev + n:
                    return item, pos + (audible - dev)
                if dev + n <= audible:
                    last = (item, pos + n)
                    break
            if last is None:  # audible frame is before the first recorded run (start latency)
                dev, item, pos, n = self._runs[0]
                return item, pos
            return last

    # ---- control -------------------------------------------------------------------------------------
    def enqueue(self, item: PlayItem) -> None:
        with self._lock:
            self._queue.append(item)
        if not self._started and not self._paused:
            self._restart_stream()

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()
            self._cur = None
            self._popped.clear()
        self._restart_stream()

    def pause(self) -> None:
        if self._paused:
            return
        pos = self.position()
        with self._lock:
            self._paused = True
            if pos is not None:  # re-queue from the sample being heard; buffered audio is dropped below
                item, sample = pos
                later: list[PlayItem] = []
                seen = False
                for it in self._popped:
                    if it is item:
                        seen = True
                    elif seen:
                        later.append(it)
                resumed = PlayItem(item.epoch, item.index, item.synth, min(sample, len(item.synth.audio)))
                self._queue.extendleft(reversed([resumed] + later))
                self._cur = None
                self._popped.clear()
        if self._stream is not None:
            try:
                self._stream.abort()
            except sd.PortAudioError:
                pass
        self._started = False
        with self._lock:
            # keep a run at the paused position so position() keeps reporting it while paused
            if pos is not None:
                self._runs.clear()
                self._runs.append((0, pos[0], pos[1], 0))
                self._dev, self._clock, self._last_audible = 0, None, 0

    def resume(self) -> None:
        if not self._paused:
            return
        self._paused = False
        self._restart_stream()

    @property
    def paused(self) -> bool:
        return self._paused

    def pending(self) -> int:
        """Items not yet fully sent to the device."""
        with self._lock:
            return len(self._queue) + (1 if self._cur is not None else 0)

    def queued_indices(self) -> list[int]:
        with self._lock:
            out = [self._cur.index] if self._cur is not None else []
            return out + [it.index for it in self._queue]

    def drained(self) -> bool:
        """Everything sent has been heard and nothing is waiting."""
        with self._lock:
            if self._queue or self._cur is not None:
                return False
            if not self._runs:
                return True
            dev, _, _, n = self._runs[-1]
            return self._audible_frame() >= dev + n


def wait_until(predicate, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
