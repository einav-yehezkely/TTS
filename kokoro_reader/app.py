"""Application entry point."""

import logging
import logging.handlers
import sys
import traceback

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from . import assets
from .docstate import Settings
from .paths import LOG_DIR, ROOT, ensure_dirs


def _setup_logging() -> None:
    ensure_dirs()
    handler = logging.handlers.RotatingFileHandler(LOG_DIR / "app.log", maxBytes=2_000_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _excepthook(kind, value, tb) -> None:
    text = "".join(traceback.format_exception(kind, value, tb))
    logging.getLogger("crash").error(text)
    app = QApplication.instance()
    if app is not None:
        box = QMessageBox(QMessageBox.Icon.Critical, "Unexpected error",
                          "Something went wrong. The application will try to continue.\n\n"
                          f"{value}\n\nA log was saved in: {LOG_DIR / 'app.log'}")
        box.setDetailedText(text)
        box.exec()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    _setup_logging()
    sys.excepthook = _excepthook
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName("Kokoro PDF Reader")
    app.setFont(QFont("Segoe UI", 10))
    icon = ROOT / "launcher" / "app.ico"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    from .ui import theme
    settings = Settings.load()
    theme.apply(app, settings.theme)

    if assets.missing_assets():
        from .ui.dialogs import SetupDialog
        if not SetupDialog().exec() or assets.missing_assets():
            QMessageBox.information(None, "Setup not finished",
                                    "The speech files are not downloaded yet, so the application cannot read "
                                    "aloud. Start it again when you are connected to the internet.")
            return 1

    from .ui.main_window import MainWindow
    win = MainWindow(settings)
    win.show()
    pdfs = [a for a in argv[1:] if a.lower().endswith(".pdf")]
    if pdfs:
        win.open_pdf(pdfs[0])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
