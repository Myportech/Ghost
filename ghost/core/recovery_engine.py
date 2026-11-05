"""Récupération : changer de stratégie, ne pas répéter une action externe incertaine."""

from __future__ import annotations

from dataclasses import dataclass

from ghost.core.results import (
    ACTION_FAILED,
    ACTION_INDETERMINATE,
    ACTION_UNVERIFIED,
    ACTION_VERIFIED,
    PROCESS_INTERRUPTED,
    PROCESS_TIMEOUT,
    ExecutionResult,
)

CATEGORIES = (
    "syntax",
    "permission_denied",
    "accessibility_permission",
    "app_not_responding",
    "window_not_found",
    "ui_element_not_found",
    "invalid_applescript_property",
    "authentication",
    "api_rate_limit",
    "network_timeout",
    "process_timeout",
    "process_interrupted",
    "action_failed",
    "verification_failed",
    "unknown_result",
)

_EXTERNAL_TOOLS = {"outlook.send_email"}
_READ_TOOLS = {
    "outlook.search_emails",
    "outlook.read_email",
    "outlook.search_sent_items",
    "outlook.verify_sent_email",
    "finder.list",
    "calendar.verify_event",
}


@dataclass
class RecoveryDecision:
    category: str
    retry: bool
    switch_strategy: bool
    stop: bool
    message: str
    same_count: int = 0


class RecoveryEngine:
    def __init__(self) -> None:
        self.history: list = []
        self.exhausted = False
        self.hint = ""
        self.last_category = ""
        self.last_tool = ""

    def classify(self, result: ExecutionResult) -> str:
        explicit = str((result.evidence or {}).get("category") or "")
        if explicit in CATEGORIES:
            return explicit
        if result.interrupted or result.process_status == PROCESS_INTERRUPTED:
            return "process_interrupted"
        if result.process_status == PROCESS_TIMEOUT:
            return "process_timeout"
        blob = " ".join(
            part for part in (result.error, result.stderr, result.stdout) if part
        ).lower()
        checks = (
            ("accessibility_permission", ("assistive", "accessibility", "axerror", "1002")),
            ("permission_denied", ("operation not permitted", "not authorized", "permission denied", "-1743")),
            ("authentication", ("aadsts", "invalid_grant", "unauthorized", "authentication")),
            ("api_rate_limit", ("rate limit", "too many requests", "429")),
            ("network_timeout", ("connecttimeout", "name or service not known", "network is unreachable")),
            ("window_not_found", ("window not found", "fenêtre introuvable", "fenetre introuvable")),
            ("ui_element_not_found", ("élément introuvable", "element introuvable", "champ introuvable", "ui element")),
            ("invalid_applescript_property", ("ne comprend pas", "nom de classe", "-2741", "ghost_field_mismatch", "can't get", "can’t get")),
            ("syntax", ("syntax error", "syntaxe")),
            ("app_not_responding", ("not running", "n'est pas lancé", "introuvable")),
            ("verification_failed", ("non conforme", "verification_failed")),
        )
        for name, needles in checks:
            if any(needle in blob for needle in needles):
                return name
        if result.permission_denied:
            return "permission_denied"
        if result.action_status in {ACTION_VERIFIED, "ACTION_EXECUTED"} and result.execution_success:
            return ""
        if result.action_status == ACTION_UNVERIFIED and result.execution_success:
            return ""
        if result.action_status == ACTION_INDETERMINATE:
            return "unknown_result"
        if result.action_status == ACTION_FAILED or result.process_status == "PROCESS_FAILED":
            return "action_failed"
        return "unknown_result"

    def observe(self, result: ExecutionResult, tool: str, *, external: bool = False) -> RecoveryDecision:
        category = self.classify(result)
        if not category:
            return RecoveryDecision("", False, False, False, "", 0)
        self.history.append({"category": category, "tool": tool})
        self.last_category = category
        self.last_tool = tool
        same = [item for item in self.history if item["category"] == category and item["tool"] == tool]
        switch = len(same) >= 2
        uncertain = result.action_status == ACTION_INDETERMINATE or category == "unknown_result"
        stop = False
        retry = True
        if external and (uncertain or tool in _EXTERNAL_TOOLS and result.action_status == ACTION_INDETERMINATE):
            retry = False
            stop = True
            self.exhausted = True
        elif category in {"api_rate_limit"}:
            retry = False
        elif tool in _READ_TOOLS:
            retry = category not in {"permission_denied", "authentication"}
        elif external and category in {"authentication", "permission_denied", "invalid_applescript_property", "syntax", "action_failed"}:
            retry = not switch
        if switch and external:
            retry = False
            self.hint = "Deux erreurs de même nature. Change de stratégie."
        if stop:
            self.hint = "Résultat externe incertain. Aucune répétition automatique."
        message = self.hint or category
        if switch and not self.hint:
            message = "Deux erreurs de même nature. Change de stratégie."
            self.hint = message
        if self.exhausted and external:
            stop = True
        return RecoveryDecision(category, retry, switch, stop, message, len(same))

    def must_switch(self, tool: str, category: str) -> bool:
        same = [item for item in self.history if item["category"] == category and item["tool"] == tool]
        return len(same) >= 2
