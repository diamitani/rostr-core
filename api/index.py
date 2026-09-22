"""Vercel Python entry. Same Handler as server.py — ROSTR as a harness API."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import Handler as handler  # noqa: E402, F401
