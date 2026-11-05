"""Lance un processus et qualifie son résultat, sans conclure l'action métier."""

from __future__ import annotations

import re
import subprocess
import time
from typing import Optional, Sequence

from ghost.core.results import (
    ACTION_FAILED,
    ACTION_INDETERMINATE,
    ACTION_UNVERIFIED,
    PROCESS_FAILED,
    PROCESS_INTERRUPTED,
    PROCESS_SUCCEEDED,
    PROCESS_TIMEOUT,
    ExecutionResult,
    make_result,
)

_PERMISSION_RE = re.compile(
    r"operation not permitted|not authorized|permission denied|"
    r"errAEEventNotPermitted|-1743|not allowed to send apple events",
    re.I,
)
_PDF_HINT = (
    "[ERREUR: Ceci est un fichier PDF binaire brut. "
    "N'utilise JAMAIS cat ou textutil sur un PDF. "
    "Utilise bash avec python3 -c et PdfReader (pypdf ou PyPDF2).]"
)
_BLIND_SEND_RE = re.compile(
    r"""keystroke\s+["']d["']\s+using\s+command down""",
    re.I,
)
_BLIND_RETURN_RE = re.compile(
    r"""keystroke\s+return\s+using\s+command down""",
    re.I,
)


def blocked_blind_input(code: str) -> str:
    """Refuse les raccourcis d'envoi qui ne prouvent pas le contenu des champs."""
    text = code or ""
    if _BLIND_SEND_RE.search(text) or _BLIND_RETURN_RE.search(text):
        return (
            "ERREUR: raccourci clavier d'envoi refusé. "
            "Utilise outlook.send_email, qui vérifie les champs avant l'envoi."
        )
    return ""


def assess_process(
    returncode: Optional[int],
    stdout: str,
    stderr: str,
    *,
    timed_out: bool = False,
    interrupted: bool = False,
) -> dict:
    """Sépare le code de retour, l'erreur d'exécution et l'absence de preuve métier."""
    out = stdout or ""
    err = stderr or ""
    blob = f"{out}\n{err}"
    permission = bool(_PERMISSION_RE.search(blob))
    if timed_out:
        return {
            "process_status": PROCESS_TIMEOUT,
            "action_status": ACTION_INDETERMINATE,
            "execution_success": False,
            "permission_denied": permission,
            "interrupted": False,
            "error": "ERREUR: délai dépassé.",
        }
    if interrupted or (returncode is not None and returncode < 0):
        return {
            "process_status": PROCESS_INTERRUPTED,
            "action_status": ACTION_INDETERMINATE,
            "execution_success": False,
            "permission_denied": permission,
            "interrupted": True,
            "error": f"ERREUR: processus interrompu (code {returncode}).",
        }
    if returncode not in (0, None) or (returncode is None and not out and not err):
        code = returncode if returncode is not None else "inconnu"
        error = f"ERREUR: code de retour {code}."
        if permission:
            error += " Permission macOS refusée."
        return {
            "process_status": PROCESS_FAILED,
            "action_status": ACTION_FAILED,
            "execution_success": False,
            "permission_denied": permission,
            "interrupted": False,
            "error": error,
        }
    action = ACTION_FAILED if permission else ACTION_UNVERIFIED
    error = "ERREUR: permission macOS refusée." if permission else None
    return {
        "process_status": PROCESS_SUCCEEDED,
        "action_status": action,
        "execution_success": True,
        "permission_denied": permission,
        "interrupted": False,
        "error": error,
    }


def _cap(text: str, limit: int = 20000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… [sortie tronquée]"


def run_subprocess(
    *,
    tool: str,
    argv: Optional[Sequence[str]] = None,
    shell_command: Optional[str] = None,
    timeout: float = 20,
    cwd: Optional[str] = None,
) -> ExecutionResult:
    started = time.monotonic()
    if blocked := blocked_blind_input(shell_command or " ".join(argv or ())):
        return make_result(
            tool,
            process_status=PROCESS_FAILED,
            action_status=ACTION_FAILED,
            error=blocked,
            duration=0.0,
        )
    run_kwargs = dict(
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
    )
    try:
        if shell_command is not None:
            proc = subprocess.run(shell_command, shell=True, **run_kwargs)
        else:
            proc = subprocess.run(list(argv or []), shell=False, **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        judged = assess_process(None, str(stdout), str(stderr), timed_out=True)
        return _finish(tool, None, str(stdout), str(stderr), judged, time.monotonic() - started)
    except KeyboardInterrupt:
        judged = assess_process(None, "", "", interrupted=True)
        return _finish(tool, None, "", "", judged, time.monotonic() - started)
    except Exception as exc:
        return make_result(
            tool,
            process_status=PROCESS_FAILED,
            action_status=ACTION_FAILED,
            error=f"ERREUR: {exc}",
            duration=time.monotonic() - started,
        )
    return _finish(
        tool,
        proc.returncode,
        proc.stdout or "",
        proc.stderr or "",
        assess_process(proc.returncode, proc.stdout or "", proc.stderr or ""),
        time.monotonic() - started,
    )


def _finish(tool: str, returncode, stdout: str, stderr: str, judged: dict, duration: float) -> ExecutionResult:
    stdout = _cap(stdout)
    stderr = _cap(stderr)
    evidence = {}
    if stdout.lstrip().startswith("%PDF-"):
        stdout = _PDF_HINT
        judged = dict(judged)
        judged["action_status"] = ACTION_FAILED
        judged["error"] = _PDF_HINT
        evidence["pdf_binary"] = True
    return make_result(
        tool,
        process_status=judged["process_status"],
        action_status=judged["action_status"],
        return_code=returncode,
        stdout=stdout,
        stderr=stderr,
        error=judged.get("error"),
        evidence=evidence,
        duration=duration,
        permission_denied=judged.get("permission_denied", False),
        interrupted=judged.get("interrupted", False),
    )


def result_from_checked_text(tool: str, text: str, *, duration: float = 0.0) -> ExecutionResult:
    """Convertit une traduction Finder déjà exécutée, sans la déclarer réussie métier."""
    body = text or ""
    failed = bool(
        re.search(r"ERREUR|Error:|execution error|Operation not permitted|not authorized", body, re.I)
    )
    if failed:
        permission = bool(_PERMISSION_RE.search(body))
        return make_result(
            tool,
            process_status=PROCESS_FAILED,
            action_status=ACTION_FAILED,
            return_code=1,
            stdout=body,
            error="ERREUR: échec de la commande traduite.",
            duration=duration,
            permission_denied=permission,
            evidence={"legacy_text": True},
        )
    return make_result(
        tool,
        process_status=PROCESS_SUCCEEDED,
        action_status=ACTION_UNVERIFIED,
        return_code=0,
        stdout=body,
        duration=duration,
        evidence={"legacy_text": True},
    )
