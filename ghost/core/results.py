"""Résultats d'exécution : un processus terminé n'est pas une action accomplie."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ghost.core.redact import redact

PROCESS_SUCCEEDED = "PROCESS_SUCCEEDED"
PROCESS_FAILED = "PROCESS_FAILED"
PROCESS_TIMEOUT = "PROCESS_TIMEOUT"
PROCESS_INTERRUPTED = "PROCESS_INTERRUPTED"

ACTION_EXECUTED = "ACTION_EXECUTED"
ACTION_VERIFIED = "ACTION_VERIFIED"
ACTION_FAILED = "ACTION_FAILED"
ACTION_UNVERIFIED = "ACTION_UNVERIFIED"
ACTION_INDETERMINATE = "ACTION_INDETERMINATE"

REQUEST_ACCEPTED = "REQUEST_ACCEPTED"
SENT_ITEM_FOUND = "SENT_ITEM_FOUND"
DELIVERY_CONFIRMED = "DELIVERY_CONFIRMED"

TASK_PLANNING = "PLANNING"
TASK_RUNNING = "RUNNING"
TASK_VERIFIED = "VERIFIED"
TASK_FAILED = "FAILED"
TASK_UNVERIFIED = "UNVERIFIED"
TASK_INDETERMINATE = "INDETERMINATE"
TASK_COMPLETED = "COMPLETED"


@dataclass
class ExecutionResult:
    tool: str
    status: str
    return_code: Optional[int]
    stdout: str
    stderr: str
    execution_success: bool
    action_verified: bool
    evidence: dict = field(default_factory=dict)
    duration: float = 0.0
    error: Optional[str] = None
    process_status: str = PROCESS_FAILED
    action_status: str = ACTION_UNVERIFIED
    permission_denied: bool = False
    interrupted: bool = False

    def __post_init__(self) -> None:
        self.action_verified = self.action_status == ACTION_VERIFIED
        self.status = self.action_status or self.process_status
        self.error = redact(self.error) if self.error else None
        self.stdout = self.stdout or ""
        self.stderr = self.stderr or ""

    def render(self, limit: int = 1400) -> str:
        header = f"[{self.process_status} | {self.action_status}]"
        chunks = [header]
        if self.error:
            chunks.append(self.error)
        out = self.stdout.strip()
        err = self.stderr.strip()
        if out:
            chunks.append(out)
        if err and err not in out:
            chunks.append(err)
        if len(chunks) == 1:
            chunks.append("(aucune sortie)")
        text = redact("\n".join(chunks))
        if len(text) <= limit:
            return text
        marker = "\n… [milieu omis] …\n"
        budget = limit - len(marker)
        head = max(1, int(budget * 0.6))
        tail = max(1, budget - head)
        return text[:head] + marker + text[-tail:]


def make_result(
    tool: str,
    *,
    process_status: str,
    action_status: str,
    return_code: Optional[int] = None,
    stdout: str = "",
    stderr: str = "",
    error: Optional[str] = None,
    evidence: Optional[dict] = None,
    duration: float = 0.0,
    permission_denied: bool = False,
    interrupted: bool = False,
) -> ExecutionResult:
    execution_success = process_status == PROCESS_SUCCEEDED
    return ExecutionResult(
        tool=tool,
        status=action_status,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        execution_success=execution_success,
        action_verified=action_status == ACTION_VERIFIED,
        evidence=dict(evidence or {}),
        duration=duration,
        error=error,
        process_status=process_status,
        action_status=action_status,
        permission_denied=permission_denied,
        interrupted=interrupted,
    )


def failed_result(
    tool: str,
    message: str,
    *,
    return_code: Optional[int] = None,
    stdout: str = "",
    stderr: str = "",
    evidence: Optional[dict] = None,
    duration: float = 0.0,
    process_status: str = PROCESS_FAILED,
    permission_denied: bool = False,
    interrupted: bool = False,
) -> ExecutionResult:
    text = message if str(message).startswith("ERREUR") else f"ERREUR: {message}"
    return make_result(
        tool,
        process_status=process_status,
        action_status=ACTION_FAILED,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        error=text,
        evidence=evidence,
        duration=duration,
        permission_denied=permission_denied,
        interrupted=interrupted,
    )
