"""Open an Ableton Live set and edit its MIDI mappings.

Run with: py -3 main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ALS MIDI Remapper")
    window = MainWindow()
    if len(sys.argv) > 1:
        window.load_path(Path(sys.argv[1]))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
