"""Boucle de contrôle : Python décide si une tâche peut être déclarée terminée."""

from __future__ import annotations

import os
import re
import shlex
import time

from ghost.connectors.registry import build_registry
from ghost.core.prompt import system_prompt
from ghost.core.protocol import SHELL_TOOLS, Action, parse_action, public_args
from ghost.core.recovery_engine import RecoveryEngine
from ghost.core.results import (
    ACTION_EXECUTED,
    ACTION_FAILED,
    ACTION_INDETERMINATE,
    ACTION_UNVERIFIED,
    ACTION_VERIFIED,
)
from ghost.core.status_text import final_text
from ghost.core.task_manager import DoneDecision, TaskState, authorize_done, strategy_remaining
from ghost.core.verification_engine import (
    VerificationResult,
    verify_app_running,
    verify_file_created,
    verify_file_moved,
    verify_sent_email,
)

_SEND_HINT = re.compile(r"\bsend\b|keystroke", re.I)


class ControlSession:
    def __init__(self, goal: str, *, registry=None, email_checker=None, app_probe=None) -> None:
        self.goal = goal
        self.state = TaskState(goal)
        self.recovery = RecoveryEngine()
        self.registry = registry
        self.email_checker = email_checker
        self.app_probe = app_probe
        self.seen = set()
        self.last_connector = ""
        self.started = time.time()

    def system_prompt(self) -> str:
        return system_prompt()

    def snapshot(self) -> dict:
        return self.state.snapshot()

    def parse(self, payload: dict):
        return parse_action(payload)

    def authorize_done(self) -> DoneDecision:
        return authorize_done(
            self.state,
            strategy_remaining=strategy_remaining(self.state),
            recovery_exhausted=self.recovery.exhausted,
        )

    def final_text(self, model_text: str, decision: DoneDecision) -> str:
        delivery = ""
        summary = ""
        if self.state.evidence:
            delivery = str(self.state.evidence[-1].get("delivery") or "")
            summary = ""
        return final_text(decision.status, model_text, delivery=delivery, summary=summary)

    def notify_text(self, decision: DoneDecision) -> str:
        return decision.notify

    def reliability(self) -> dict:
        return {
            "goal": self.goal,
            "connector": self.last_connector,
            "tool": self.state.last_tool,
            "status": self.state.status,
            "verified": self.state.verified,
            "error_category": self.recovery.last_category,
            "strategy": self.state.last_tool,
        }

    def before_execute(self, action: Action) -> str:
        if self.state.blocked_external and self._is_external_repeat(action):
            return (
                "REFUS SYSTÈME : une action externe identique est déjà incertaine ou acceptée. "
                "N'envoie plus rien. Passe à tool=done en décrivant uniquement l'incertitude."
            )
        if action.exact_key() in self.seen:
            return (
                "REFUS SYSTÈME : cette commande a déjà été exécutée. "
                "Change de méthode ou passe à tool=done."
            )
        if (
            self.recovery.last_tool
            and action.tool == self.recovery.last_tool
            and self.recovery.must_switch(action.tool, self.recovery.last_category)
        ):
            return "REFUS SYSTÈME : deux erreurs de même nature. Change de stratégie."
        return ""

    def execute(self, action: Action, shell):
        self.seen.add(action.exact_key())
        if action.tool in SHELL_TOOLS and action.tool != "done":
            result = shell(action.tool, action.code)
        else:
            registry = self.registry or build_registry()
            self.registry = registry
            result = registry.execute(action.tool, action.args)
        self._absorb(action, result)
        return result

    def exhausted_decision(self) -> DoneDecision:
        if self.state.verified:
            status = "VERIFIED"
        elif self.state.status == "FAILED":
            status = "FAILED"
        elif self.state.status == "INDETERMINATE" or self.state.blocked_external:
            status = "INDETERMINATE"
        elif self.state.verification_required:
            status = "UNVERIFIED"
        elif self.state.action_attempted:
            status = "COMPLETED"
        else:
            status = "UNVERIFIED"
        self.state.status = status
        from ghost.core.task_manager import notify_for

        return DoneDecision(True, status, "Limite d'étapes atteinte.", notify_for(status))

    def _is_external_repeat(self, action: Action) -> bool:
        if action.tool == "outlook.send_email":
            return True
        return self.state.spec.task_type == "email_send" and _looks_like_send(action.code)

    def _absorb(self, action: Action, result) -> None:
        self.state.action_attempted = True
        self.state.attempts += 1
        self.state.last_tool = action.tool
        self.state.execution_success = bool(result.execution_success)
        self.last_connector = str((result.evidence or {}).get("connector") or "")
        if action.tool in {"bash", "python", "applescript"} and self.state.verification_required:
            verification = self._verify_shell(action, result)
            self._apply_verification(verification, result)
        elif (result.evidence or {}).get("effect") == "read":
            if self.state.status == "PLANNING":
                self.state.status = "RUNNING"
        elif self.state.verification_required and (result.evidence or {}).get("effect") == "external":
            self.state.verification_completed = True
            self._apply_action_status(result)
        elif self.state.verification_required:
            self.state.verification_completed = True
            self.state.status = "UNVERIFIED"
        elif result.execution_success and (result.stdout or result.stderr) and result.action_status == ACTION_UNVERIFIED:
            result.action_status = ACTION_EXECUTED
            result.status = ACTION_EXECUTED
        external = self.state.spec.external_effect and (
            action.tool == "outlook.send_email" or _looks_like_send(getattr(action, "code", ""))
        )
        decision = self.recovery.observe(result, action.tool, external=external)
        self.state.recovery_attempts = len(self.recovery.history)
        if decision.stop or (result.evidence or {}).get("adapters_exhausted") and result.action_status == ACTION_FAILED:
            self.recovery.exhausted = True
        if (result.evidence or {}).get("category") and not self.recovery.last_category:
            self.recovery.last_category = result.evidence["category"]
        shrunk = {
            key: value
            for key, value in (result.evidence or {}).items()
            if key not in {"body", "content", "messages", "access_token", "refresh_token"}
        }
        self.state.evidence.append(shrunk)

    def _apply_action_status(self, result) -> None:
        task_type = (result.evidence or {}).get("task_type")
        matches = task_type in {None, "", self.state.spec.task_type} or (
            self.state.spec.task_type == "email_send" and (result.evidence or {}).get("verifier") == "email_send"
        )
        if result.action_status == ACTION_VERIFIED and matches:
            self.state.verified = True
            self.state.status = "VERIFIED"
            if self.state.spec.non_idempotent:
                self.state.blocked_external = True
            return
        if result.action_status == ACTION_INDETERMINATE:
            self.state.status = "INDETERMINATE"
            if self.state.spec.non_idempotent or self.state.spec.task_type == "email_send":
                self.state.blocked_external = True
            self.recovery.exhausted = True
            return
        if result.action_status == ACTION_FAILED:
            self.state.status = "FAILED"
            return
        self.state.status = "UNVERIFIED"

    def _apply_verification(self, verification: VerificationResult, result) -> None:
        self.state.verification_completed = True
        if verification.summary:
            result.stdout = (result.stdout.rstrip() + "\n" + verification.summary).strip()
        result.evidence = dict(result.evidence or {})
        result.evidence.update(verification.evidence)
        result.evidence["effect"] = verification.effect
        result.evidence["task_type"] = verification.task_type
        if verification.status == "VERIFIED" and verification.task_type == self.state.spec.task_type:
            result.action_status = ACTION_VERIFIED
            result.action_verified = True
            result.status = ACTION_VERIFIED
            self.state.verified = True
            self.state.status = "VERIFIED"
            return
        if verification.status == "INDETERMINATE":
            result.action_status = ACTION_INDETERMINATE
            result.action_verified = False
            result.status = ACTION_INDETERMINATE
            self.state.status = "INDETERMINATE"
            if self.state.spec.non_idempotent:
                self.state.blocked_external = True
                self.recovery.exhausted = True
            return
        if verification.status == "FAILED" or not result.execution_success:
            result.action_status = ACTION_FAILED
            result.action_verified = False
            result.status = ACTION_FAILED
            if not result.error:
                result.error = f"ERREUR: {verification.summary or 'échec établi'}"
            self.state.status = "FAILED"
            return
        result.action_status = ACTION_UNVERIFIED
        result.action_verified = False
        result.status = ACTION_UNVERIFIED
        self.state.status = "UNVERIFIED"

    def _verify_shell(self, action: Action, result) -> VerificationResult:
        spec = self.state.spec
        if spec.task_type == "email_send":
            return self._verify_shell_email(action, result)
        if spec.task_type == "file_create":
            expected = dict(spec.expected)
            inferred = infer_created_path(action.code)
            if inferred and not expected.get("path"):
                expected["path"] = inferred
            if expected.get("path"):
                expected["min_mtime"] = self.started - 2
            verdict = verify_file_created(expected)
            return _downgrade_if_process_failed(verdict, result)
        if spec.task_type == "file_move":
            expected = dict(spec.expected)
            inferred = infer_move(action.code)
            if inferred and not expected.get("source"):
                expected.update(inferred)
            verdict = verify_file_moved(expected)
            return _downgrade_if_process_failed(verdict, result)
        if spec.task_type == "app_open":
            name = str(spec.expected.get("name") or "")
            if self.app_probe is not None:
                running = bool(self.app_probe(name))
            else:
                running = _app_running(name)
            verdict = verify_app_running({"name": name}, {"running": running})
            return _downgrade_if_process_failed(verdict, result)
        if not result.execution_success:
            return VerificationResult(
                spec.task_type,
                "FAILED",
                False,
                {"category": "action_failed"},
                ["Le processus a échoué."],
                "Le processus a échoué.",
            )
        return VerificationResult(
            spec.task_type,
            "UNVERIFIED",
            False,
            {},
            ["Aucune vérification fiable pour cette action."],
            "Aucune vérification fiable pour cette action.",
            effect="none",
        )

    def _verify_shell_email(self, action: Action, result) -> VerificationResult:
        expected = dict(self.state.spec.expected)
        if not result.execution_success:
            verdict = verify_sent_email(expected, [], started_at=self.started, now=time.time(), receipt=None)
            verdict.status = "FAILED"
            verdict.verified = False
            verdict.summary = result.error or "Le script a échoué avant une preuve d'envoi."
            verdict.reasons = [verdict.summary]
            return verdict
        if _looks_like_send(action.code):
            checker = self.email_checker or _default_email_checker
            verdict = checker(expected, self.started)
            if not verdict.verified and verdict.status == "UNVERIFIED":
                verdict.status = "INDETERMINATE"
                verdict.verified = False
                verdict.evidence["allow_resend"] = False
                verdict.summary = (
                    "Le script s'est terminé sans erreur, mais le message n'a pas été retrouvé. "
                    "Aucun nouvel envoi."
                )
            return verdict
        return verify_sent_email(expected, [], started_at=self.started, now=time.time(), receipt=None)


