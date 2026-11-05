"""Journal d'opérations externes, sans corps de message ni jeton."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from ghost.core.results import ACTION_INDETERMINATE, ACTION_VERIFIED, REQUEST_ACCEPTED, SENT_ITEM_FOUND

BLOCKING = {
    REQUEST_ACCEPTED,
    SENT_ITEM_FOUND,
    "VERIFIED",
    "INDETERMINATE",
    "TIMEOUT_AFTER_SEND",
    ACTION_VERIFIED,
    ACTION_INDETERMINATE,
}


class OperationJournal:
    def __init__(self, path: Optional[Path] = None, ttl: float = 1800) -> None:
        self.path = path
        self.ttl = ttl
        self.entries: list = []
        if path is not None and path.is_file():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = []
        if isinstance(data, list):
            self.entries = [item for item in data if isinstance(item, dict) and "fingerprint" in item]

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.entries[-200:], ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def record(self, fingerprint: str, status: str, connector: str, *, now: Optional[float] = None) -> str:
        op_id = uuid.uuid4().hex[:12]
        self.entries.append(
            {
                "id": op_id,
                "fingerprint": fingerprint,
                "status": status,
                "connector": connector[:40],
                "at": now if now is not None else time.time(),
            }
        )
        self._save()
        return op_id

    def has_blocking(self, fingerprint: str, *, now: Optional[float] = None) -> bool:
        moment = time.time() if now is None else now
        for entry in self.entries:
            if entry.get("fingerprint") != fingerprint:
                continue
            if moment - float(entry.get("at") or 0) > self.ttl:
                continue
            if entry.get("status") in BLOCKING:
                return True
        return False
