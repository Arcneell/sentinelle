"""Rend le paquet du dépôt importable pendant les tests (racine sur sys.path)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
