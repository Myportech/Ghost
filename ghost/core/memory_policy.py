"""Mémoire : une réussite n'est enregistrée que si Python l'a vérifiée."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

from ghost.core.redact import redact

_SUCCESS = re.compile(
    r"\b("
    r"réussi|reussi|succès|succes|envoyé|envoyée|envoyes|a envoyé|"
    r"tâche terminée|tache terminee|mail envoyé|livré|delivered|successfully"
    r")\b",
    re.I,
)


def fact_allowed(fact: str, verified: bool) -> bool:
    if verified:
        return True
    return not bool(_SUCCESS.search(fact or ""))


def reliability_entry(reliability: Optional[dict]) -> Optional[dict]:
    if not isinstance(reliability, dict) or not reliability:
        return None
    goal = str(reliability.get("goal") or "")
    return {
        "goal_hash": hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16] if goal else "",
        "connector": str(reliability.get("connector") or "")[:40],
        "tool": str(reliability.get("tool") or "")[:60],
        "status": str(reliability.get("status") or "")[:40],
        "verified": bool(reliability.get("verified")),
        "error_category": str(reliability.get("error_category") or "")[:40],
        "strategy": str(reliability.get("strategy") or "")[:40],
    }


def summary_for_memory(outcome: str, reliability: Optional[dict]) -> str:
    if isinstance(reliability, dict) and reliability:
        entry = reliability_entry(reliability) or {}
        return json.dumps(entry, ensure_ascii=False)
    return redact(outcome or "")[:1500]


def harvest_text(outcome: str, reliability: Optional[dict]) -> str:
    cleaned = redact(outcome or "")
    if not reliability:
        return cleaned
    kept = []
    for line in cleaned.splitlines():
        if "🛠️" in line or line.strip().startswith("/"):
            kept.append(line)
    return "\n".join(kept)
