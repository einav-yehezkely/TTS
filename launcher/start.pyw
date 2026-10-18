"""Started by the launcher with pythonw (no console). Shows a readable message if startup fails."""

import ctypes
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def fail(text: str) -> None:
    log = ROOT / "data" / "logs" / "startup_error.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text, encoding="utf-8")
    except OSError:
        pass
    ctypes.windll.user32.MessageBoxW(
        None,
        "Kokoro PDF Reader could not start.\n\n"
        f"{text.strip().splitlines()[-1]}\n\n"
        "Try double-clicking the launcher again. If the problem stays, delete the '.venv' folder inside the "
        "application folder and start the launcher again: it will reinstall the libraries.\n\n"
        f"Details were saved in:\n{log}",
        "Kokoro PDF Reader", 0x10)


try:
    from kokoro_reader.app import main
except Exception:  # noqa: BLE001
    fail(traceback.format_exc())
    sys.exit(1)

try:
    sys.exit(main())
except SystemExit:
    raise
except Exception:  # noqa: BLE001
    fail(traceback.format_exc())
    sys.exit(1)
