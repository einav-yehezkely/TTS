Build a working Windows desktop application that reads English PDF books and academic articles aloud using Kokoro-82M locally.

I have an NVIDIA GPU. Detect its model, available VRAM, and driver version, and configure compatible GPU acceleration. I am not a technical user, so handle the setup and provide a simple interface and double-click launcher.

Implement the application, run it, and test it. Do not stop at a plan.

## Core requirements

- Open PDF files from my computer.
- Display the original PDF with its page layout preserved.
- Extract text in reading order and read it aloud using Kokoro.
- Highlight the exact line currently being read directly on the original PDF page.
- Automatically scroll and change pages to keep the highlighted line visible.
- Let me click a line in the PDF to start reading from that position.
- Provide voice selection and adjustable speech speed.
- Provide Play, Pause, Resume, Stop, Previous Sentence, and Next Sentence controls.
- Remember the last reading position for each document.
- Export the full document or a selected page range to WAV. Add MP3 export if practical.

## PDF highlighting and synchronization

Highlighting inside the original PDF is essential. A separate text panel alone does not satisfy this requirement.

- Preserve page numbers and text bounding boxes during extraction.
- Maintain a mapping between narration segments and the corresponding PDF lines and coordinates.
- If a spoken sentence spans multiple lines, move the highlight between those lines as the audio progresses.
- Synchronize highlighting to actual audio playback, including pauses, seeking, speed changes, and page transitions.
- Use speech timing or alignment when available. Otherwise, implement approximate line timing and clearly document its accuracy.
- Keep highlights aligned when the PDF is zoomed, resized, or scrolled.
- Avoid highlighting an entire page or paragraph when only one line is being read.
- Make automatic following optional so I can browse other pages while listening.

## Books and academic articles

- Support long PDFs without loading the entire generated audiobook into memory.
- Handle single-column and two-column layouts, preserving the correct reading order.
- Avoid narrating repeated page headers, footers, and page numbers where reliably detectable.
- Handle words hyphenated across line breaks.
- Provide options to skip references and footnotes.
- Allow me to preview and correct the extracted reading order or exclude text regions before narration.
- Keep narration and PDF highlighting mapped correctly after text cleanup.
- Do not claim reliable interpretation of equations, tables, or diagrams. Let me skip those regions.
- Detect scanned PDFs with no usable text layer. Show a clear “OCR required” message rather than failing silently. OCR can be a later feature.

## Local speech generation and NVIDIA GPU

- Use Kokoro-82M locally with no paid API, subscription, account requirement, or usage quota.
- Prefer Kokoro ONNX with ONNX Runtime CUDA if compatible with this computer.
- Verify current official documentation and package compatibility before selecting dependency versions.
- Check NVIDIA driver, CUDA, cuDNN, and ONNX Runtime compatibility.
- Prefer project-local dependencies and runtime libraries over system-wide changes.
- Do not replace NVIDIA drivers or modify system-wide CUDA installations without explaining why and asking first.
- Verify that actual speech inference uses the GPU; merely detecting CUDA is insufficient.
- Display the active inference device in the interface.
- Fall back to CPU automatically if GPU initialization or inference fails, with a readable explanation.
- Download required models and voice files during initial setup, with progress indicators and retry support.
- After initial setup, document reading and narration must work offline.

## Performance and playback

- Generate speech incrementally and start playback before the whole document is converted.
- Prefetch upcoming narration segments while the current segment plays.
- Keep the interface responsive during extraction, model downloads, speech generation, and export.
- Cache generated audio locally with a reasonable storage limit and a clear-cache option.
- Make cancellation reliable.
- Keep playback and highlighting synchronized when buffered audio ends or generation is slower than playback.

## Interface and setup

- Use a clean, straightforward desktop interface with the PDF viewer as the main area.
- Choose a maintained framework and PDF renderer that support accurate coordinate overlays.
- Inspect the environment first, then choose a practical stack. Do not assume required dependencies are installed.
- Create an isolated project environment and pin compatible dependency versions.
- Provide a double-click launcher that handles startup and shows understandable errors.
- Provide brief installation and usage instructions in Hebrew.
- Keep documents and generated audio local.
- Clearly distinguish downloading models from normal offline operation.

## Implementation workflow

Build and verify a small end-to-end version first:
1. Open and display a PDF.
2. Extract text with page and line coordinates.
3. Generate and play Kokoro audio.
4. Highlight the current line inside the PDF during playback.
5. Verify NVIDIA GPU inference and CPU fallback.

Then add navigation, saved reading positions, text cleanup, caching, and audio export.

Test with both a normal book PDF and a two-column academic article. Ask me for sample documents if necessary, and continue independent setup work while waiting.

Verify:
- PDF loading and reading order.
- Highlight placement at different zoom levels.
- Highlight timing during playback, pause, resume, and sentence navigation.
- Automatic page changes.
- Long-document streaming and cancellation.
- GPU inference and CPU fallback.
- WAV export.
- Offline operation after setup.

At completion, launch the application, explain how I open it next time, and report what was actually tested, what failed, and any remaining limitations. Do not describe untested functionality as working.