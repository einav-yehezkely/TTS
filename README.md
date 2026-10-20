# Kokoro PDF Reader

A Windows desktop app that reads English PDF books and papers aloud with Kokoro-82M, running locally.
While it reads, it highlights the current line on the original PDF page.
User instructions (Hebrew): [README_HE.md](README_HE.md). To start, double-click `Kokoro PDF Reader.bat`.

## Stack and why

| Part | Choice | Reason |
|---|---|---|
| UI | PySide6 6.12 (Qt) `QGraphicsView` | Maintained. Scene units are PDF points, so overlays stay exact under any zoom, resize or scroll. |
| PDF | PyMuPDF 1.28 | Fast rendering plus line, span and bbox extraction. |
| Speech | Kokoro-82M v1.0, [timestamped ONNX export](https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX-timestamped) | Outputs per-phoneme durations alongside the audio. These drive the line timing. |
| G2P | kokoro-onnx 0.6.1 tokenizer + espeak-ng (bundled) | Same phonemization Kokoro-ONNX uses. Works offline. |
| Runtime | onnxruntime-gpu **1.26.0** + nvidia-*-cu12 wheels (CUDA 12.9 runtime, cuDNN 9.27) | ORT ≥ 1.27 wheels are built for CUDA 13, which needs driver ≥ 580. This machine has driver 577.05 (CUDA ≤ 12.9). 1.26.0 is the newest ORT built for CUDA 12.8. Its DLLs live in the venv, so there is no system CUDA install and no driver change. |
| Audio | sounddevice (PortAudio), soundfile (FLAC cache, WAV), lameenc (MP3) | |
| Video | PyAV 19.0.1 (FFmpeg 8 in the wheel): `h264_nvenc`, `libx264`, native AAC | In-process encoding with millisecond frame timestamps. The wheel already contains NVENC and x264, so no system FFmpeg is needed. |
| Env | uv, Python 3.12 installed under `.runtime/`, pinned `uv.lock` | Project-local. |

Verified on: RTX 5050 Laptop GPU (Blackwell, compute capability 12.0, 8 GB), driver 577.05, Windows 11.

## How highlighting is synchronized

1. **Extraction** (`layout.py`) keeps every visual line with its page and bbox. Reading order comes from gutter detection. Full-width lines split the page into horizontal bands, and each band is read left column, then right column.
2. **Segmentation** (`segments.py`) builds sentences out of *fragments*, each tied to one line id. De-hyphenation moves the continuation to the line where the word starts. Citation removal and abbreviation expansion happen inside fragments, or on the joined text with offset remapping, so the line mapping always survives cleanup.
3. **Synthesis** (`engine.py`) phonemizes each fragment separately in a single espeak call, joins the tokens, runs the model once per sentence, and uses the returned per-token durations to find the sample where each line starts.
4. **Playback** (`player.py`) maps the device frame being *heard* (from PortAudio DAC timestamps, or the stream latency when those are missing) back to (sentence, sample), and from there to a line. Pauses, underruns, seeks and page transitions all go through this one mapping.

**Accuracy.** Line boundaries come from the model's own duration predictions, which are the durations the decoder uses to generate the audio. They are accurate to roughly one 25 ms frame. The on-screen change happens within about 0.1 s of the sound (30 ms UI poll plus the output-latency estimate). In testing, a line change was observed 115 ms after its data time, which matches the measured ~90 ms device latency. There is no word-level highlighting.

## Video export (MP4)

Export → *MP4 video* writes the original pages with the line being read highlighted, plus the narration.

* **Views.** *Full page* shows each page, fitted to the frame. The size is the most common page shape in the range, capped by the H.264 level (≤1920×1080 pixels in total). The two *Zoomed* views (portrait 1080×1920, landscape 1920×1080) fit the current text column to the frame width. In a two-column paper that means one column. Both columns of a page use the same zoom, and the document's usual column width keeps the zoom steady from page to page. The camera holds still while the line stays comfortably in view. Otherwise it glides (0.32 s) so the line sits near the top quarter, and it cuts at a page or column change. On the test PDFs, 10 pt body text is 42–45 px per em at 1080p in the zoomed views. In full page at 720p it is 14 px per em, which is too small for a phone.
* **Sync.** Each sentence's audio and per-line sample marks are the same cached data that drives the live highlight. A frame is emitted at each line start, with a millisecond timestamp. In the test runs, every line switch landed within 0.5 ms of its audio sample.
* **Variable frame rate.** Frames are only encoded when the picture changes: a new line, a camera glide, or a repeat at least once a second for seeking. Page changes are forced keyframes, and there is a keyframe at least every 10 frames. There are no B-frames: with frames seconds apart, reordering needs composition offsets of seconds, and Windows Media Foundation decoded the wrong frame when x264 used them. A 79-minute video is about 5,700 frames and 224 MB.
* **Format.** H.264 High (level 4.0 at 1080p, 3.1 at 720p), yuv420p, BT.709 tags, AAC-LC 96 kbps mono 48 kHz, MP4 with the index first (`faststart`).
* **Encoder.** NVENC is tried with a one-frame test encode. If it is unavailable or fails, x264 runs on the CPU. If NVENC fails part-way, the export restarts on x264, and by then the narration is cached. The finished message names the encoder.
* **Process model.** `video.py` (GUI process) gets each sentence from the cache, or synthesizes and caches it, and streams it through a pipe to `videoworker.py`, a separate process that renders with PyMuPDF and encodes with PyAV. Only one sentence is in flight, so memory is flat: the worker peaked at 345 MB on a 79-minute export, the same as on a 5-minute one. Rendering never holds the GUI's interpreter lock. Cancel kills the worker and deletes the `.part` file. Worker errors go to `data/logs/video_export.log`.

