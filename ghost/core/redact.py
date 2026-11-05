"""Masque les secrets avant tout journal, prompt ou notification."""

from __future__ import annotations

import re

_PATTERNS = (
    re.compile(r"gsk_[A-Za-z0-9]+"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-+/=]+", re.I),
    re.compile(
        r"(?i)(access_token|refresh_token|client_secret|id_token|password|api_key)"
        r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
    ),
)


def redact(text: str) -> str:
    raw = "" if text is None else str(text)
    raw = _PATTERNS[0].sub("[clé]", raw)
    raw = _PATTERNS[1].sub("Bearer [token]", raw)

    def _swap(match: re.Match) -> str:
        return match.group(1) + match.group(2) + "[secret]"

    return _PATTERNS[2].sub(_swap, raw)