def _downgrade_if_process_failed(verdict: VerificationResult, result) -> VerificationResult:
    if result.execution_success or verdict.verified:
        return verdict
    verdict.status = "FAILED"
    verdict.verified = False
    verdict.summary = result.error or verdict.summary or "Le processus a échoué."
    return verdict


def _looks_like_send(code: str) -> bool:
    folded = code or ""
    if not _SEND_HINT.search(folded):
        return False
    lowered = folded.lower()
    return "outlook" in lowered or "keystroke" in lowered


def infer_created_path(code: str) -> str:
    try:
        tokens = shlex.split(code or "")
    except ValueError:
        tokens = []
    if tokens and os.path.basename(tokens[0]) == "touch" and len(tokens) > 1:
        return tokens[-1]
    match = re.search(r"(?<!>)>\s*([^\s;|&]+)", code or "")
    if match and ">>" not in (code or ""):
        return match.group(1)
    return ""


def infer_move(code: str) -> dict:
    try:
        tokens = shlex.split(code or "")
    except ValueError:
        return {}
    if len(tokens) >= 3 and os.path.basename(tokens[0]) == "mv":
        return {"source": tokens[-2], "destination": tokens[-1]}
    return {}


def _app_running(name: str) -> bool:
    if not name:
        return False
    try:
        from AppKit import NSWorkspace
    except Exception:
        return False
    folded = name.lower()
    try:
        apps = NSWorkspace.sharedWorkspace().runningApplications()
    except Exception:
        return False
    for app in apps:
        label = str(app.localizedName() or "")
        if folded in label.lower():
            return True
    return False


def _default_email_checker(expected: dict, started: float):
    from ghost.connectors.outlook.adapter import OutlookConnector

    connector = OutlookConnector()
    result = connector.verify_sent_email(
        {
            "recipient": expected.get("recipient"),
            "subject": expected.get("subject"),
            "body": expected.get("body"),
            "not_before": started,
        }
    )
    status = {
        ACTION_VERIFIED: "VERIFIED",
        ACTION_FAILED: "FAILED",
        ACTION_INDETERMINATE: "INDETERMINATE",
    }.get(result.action_status, "UNVERIFIED")
    return VerificationResult(
        "email_send",
        status,
        status == "VERIFIED",
        dict(result.evidence or {}),
        [result.stdout or result.error or ""],
        result.stdout or result.error or "",
    )


def describe_action(action: Action) -> str:
    return f"[{action.tool}] {public_args(action)}"