## Interface

* **Transcript panel** (`ui/transcript.py`): every narrated sentence in one `QTextDocument`, grouped by page.
  * The current sentence and the line being heard inside it are marked with extra selections, so the 5,000-sentence book never re-lays-out while playing.
  * It follows the reading with animated scrolling. Scrolling it by hand suspends following and shows "Back to current".
  * Clicking any part of a sentence plays from that fragment's PDF line.
  * Search is Ctrl+F. Ctrl+T collapses the panel to a rail with a slide animation.
* **Player bar** (`ui/widgets.py`):
  * The play button shows a spinner while speech is being prepared.
  * A live caption shows the sentence, with the line being heard marked.
  * A whole-document scrubber with page ticks shows a hover preview (page and text) and seeks on click or drag.
  * It shows page, percent and time left. Time left uses the speaking rate measured from the synthesized audio.
  * Voice and speed chips, and a sleep timer (minutes or end of page).
* **Focus mode:** a dimming layer with a cut-out around the current line, in PDF coordinates, so it stays aligned at every zoom.
* **Other:** smooth animated page-following, toast notifications, custom vector icons, and light/dark themes from one set of color tokens (`ui/theme.py`).

## GPU verification and fallback

At startup the first inference runs with ONNX Runtime profiling enabled. The app counts per-node kernel time by provider and requires at least half of it to be on CUDA (measured: 89–94%). CPU fallback is disabled inside ORT (`disable_fallback`), so failures surface instead of silently running on the CPU. Then:
* If GPU initialization fails, the app uses the CPU and explains why in a dialog and in the status bar.
* If a GPU run fails later, the app switches to the CPU and retries that sentence.

One real case this caught: cuDNN 9 loads its sub-libraries (for example `cudnn_engines_tensor_ir64_9.dll`) through the normal DLL search path. Without the venv's `nvidia/*/bin` folders on `PATH`, ORT silently fell back to CPU even though "CUDA was available". `gpu.prepare_cuda_dlls()` fixes this.

## Tests (run with `.venv\Scripts\python.exe`)

| Script | Checks |
|---|---|
| `tests/unit_checks.py` | Known-answer layout tests (header, footer, page numbers, footnote, hyphenation, two columns, references). Scanned-PDF detection. Edits keep the mapping. Runtime GPU→CPU fallback. Cancellation. Runs with **network sockets blocked**. |
| `tests/pipeline_check.py` | Real audio: play, pause, resume, next/previous, speed change, stop. Line-change timing compared against the model's timing. |
| `tests/video_check.py [dir] [--long]` | MP4 export, decoded and measured against the narration timing: codec, level, no reordering, faststart, A/V lengths, AAC vs. narration samples (cross-correlation), the highlighted line in every frame, line-switch times, page changes on keyframes, text size. A second decoder (Windows Media Foundation, `tests/mf_decode_check.ps1`) must read the file, show the same highlight and transcode it. Also NVENC→x264 fallback, cancellation, and with `--long` a 40-page export. |
| `tests/gui_video_check.py [pdf] [dir]` | MP4 export from the real window: dialog choices, progress window, event-loop stalls during export, narration playing at the same time, Cancel. |
| `tests/gui_check.py [pdf]` | Real window: click-to-read, ink-based highlight placement at 70/100/160/250% zoom and after resize, line-to-line movement, automatic page change, follow on/off, pause, edit mode, WAV and MP3 export. |
| `tests/gui_ui_check.py [pdf]` | Transcript follows the sentence and line, click-to-read from a transcript line, Back to current, search, collapse/expand, focus mode (pixel brightness), scrubber seek, time left, sleep timer, theme switch. |
| `tests/gui_fallback_check.py` | Whole app on CPU (simulated GPU failure), the explanation shown, position restored after reopening, OCR-required message. |
| `tests/longdoc_check.py [pdf] [s]` | 244-page book from page 196: time to first audio, bounded prefetch, underruns, memory, stop. `KOKORO_FORCE_CPU=1` runs it on the CPU. |
| `tests/download_check.py`, `tests/setup_dialog_check.py` | Resumable downloads and the first-run download dialog. |

Developer tools: `tools/dump_layout.py`, `tools/dump_segments.py`.

Environment switches: `KOKORO_FORCE_CPU=1`, `KOKORO_SIMULATE_GPU_FAILURE=1` and `KOKORO_SIMULATE_NVENC_FAILURE=1`.

## Folders

* `models/`: model and voices, downloaded once.
* `data/`: settings, per-document state (position, edits), extraction cache, audio cache, logs.
* `.venv/` and `.runtime/`: the private environment, created by `launcher/setup.ps1`.

## Known limitations

* Layout roles (headers, footnotes, references, equations, figure text, captions) are heuristics. Equations, tables and diagrams are not interpreted. Lines full of symbols are skipped by default, and Edit reading order lets you exclude regions by hand.
* De-hyphenation drops every line-end hyphen that is followed by a lowercase letter, so a real compound split at its hyphen ("high-/level") is read as one word.
* No OCR: scanned PDFs are detected and reported but can't be read.
* Pausing drops the audio buffered in the device and resumes from the exact sample that was heard. The first ~0.1 s after Play is latency.
* The process uses about 1.3–1.4 GB of RAM on the GPU path (CUDA and cuDNN libraries). This grows by a few tens of MB as ONNX Runtime caches new input shapes, then levels off.
