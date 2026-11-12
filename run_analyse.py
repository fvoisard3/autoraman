"""Outil d'analyse des acquisitions (cartes et graphiques), indépendant de l'acquisition.

    python run_analyse.py                      # puis « Ouvrir… »
    python run_analyse.py data/runs/<date>/<fichier>.h5

Aucun appareil n'est utilisé : on peut l'ouvrir pendant une acquisition et relire le fichier en cours
avec « Recharger ».
"""
import argparse
import os
import sys

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")  # PyQt5 est aussi installé dans l'environnement

from PySide6.QtWidgets import QApplication  # noqa: E402

from raman_auto.analysis.viewer import AnalysisWindow  # noqa: E402
from raman_auto.config import load_config  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("fichier", nargs="?", help="fichier d'exécution HDF5")
    args = p.parse_args()
    app = QApplication(sys.argv)
    window = AnalysisWindow(load_config(), args.fichier)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
