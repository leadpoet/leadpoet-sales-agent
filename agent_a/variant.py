
from __future__ import annotations

from typing import Sequence, TypeVar

try:
    from .arena_tools import STRATEGY
except ImportError:
    import importlib as _importlib
    import os as _os

    STRATEGY = _importlib.import_module(
        f"{_os.path.basename(_os.path.dirname(_os.path.abspath(__file__)))}.arena_tools").STRATEGY

T = TypeVar("T")
MAX_VARIANT = 7


def _knob(key: str, default: int, high: int) -> int:
    value = STRATEGY.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = default
    return max(0, min(high, int(value)))


VARIANT = _knob("ticket_variant", 0, MAX_VARIANT)


def rotate(items: Sequence[T], step: int, variant: int | None = None) -> list[T]:

    out = list(items)
    v = VARIANT if variant is None else max(0, min(MAX_VARIANT, int(variant)))
    if v <= 0 or len(out) < 2 or step <= 0:
        return out
    k = (v * step) % len(out)
    return out[k:] + out[:k]


__all__ = ["VARIANT", "MAX_VARIANT", "rotate"]
