"""Lance l'interface de contrôle Raman.

    python run_gui.py          # appareils réels (fermer LightField avant de connecter le spectromètre)
    python run_gui.py --sim    # simulation, sans matériel
"""
import argparse
import os
import sys

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")  # PyQt5 est aussi installé dans l'environnement

from PySide6.QtWidgets import QApplication  # noqa: E402

from raman_auto.config import load_config  # noqa: E402
from raman_auto.gui.main_window import MainWindow  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sim", action="store_true", help="utiliser les simulateurs")
    args = p.parse_args()
    app = QApplication(sys.argv)
    window = MainWindow(load_config(), simulate=args.sim)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
