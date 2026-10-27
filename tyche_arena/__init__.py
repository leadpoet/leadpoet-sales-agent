"""Leadpoet Arena host for TYCHE's shared research implementation."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents/skills/lead-sourcing"
for _path in (str(SKILL / "scripts"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
