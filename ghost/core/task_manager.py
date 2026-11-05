"""Classification de la tâche et autorisation de clôture."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ghost.core.results import (
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_INDETERMINATE,
    TASK_PLANNING,
    TASK_UNVERIFIED,
    TASK_VERIFIED,
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_PATH_RE = re.compile(r"(?:~|/|\$HOME|\$\{HOME\})[^\s\"'`|;<>]+")


def fold(text: str) -> str:
    folded = (text or "").lower()
    return folded.translate(str.maketrans({
        "à": "a", "á": "a", "â": "a", "ä": "a",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "î": "i", "ï": "i", "í": "i",
        "ô": "o", "ö": "o", "ó": "o",
        "ù": "u", "û": "u", "ü": "u",
        "ç": "c", "œ": "oe", "’": "'",
    }))


@dataclass
class TaskSpec:
    task_type: str
    verification_required: bool
    external_effect: bool
    idempotent: bool
    non_idempotent: bool
    requires_tool: bool
    expected: dict = field(default_factory=dict)


@dataclass
class DoneDecision:
    allow: bool
    status: str
    reason: str
    notify: str


def extract_email_expected(goal: str) -> dict:
    emails = _EMAIL_RE.findall(goal or "")
    subject = None
    body = None
    subject_match = re.search(
        r"(?:sujet|subject)\s*[:=]\s*(.+?)(?=\s+(?:corps|body|message)\s*[:=]|$)",
        goal or "",
        re.I | re.S,
    )
    body_match = re.search(r"(?:corps|body|message)\s*[:=]\s*(.+)$", goal or "", re.I | re.S)
    if subject_match:
        subject = subject_match.group(1).strip().strip("\"'«»")
    if body_match:
        body = body_match.group(1).strip().strip("\"'«»")
    return {
        "recipient": emails[0] if emails else "",
        "subject": subject,
        "body": body,
    }


def _paths(text: str) -> list:
    return [item.rstrip(".,);]") for item in _PATH_RE.findall(text or "")]


def classify_task(goal: str) -> TaskSpec:
    text = fold(goal)
    email_expected = extract_email_expected(goal)
    if re.search(r"\b(envoy\w*|envoi)\b", text) and (
        email_expected["recipient"] or re.search(r"\b(mail|e-mail|courriel|outlook)\b", text)
    ):
        return TaskSpec(
            "email_send",
            True,
            True,
            False,
            True,
            True,
            email_expected,
        )
    if re.search(r"\b(deplace|renomme|deplacer)\b", text):
        paths = _paths(goal)
        expected = {}
        if len(paths) >= 2:
            expected = {"source": paths[0], "destination": paths[1]}
        return TaskSpec("file_move", True, True, True, False, True, expected)
    if re.search(r"\b(cree|creer|ecris|ecrire)\b", text) and re.search(r"\b(fichier|file|dossier)\b", text):
        paths = _paths(goal)
        expected = {"path": paths[0]} if paths else {}
        return TaskSpec("file_create", True, True, True, False, True, expected)
    if re.search(r"\b(calendrier|evenement|rendez-vous|rdv)\b", text) and re.search(
        r"\b(cree|ajoute|planifie|nouveau)\b", text
    ):
        return TaskSpec("calendar_create", True, True, False, True, True, {})
    if re.search(r"\b(ouvre|ouvrir|lance)\b", text):
        name = ""
        match = re.search(r"\b(?:ouvre|ouvrir|lance)\s+(?:l'|le |la |les )?([a-z0-9][\w .-]{1,40})", text)
        if match:
            name = match.group(1).strip()
        return TaskSpec("app_open", True, True, True, False, True, {"name": name})
    if re.search(r"\b(supprime|efface|jette)\b", text):
        return TaskSpec("file_delete", True, True, True, False, True, {})
    if re.search(
        r"\b(pourquoi|comment|explique|qu'est|c'est quoi|quel|quelle|liste|affiche|montre|cherche|resume|lis|lire)\b",
        text,
    ):
        return TaskSpec("informational", False, False, True, False, True, {})
    return TaskSpec("general", False, False, True, False, True, {})


class TaskState:
    def __init__(self, goal: str):
        self.goal = goal
        self.spec = classify_task(goal)
        self.status = TASK_PLANNING
        self.action_attempted = False
        self.execution_success = False
        self.verification_required = self.spec.verification_required
        self.verification_completed = False
        self.verified = False
        self.evidence: list = []
        self.attempts = 0
        self.recovery_attempts = 0
        self.done_rejections = 0
        self.last_tool = ""
        self.blocked_external = False

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "action_attempted": self.action_attempted,
            "execution_success": self.execution_success,
            "verification_required": self.verification_required,
            "verification_completed": self.verification_completed,
            "verified": self.verified,
            "evidence": list(self.evidence[-8:]),
            "attempts": self.attempts,
            "recovery_attempts": self.recovery_attempts,
        }


def notify_for(status: str) -> str:
    return {
        TASK_VERIFIED: "Ghost : succès vérifié.",
        TASK_COMPLETED: "Ghost : réponse prête.",
        TASK_FAILED: "Ghost : échec établi.",
        TASK_UNVERIFIED: "Ghost : résultat non vérifié.",
        TASK_INDETERMINATE: "Ghost : résultat incertain, aucun nouvel envoi.",
    }.get(status, "Ghost : résultat non vérifié.")


def authorize_done(state: TaskState, *, strategy_remaining: bool, recovery_exhausted: bool) -> DoneDecision:
    """Le modèle ne clôt une action externe que si Python a une preuve."""
    if state.verification_required:
        if not state.action_attempted or not state.verification_completed:
            return DoneDecision(
                False,
                state.status,
                "tool=done refusé : aucune vérification indépendante n'a encore été exécutée.",
                "",
            )
        if state.verified:
            return DoneDecision(True, TASK_VERIFIED, "Preuves suffisantes.", notify_for(TASK_VERIFIED))
        if state.status == TASK_INDETERMINATE or state.blocked_external:
            state.status = TASK_INDETERMINATE
            return DoneDecision(
                True,
                TASK_INDETERMINATE,
                "Résultat incertain. Aucun nouvel essai automatique.",
                notify_for(TASK_INDETERMINATE),
            )
        if state.status == TASK_FAILED and not recovery_exhausted:
            return DoneDecision(
                False,
                TASK_FAILED,
                "Échec établi. Une autre stratégie est encore possible, sans répéter l'action incertaine.",
                "",
            )
        if state.status == TASK_FAILED and recovery_exhausted:
            return DoneDecision(True, TASK_FAILED, "Échec établi.", notify_for(TASK_FAILED))
        if strategy_remaining and state.done_rejections < 1:
            state.done_rejections += 1
            return DoneDecision(
                False,
                TASK_UNVERIFIED,
                "Résultat non vérifié. Change de stratégie ou apporte une preuve observable.",
                "",
            )
        state.status = TASK_UNVERIFIED
        return DoneDecision(
            True,
            TASK_UNVERIFIED,
            "Aucune preuve suffisante. La tâche n'est pas marquée réussie.",
            notify_for(TASK_UNVERIFIED),
        )
    if state.spec.requires_tool and not state.action_attempted:
        return DoneDecision(
            False,
            TASK_PLANNING,
            "tool=done refusé : exécute d'abord l'action demandée.",
            "",
        )
    return DoneDecision(True, TASK_COMPLETED, "Tâche sans effet externe.", notify_for(TASK_COMPLETED))


def strategy_remaining(state: TaskState) -> bool:
    if state.last_tool == "outlook.send_email":
        return False
    if state.spec.task_type == "email_send" and state.last_tool in {"bash", "python", "applescript"}:
        return True
    if state.spec.task_type in {"file_create", "file_move", "app_open"} and state.attempts < 2:
        return True
    return False
