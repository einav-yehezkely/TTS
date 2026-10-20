"""Kokoro-82M speech synthesis with per-line timing.

Line timing comes from the model itself. The timestamped ONNX export returns
the duration the model predicted for every phoneme token, and the decoder
builds the audio by stretching each token over exactly that many frames. Each
line fragment of a sentence is phonemized separately (in one espeak call that
keeps the fragments apart), so the token index where every line starts is
known, and with it the audio sample where that line starts. The precision is
one model frame (25 ms) plus rounding, far finer than a line of text.

GPU use is verified, not assumed: the first inference runs with ONNX Runtime's
profiler on and the per-node records show which provider executed the kernels.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .gpu import GpuInfo, prepare_cuda_dlls, query_gpu
from .paths import LOG_DIR

prepare_cuda_dlls()

import onnxruntime as ort  # noqa: E402  (must follow DLL setup)
import phonemizer  # noqa: E402
from kokoro_onnx.tokenizer import Tokenizer, _espeak_lock  # noqa: E402
from kokoro_onnx.trim import trim as trim_audio  # noqa: E402

log = logging.getLogger(__name__)
ort.set_default_logger_severity(3)
logging.getLogger("phonemizer").setLevel(logging.ERROR)

SAMPLE_RATE = 24000
MAX_TOKENS = 500  # the model accepts 510 including the two pad tokens
_LEAD = int(0.03 * SAMPLE_RATE)
_TEST_TEXT = "This is a short test of the speech engine."

try:
    ort.preload_dlls(directory="")
except Exception:  # noqa: BLE001 - older ORT or no NVIDIA packages; CPU still works
    pass


class Cancelled(Exception):
    pass


class CancelToken:
    """Cancels the synthesis runs started with it, including one already inside the GPU."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._runs: set[ort.RunOptions] = set()

    def cancel(self) -> None:
        with self._lock:
            self._event.set()
            for ro in self._runs:
                ro.terminate = True

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancelled (True) or the timeout passes (False)."""
        return self._event.wait(timeout)

    def _add(self, ro: ort.RunOptions) -> None:
        with self._lock:
            if self._event.is_set():
                ro.terminate = True
            self._runs.add(ro)

    def _remove(self, ro: ort.RunOptions) -> None:
        with self._lock:
            self._runs.discard(ro)


@dataclass
class Synth:
    audio: np.ndarray  # float32 mono at SAMPLE_RATE
    marks: list[tuple[int, int]] = field(default_factory=list)  # (line id, first sample), ascending

    def line_at(self, sample: int) -> int | None:
        current = self.marks[0][0] if self.marks else None
        for line_id, start in self.marks:
            if start <= sample:
                current = line_id
            else:
                break
        return current

    def start_of(self, line_id: int) -> int:
        for lid, start in self.marks:
            if lid == line_id:
                return start
        return 0


def _profile_share(path: str) -> float:
    """Fraction of model kernel time that ran on the CUDA provider."""
    with open(path, encoding="utf-8") as f:
        events = json.load(f)
    by_provider: dict[str, float] = {}
    for ev in events:
        if ev.get("cat") != "Node" or not ev.get("name", "").endswith("_kernel_time"):
            continue
        prov = ev.get("args", {}).get("provider", "?")
        by_provider[prov] = by_provider.get(prov, 0.0) + float(ev.get("dur", 0))
    total = sum(by_provider.values())
    return by_provider.get("CUDAExecutionProvider", 0.0) / total if total else 0.0


class KokoroEngine:
    def __init__(self, model_path: Path, voices_dir: Path, prefer_gpu: bool = True):
        self.model_path = Path(model_path)
        self.voices_dir = Path(voices_dir)
        self.tokenizer = Tokenizer()
        self.vocab: dict[str, int] = self.tokenizer.vocab
        self._space = self.vocab[" "]
        self._voices: dict[str, np.ndarray] = {}
        self._switch_lock = threading.Lock()
        self.gpu: GpuInfo | None = None
        self.device = "cpu"
        self.gpu_share: float | None = None
        self.gpu_rtf: float | None = None
        self.fallback_reason = ""
        self.on_device_change = None  # callback(engine) when a runtime failure forces CPU
        self._init_session(prefer_gpu)

    # ---- sessions -------------------------------------------------------------------------
    def _init_session(self, prefer_gpu: bool) -> None:
        reason = ""
        if os.environ.get("KOKORO_FORCE_CPU"):
            prefer_gpu, reason = False, "CPU mode was selected."
        if prefer_gpu:
            self.gpu = query_gpu()
            if "CUDAExecutionProvider" not in ort.get_available_providers():
                reason = "This ONNX Runtime build has no CUDA support."
            elif not self.gpu.found:
                reason = f"No NVIDIA GPU was found ({self.gpu.error or 'nvidia-smi gave no GPU'})."
            else:
                try:
                    self.sess = self._make_gpu_session()
                    self.device = "cuda"
                    return
                except Exception as e:  # noqa: BLE001 - any GPU problem means CPU fallback
                    reason = "GPU initialization failed: " + (str(e).strip().splitlines() or ["unknown error"])[0][:300]
                    log.warning(reason)
        self.sess = self._make_cpu_session()
        self.device = "cpu"
        self.fallback_reason = reason

    def _make_cpu_session(self) -> ort.InferenceSession:
        so = ort.SessionOptions()
        so.log_severity_level = 3
        return ort.InferenceSession(str(self.model_path), so, providers=["CPUExecutionProvider"])

    def _make_gpu_session(self) -> ort.InferenceSession:
        if os.environ.get("KOKORO_SIMULATE_GPU_FAILURE"):
            raise RuntimeError("Simulated GPU failure (test mode, KOKORO_SIMULATE_GPU_FAILURE is set)")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.enable_profiling = True
        so.profile_file_prefix = str(LOG_DIR / "gpu_check")
        cuda = ("CUDAExecutionProvider", {"device_id": 0, "cudnn_conv_algo_search": "HEURISTIC"})
        sess = ort.InferenceSession(str(self.model_path), so, providers=[cuda, "CPUExecutionProvider"])
        sess.disable_fallback()  # a CUDA failure must surface, not silently switch to CPU
        if sess.get_providers()[0] != "CUDAExecutionProvider":
            raise RuntimeError("ONNX Runtime did not activate the CUDA provider")
        tokens = self._tokens_for(_TEST_TEXT, "en-us")
        voices = sorted(self.voices_dir.glob("*.bin"))
        test_voice = "af_heart" if (self.voices_dir / "af_heart.bin").exists() else voices[0].stem
        audio, _ = self._raw_run(sess, tokens, self._style(test_voice, len(tokens)), 1.0, None)
        profile = sess.end_profiling()
        try:
            share = _profile_share(profile)
        finally:
            try:
                os.remove(profile)
            except OSError:
                pass
        if not np.isfinite(audio).all() or float(np.abs(audio).max()) < 1e-4:
            raise RuntimeError("GPU produced invalid audio")
        if share < 0.5:
            raise RuntimeError(f"only {share:.0%} of the model ran on the GPU")
        t = time.perf_counter()
        audio, _ = self._raw_run(sess, tokens, self._style(test_voice, len(tokens)), 1.0, None)
        self.gpu_rtf = (time.perf_counter() - t) / (len(audio) / SAMPLE_RATE)
        self.gpu_share = share
        return sess

    def _fall_back_to_cpu(self, error: Exception) -> None:
        with self._switch_lock:
            if self.device == "cpu":
                return
            self.fallback_reason = "GPU inference failed, switched to CPU: " + str(error).strip().splitlines()[0][:300]
            log.warning(self.fallback_reason)
            self.sess = self._make_cpu_session()
            self.device = "cpu"
        if self.on_device_change:
            self.on_device_change(self)

    def device_label(self) -> str:
        if self.device == "cuda" and self.gpu:
            return f"GPU: {self.gpu.name} (CUDA)"
        return "CPU"

    def device_details(self) -> str:
        lines = [f"Active inference device: {self.device_label()}", f"ONNX Runtime {ort.__version__}"]
        if self.gpu and self.gpu.found:
            lines.append("GPU: " + self.gpu.summary())
        if self.device == "cuda":
            if self.gpu_share is not None:
                lines.append(f"Verified: {self.gpu_share:.0%} of model compute ran on CUDA in the startup test.")
            if self.gpu_rtf:
                lines.append(f"Speed: {1 / self.gpu_rtf:.0f}x faster than real time in the startup test.")
        if self.fallback_reason:
            lines.append("Reason for CPU: " + self.fallback_reason)
        return "\n".join(lines)

    # ---- voices and text --------------------------------------------------------------------
    def _voice(self, name: str) -> np.ndarray:
        if name not in self._voices:
            path = self.voices_dir / f"{name}.bin"
            self._voices[name] = np.fromfile(path, dtype=np.float32).reshape(-1, 1, 256)
        return self._voices[name]

    def _style(self, voice: str, n_tokens: int) -> np.ndarray:
        v = self._voice(voice)
        return v[max(1, min(n_tokens, len(v))) - 1]

    @staticmethod
    def lang_for(voice: str) -> str:
        return "en-gb" if voice.startswith("b") else "en-us"

    def _phonemize(self, texts: list[str], lang: str) -> list[str]:
        with _espeak_lock:
            out = phonemizer.phonemize(texts, language=lang, backend="espeak", preserve_punctuation=True,
                                       with_stress=True, njobs=1)
        if isinstance(out, str):
            out = [out]
        return ["".join(c for c in p if c in self.vocab).strip() for p in out]

    def _tokens_for(self, text: str, lang: str) -> list[int]:
        return [self.vocab[c] for c in self._phonemize([text], lang)[0]]

    # ---- inference ----------------------------------------------------------------------------
    def _raw_run(self, sess, tokens, style, speed, token: CancelToken | None):
        ro = ort.RunOptions()
        ro.log_severity_level = 3
        if token:
            token._add(ro)
        try:
            out = sess.run(None, {
                "input_ids": np.array([[0, *tokens, 0]], dtype=np.int64),
                "style": style.astype(np.float32),
                "speed": np.array([speed], dtype=np.float32),
            }, ro)
        except Exception:
            if token and token.cancelled:
                raise Cancelled() from None
            raise
        finally:
            if token:
                token._remove(ro)
        return np.asarray(out[0], dtype=np.float32).ravel(), np.asarray(out[1], dtype=np.float64).ravel()

    def _run(self, tokens, voice, speed, token):
        if token and token.cancelled:
            raise Cancelled()
        style = self._style(voice, len(tokens))
        try:
            return self._raw_run(self.sess, tokens, style, speed, token)
        except Cancelled:
            raise
        except Exception as e:
            if self.device != "cuda":
                raise
            self._fall_back_to_cpu(e)
            return self._raw_run(self.sess, tokens, style, speed, token)

    def synthesize(self, fragments: list[tuple[int, str]], voice: str, speed: float = 1.0,
                   pause_after: float = 0.3, token: CancelToken | None = None) -> Synth:
        """Speak a sequence of (line id, text) fragments as one utterance."""
        speed = float(min(2.0, max(0.5, speed)))
        frags = [(lid, t.strip()) for lid, t in fragments if t and t.strip()]
        if not frags:
            return Synth(np.zeros(int(pause_after * SAMPLE_RATE / speed), np.float32), [])
        phs = self._phonemize([t for _, t in frags], self.lang_for(voice))
        parts = [(lid, [self.vocab[c] for c in p]) for (lid, _), p in zip(frags, phs)]
        synth = self._synth_tokens([(lid, toks) for lid, toks in parts if toks], voice, speed, token)
        pad = np.zeros(int(pause_after * SAMPLE_RATE / speed), np.float32)
        if not synth.marks and frags:
            synth.marks = [(frags[0][0], 0)]
        return Synth(np.concatenate([synth.audio, pad]), synth.marks)

    def _synth_tokens(self, parts: list[tuple[int, list[int]]], voice, speed, token) -> Synth:
        if not parts:
            return Synth(np.zeros(0, np.float32), [])
        total = sum(len(t) for _, t in parts) + len(parts) - 1
        if total > MAX_TOKENS:
            if len(parts) == 1:  # one very long line: split its tokens at a word gap
                lid, toks = parts[0]
                cut = len(toks) // 2
                spaces = [i for i, t in enumerate(toks) if t == self._space]
                if spaces:
                    cut = min(spaces, key=lambda i: abs(i - cut))
                halves = [(lid, toks[:cut]), (lid, toks[cut + 1:] if toks[cut] == self._space else toks[cut:])]
            else:
                acc, cut = 0, 1
                for i, (_, toks) in enumerate(parts):
                    acc += len(toks) + 1
                    if acc >= total / 2:
                        cut = max(1, i)
                        break
                halves = [parts[:cut], parts[cut:]]
                halves = [h for h in halves if h]
            if len(parts) == 1:
                a = self._synth_tokens([halves[0]], voice, speed, token)
                b = self._synth_tokens([halves[1]], voice, speed, token)
            else:
                a = self._synth_tokens(halves[0], voice, speed, token)
                b = self._synth_tokens(halves[1], voice, speed, token)
            gap = np.zeros(int(0.06 * SAMPLE_RATE), np.float32)
            off = len(a.audio) + len(gap)
            return Synth(np.concatenate([a.audio, gap, b.audio]), a.marks + [(l, s + off) for l, s in b.marks])

        tokens: list[int] = []
        starts: list[tuple[int, int]] = []
        for lid, toks in parts:
            if tokens:
                tokens.append(self._space)
            starts.append((lid, len(tokens)))
            tokens.extend(toks)
        audio, dur = self._run(tokens, voice, speed, token)
        frames = np.concatenate([[0.0], np.cumsum(dur)])
        edges = np.round(frames * (len(audio) / frames[-1])).astype(np.int64) if frames[-1] > 0 else np.zeros_like(frames, dtype=np.int64)
        _, (head, tail) = trim_audio(audio)
        if tail <= head:  # nothing audible
            head, tail = 0, len(audio)
        head = max(0, int(head) - _LEAD)
        tail = min(len(audio), int(tail) + _LEAD)
        audio = audio[head:tail]
        marks = []
        for lid, tok_index in starts:
            s = int(edges[min(tok_index + 1, len(edges) - 1)]) - head
            s = max(0, min(len(audio) - 1, s))
            if marks and marks[-1][0] == lid:
                continue
            if marks and s < marks[-1][1]:
                s = marks[-1][1]
            marks.append((lid, s))
        if marks:
            marks[0] = (marks[0][0], 0)
        return Synth(audio, marks)
