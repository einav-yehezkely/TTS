"""First-run experience in a fresh copy: the setup dialog downloads the model and voices.

Usage (with the fresh copy's own Python): <copy>\\.venv\\Scripts\\python.exe tests\\setup_dialog_check.py <copy>
"""

import sys
import time
from pathlib import Path

copy = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(copy))
from PySide6.QtWidgets import QApplication  # noqa: E402

from kokoro_reader import assets  # noqa: E402
from kokoro_reader.ui.dialogs import SetupDialog  # noqa: E402

assert assets.MODEL_FILE.is_relative_to(copy), assets.MODEL_FILE
app = QApplication([])
before = len(assets.missing_assets())
dlg = SetupDialog()
dlg.show()
texts = []
t0 = time.monotonic()
dlg.start_btn.click()
while dlg.isVisible() and time.monotonic() - t0 < 900:
    app.processEvents()
    if not texts or texts[-1] != dlg.status.text():
        texts.append(dlg.status.text())
    time.sleep(0.05)
after = assets.missing_assets()
print(f"missing before: {before}, after: {len(after)}; took {time.monotonic() - t0:.0f}s; result={dlg.result()}")
print("progress messages seen:", len(texts), "| first:", texts[1:2], "| last:", texts[-1:])
ok = not after and dlg.result() == 1 and len(texts) > 5
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
