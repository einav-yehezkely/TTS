"""Export narration to WAV or MP3, streaming segment by segment to disk."""

import os
from collections.abc import Callable
from pathlib import Path

import lameenc
import numpy as np
import soundfile as sf

from .audiocache import AudioCache
from .engine import SAMPLE_RATE, CancelToken, Cancelled, KokoroEngine
from .segments import Segment


def export_audio(engine: KokoroEngine, cache: AudioCache, segments: list[Segment], out_path: str | Path,
                 voice: str, speed: float, progress: Callable[[int, int], None] | None = None,
                 token: CancelToken | None = None, bitrate: int = 128) -> float:
    """Write the segments as one audio file. Returns its duration in seconds."""
    out_path = Path(out_path)
    fmt = out_path.suffix.lower()
    if fmt not in (".wav", ".mp3"):
        raise ValueError("Choose a .wav or .mp3 file name")
    tmp = out_path.with_name(out_path.name + ".part")
    samples = 0
    wav = None
    mp3 = None
    enc = None
    try:
        if fmt == ".wav":
            wav = sf.SoundFile(tmp, "w", SAMPLE_RATE, 1, subtype="PCM_16", format="WAV")
        else:
            enc = lameenc.Encoder()
            enc.set_bit_rate(bitrate)
            enc.set_in_sample_rate(SAMPLE_RATE)
            enc.set_channels(1)
            enc.set_quality(2)
            mp3 = open(tmp, "wb")
        for i, seg in enumerate(segments):
            if token and token.cancelled:
                raise Cancelled()
            key = AudioCache.key(voice, speed, seg.pause, seg.fragments)
            synth = cache.get(key)
            if synth is None:
                synth = engine.synthesize(seg.fragments, voice, speed, seg.pause, token)
                cache.put(key, synth)
            pcm = (np.clip(synth.audio, -1.0, 1.0) * 32767).astype(np.int16)
            if wav is not None:
                wav.write(pcm)
            else:
                mp3.write(enc.encode(pcm.tobytes()))
            samples += len(pcm)
            if progress:
                progress(i + 1, len(segments))
        if mp3 is not None:
            mp3.write(enc.flush())
    except BaseException:
        for f in (wav, mp3):
            if f is not None:
                f.close()
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    for f in (wav, mp3):
        if f is not None:
            f.close()
    os.replace(tmp, out_path)
    return samples / SAMPLE_RATE
