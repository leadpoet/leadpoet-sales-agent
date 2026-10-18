"""Registrable-domain split, ported from leadpoet_verifier/identity/normalization.py.

The scorer's company locator (candidate_company_prompt_identity) is the
public-suffix-aware registrable domain: example.co.uk, brand.com.au -- not the
last two labels. public_suffix_list.dat is the platform's pinned snapshot
(Mozilla Public Suffix List, MPL-2.0), verified by the same SHA-256.
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from typing import Optional

PSL_SNAPSHOT_SHA256 = "94c6c88fed2babe6b30b9118821487f7fdb354740b6a2c991951c9da90690335"


def _idna_encode(label: str) -> str:
    try:
        import idna

        return idna.encode(label, uts46=True, transitional=False, std3_rules=True).decode("ascii")
    except Exception:
        return label.encode("idna").decode("ascii") if not label.isascii() else label


class PublicSuffixSnapshot:
    def __init__(self, text: str) -> None:
        self.exact: dict[str, bool] = {}
        self.wildcards: dict[str, bool] = {}
        self.exceptions: dict[str, bool] = {}
        private = False
        for raw in text.splitlines():
            line = raw.strip()
            if line == "// ===BEGIN PRIVATE DOMAINS===":
                private = True
                continue
            if not line or line.startswith("//"):
                continue
            rule = line.lstrip("!*. ")
            normalized = rule.lower() if rule.isascii() else ".".join(_idna_encode(l) for l in rule.split(".")).lower()
            if line.startswith("!"):
                self.exceptions[normalized] = private
            elif line.startswith("*."):
                self.wildcards[normalized] = private
            else:
                self.exact[normalized] = private

    def split(self, ascii_host: str) -> tuple[str, str, bool]:
        labels = ascii_host.split(".")
        exception: Optional[tuple[int, bool]] = None
        matches: list[tuple[int, bool]] = []
        for index in range(len(labels)):
            candidate = ".".join(labels[index:])
            if candidate in self.exceptions:
                exception = (len(labels) - index - 1, self.exceptions[candidate])
                break
            if candidate in self.exact:
                matches.append((len(labels) - index, self.exact[candidate]))
            if index + 1 < len(labels):
                wildcard_base = ".".join(labels[index + 1:])
                if wildcard_base in self.wildcards:
                    matches.append((len(labels) - index, self.wildcards[wildcard_base]))
        suffix_labels, private = exception or max(matches, default=(1, False), key=lambda item: item[0])
        suffix = ".".join(labels[-suffix_labels:])
        if len(labels) <= suffix_labels:
            raise ValueError("host is a public or private suffix, not a company domain")
        registrable = ".".join(labels[-(suffix_labels + 1):])
        return suffix, registrable, private


@lru_cache(maxsize=1)
def snapshot() -> PublicSuffixSnapshot:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_suffix_list.dat")
    with open(path, "rb") as handle:
        payload = handle.read()
    if hashlib.sha256(payload).hexdigest() != PSL_SNAPSHOT_SHA256:
        raise RuntimeError("vendored Public Suffix List digest mismatch")
    return PublicSuffixSnapshot(payload.decode("utf-8"))


def registrable_domain(host: str) -> str:
    """example.co.uk for www.sub.example.co.uk; '' when the host is not a company domain."""

    host = str(host or "").strip().rstrip(".").lower()
    if not host or "." not in host:
        return ""
    ascii_host = ".".join(_idna_encode(label) for label in host.split(".")).lower()
    try:
        return snapshot().split(ascii_host)[1]
    except ValueError:
        return ""
