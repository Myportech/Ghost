"""Vérification déterministe. Le modèle ne fabrique pas les preuves."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from ghost.core.results import (
    DELIVERY_CONFIRMED,
    REQUEST_ACCEPTED,
    SENT_ITEM_FOUND,
)

VERIFIED = "VERIFIED"
FAILED = "FAILED"
UNVERIFIED = "UNVERIFIED"
INDETERMINATE = "INDETERMINATE"

_SKEW_SECONDS = 15
_SUCCESS_NOISE = re.compile(
    r"tout semble|fonctionne|succès|succes|réussi|reussi|envoyé|envoyee|tâche terminée",
    re.I,
)


@dataclass
class VerificationResult:
    task_type: str
    status: str
    verified: bool
    evidence: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)
    summary: str = ""
    effect: str = "external"

    def __post_init__(self) -> None:
        self.verified = self.status == VERIFIED
        if self.evidence.get("delivery") == DELIVERY_CONFIRMED and not self.evidence.get(
            "delivery_proof"
        ):
            self.evidence["delivery"] = SENT_ITEM_FOUND
            self.reasons.append("Aucune preuve de remise au destinataire.")


def _result(
    task_type: str,
    status: str,
    reasons: list,
    evidence: Optional[dict] = None,
    *,
    effect: str = "external",
    summary: str = "",
) -> VerificationResult:
    evidence = dict(evidence or {})
    evidence.setdefault("allow_resend", status == FAILED)
    if status in {VERIFIED, INDETERMINATE}:
        evidence["allow_resend"] = False
    return VerificationResult(
        task_type=task_type,
        status=status,
        verified=status == VERIFIED,
        evidence=evidence,
        reasons=list(reasons),
        summary=summary or " ".join(reasons),
        effect=effect,
    )


def note_llm_claim(evidence: dict, llm_claim: Optional[str]) -> dict:
    evidence = dict(evidence)
    if llm_claim and _SUCCESS_NOISE.search(llm_claim):
        evidence["llm_claim_ignored"] = True
    elif llm_claim:
        evidence["llm_claim_ignored"] = True
    return evidence


def norm_text(value: str) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\xa0", " ").split())


def email_fingerprint(recipient: str, subject: str, body: str, *, action: str = "send") -> str:
    payload = "\n".join(
        [action, str(recipient).strip().lower(), norm_text(subject), plain_text(body)]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plain_text(value: str) -> str:
    text = str(value or "")
    if "<" in text and ">" in text:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = (
            text.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&#39;", "'")
            .replace("&quot;", '"')
        )
    return norm_text(text)


def parse_timestamp(value) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    return parsed.timestamp()


def normalize_message(message: dict) -> dict:
    raw_to = message.get("to")
    if raw_to is None and message.get("toRecipients"):
        raw_to = [
            ((item.get("emailAddress") or {}).get("address"))
            for item in message.get("toRecipients") or []
        ]
    if isinstance(raw_to, str):
        recipients = [raw_to]
    else:
        recipients = [str(item).strip().lower() for item in (raw_to or []) if str(item).strip()]
    recipients = [item.strip().lower() for item in recipients if item]
    body = message.get("body")
    if isinstance(body, dict):
        body = body.get("content") or ""
    if not body:
        body = message.get("bodyPreview") or message.get("plain") or ""
    return {
        "to": recipients,
        "subject": message.get("subject"),
        "body": body if body is not None else "",
        "sent_at": parse_timestamp(message.get("sent_at") or message.get("sentDateTime")),
    }


def verify_sent_email(
    expected: dict,
    messages,
    *,
    started_at: float,
    now: float,
    receipt: Optional[str] = None,
    llm_claim: Optional[str] = None,
) -> VerificationResult:
    """Compare un envoi attendu aux messages réellement observés."""
    recipient = str((expected or {}).get("recipient") or "").strip().lower()
    subject = (expected or {}).get("subject")
    body = (expected or {}).get("body")
    evidence = note_llm_claim(
        {
            "receipt": receipt,
            "delivery": None,
            "matches": 0,
            "mismatches": 0,
            "ignored_old": 0,
        },
        llm_claim,
    )
    if not recipient or subject is None or body is None:
        return _result(
            "email_send",
            UNVERIFIED,
            ["Critères de vérification incomplets : pas de preuve fabriquée."],
            evidence,
        )
    expected_subject = norm_text(subject)
    expected_body = plain_text(body)
    matches = []
    mismatches = []
    for raw in messages or []:
        message = normalize_message(raw if isinstance(raw, dict) else {})
        sent_at = message.get("sent_at")
        if sent_at is None or sent_at < started_at - _SKEW_SECONDS or sent_at > now + _SKEW_SECONDS:
            if sent_at is not None and _same_identity(message, recipient, expected_subject, expected_body):
                evidence["ignored_old"] += 1
            continue
        identical = _same_identity(message, recipient, expected_subject, expected_body)
        related = recipient in message["to"] or norm_text(message.get("subject") or "") == expected_subject
        if identical:
            matches.append(message)
        elif related:
            mismatches.append(message)
    evidence["matches"] = len(matches)
    evidence["mismatches"] = len(mismatches)
    if len(matches) == 1:
        evidence["delivery"] = SENT_ITEM_FOUND
        evidence["allow_resend"] = False
        return _result(
            "email_send",
            VERIFIED,
            [
                "Message retrouvé dans la fenêtre temporelle de l'opération.",
                "Destinataire, sujet et contenu conformes.",
                "Présence dans les éléments envoyés confirmée. La réception par le destinataire n'est pas confirmée.",
            ],
            evidence,
            summary=(
                "Envoi vérifié dans les éléments envoyés. "
                "La livraison au destinataire n'est pas confirmée."
            ),
        )
    if len(matches) > 1:
        evidence["allow_resend"] = False
        return _result(
            "email_send",
            INDETERMINATE,
            ["Plusieurs messages identiques dans la fenêtre temporelle. Aucun envoi supplémentaire."],
            evidence,
        )
    if mismatches:
        evidence["allow_resend"] = False
        return _result(
            "email_send",
            FAILED,
            ["Un message proche a été observé, mais un champ obligatoire ne correspond pas."],
            evidence,
        )
    if receipt in {REQUEST_ACCEPTED, "TIMEOUT_AFTER_SEND"}:
        evidence["allow_resend"] = False
        evidence["delivery"] = REQUEST_ACCEPTED if receipt == REQUEST_ACCEPTED else None
        return _result(
            "email_send",
            INDETERMINATE,
            [
                "La demande d'envoi n'a pas de preuve dans les éléments envoyés.",
                "Aucun nouvel envoi automatique.",
            ],
            evidence,
        )
    if receipt in {"AUTH", "PERMISSION", "UNAVAILABLE", "ACCESSIBILITY"}:
        evidence["category"] = {
            "AUTH": "authentication",
            "PERMISSION": "permission_denied",
            "UNAVAILABLE": "app_not_responding",
            "ACCESSIBILITY": "accessibility_permission",
        }[receipt]
        evidence["allow_resend"] = receipt in {"AUTH", "PERMISSION", "UNAVAILABLE", "ACCESSIBILITY"}
        return _result(
            "email_send",
            FAILED,
            [f"Envoi non effectué ({evidence['category']})."],
            evidence,
        )
    return _result(
        "email_send",
        UNVERIFIED,
        ["Aucun message correspondant dans la fenêtre de l'opération."],
        evidence,
    )


def _same_identity(message: dict, recipient: str, subject: str, body: str) -> bool:
    recipients = message.get("to") or []
    if recipients != [recipient]:
        return False
    if norm_text(message.get("subject") or "") != subject:
        return False
    return plain_text(message.get("body") or "") == body


def _sha256_file(path: str) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def verify_file_created(
    expected: dict,
    observed: Optional[dict] = None,
    *,
    probe: bool = True,
    llm_claim: Optional[str] = None,
) -> VerificationResult:
    path = os.path.expanduser(str((expected or {}).get("path") or ""))
    evidence = note_llm_claim({"path": path}, llm_claim)
    if not path:
        return _result("file_create", UNVERIFIED, ["Chemin attendu absent."], evidence)
    if observed is None and probe:
        observed = _probe_file(path)
    observed = observed or {}
    exists = bool(observed.get("exists"))
    evidence["exists"] = exists
    evidence["location_ok"] = bool(observed.get("location_ok", exists))
    if not exists:
        return _result(
            "file_create",
            UNVERIFIED,
            ["Le fichier attendu n'est pas observable. Le processus ne prouve pas sa création."],
            evidence,
        )
    if expected.get("min_mtime") is not None and observed.get("mtime") is not None:
        if float(observed["mtime"]) + 1 < float(expected["min_mtime"]):
            evidence["stale"] = True
            return _result(
                "file_create",
                UNVERIFIED,
                ["Le fichier existait déjà avant cette opération. Il ne prouve pas la création."],
                evidence,
            )
    if observed.get("is_dir"):
        return _result("file_create", FAILED, ["Un dossier occupe le chemin attendu, pas un fichier."], evidence)
    if expected.get("content") is not None:
        if "content" not in observed:
            evidence["partial"] = True
            return _result("file_create", UNVERIFIED, ["Le fichier existe, mais son contenu n'a pas été lu."], evidence)
        if norm_text(observed.get("content") or "") != norm_text(expected.get("content") or ""):
            return _result("file_create", FAILED, ["Le contenu observé ne correspond pas au contenu attendu."], evidence)
    if observed.get("readable") is False:
        return _result("file_create", FAILED, ["Le fichier existe mais ne peut pas être ouvert."], evidence)
    evidence["readable"] = observed.get("readable", True)
    return _result(
        "file_create",
        VERIFIED,
        ["Le fichier existe au chemin attendu et peut être ouvert."],
        evidence,
    )


def _probe_file(path: str) -> dict:
    try:
        exists = os.path.exists(path)
    except OSError:
        return {"exists": False, "readable": False}
    if not exists:
        return {"exists": False}
    if os.path.isdir(path):
        return {"exists": True, "is_dir": True, "location_ok": True}
    try:
        with open(path, "rb") as handle:
            blob = handle.read(200000)
        readable = True
        try:
            content = blob.decode("utf-8")
        except UnicodeDecodeError:
            content = None
            readable = True
    except OSError:
        return {"exists": True, "readable": False, "location_ok": True}
    payload = {"exists": True, "is_dir": False, "readable": readable, "location_ok": True, "mtime": os.path.getmtime(path)}
    if content is not None:
        payload["content"] = content
    return payload


def verify_file_moved(
    expected: dict,
    observed: Optional[dict] = None,
    *,
    probe: bool = True,
    llm_claim: Optional[str] = None,
) -> VerificationResult:
    source = os.path.expanduser(str((expected or {}).get("source") or ""))
    dest = os.path.expanduser(str((expected or {}).get("destination") or ""))
    evidence = note_llm_claim({"source": source, "destination": dest}, llm_claim)
    if not source or not dest:
        return _result("file_move", UNVERIFIED, ["Source ou destination absente."], evidence)
    if observed is None and probe:
        observed = {
            "source_exists": os.path.exists(source),
            "destination_exists": os.path.exists(dest),
        }
        if observed["destination_exists"] and os.path.isfile(dest) and expected.get("sha256"):
            observed["destination_sha256"] = _sha256_file(dest)
    observed = observed or {}
    src_exists = bool(observed.get("source_exists"))
    dst_exists = bool(observed.get("destination_exists"))
    evidence.update({"source_exists": src_exists, "destination_exists": dst_exists})
    if dst_exists and not src_exists:
        wanted = expected.get("sha256")
        got = observed.get("destination_sha256")
        if wanted and got and wanted != got:
            return _result("file_move", FAILED, ["La destination existe, mais le contenu diffère."], evidence)
        if wanted and not got:
            evidence["partial"] = True
            return _result("file_move", UNVERIFIED, ["Destination présente, empreinte non vérifiée."], evidence)
        return _result(
            "file_move",
            VERIFIED,
            ["Le fichier est à destination et a disparu de l'emplacement initial."],
            evidence,
        )
    if dst_exists and src_exists:
        return _result("file_move", FAILED, ["La source et la destination existent encore."], evidence)
    if not dst_exists and not src_exists:
        return _result("file_move", INDETERMINATE, ["Ni la source ni la destination ne sont visibles."], evidence)
    return _result(
        "file_move",
        UNVERIFIED,
        ["La destination est absente. Le déplacement n'est pas prouvé."],
        evidence,
    )


def verify_calendar_event(
    expected: dict,
    events,
    *,
    llm_claim: Optional[str] = None,
) -> VerificationResult:
    title = (expected or {}).get("title")
    start = parse_timestamp((expected or {}).get("start"))
    evidence = note_llm_claim({"title": title, "start": start}, llm_claim)
    if title is None or start is None:
        return _result("calendar_create", UNVERIFIED, ["Titre ou horaire attendu absent."], evidence)
    found = []
    for event in events or []:
        event_title = norm_text((event or {}).get("title") or (event or {}).get("summary") or "")
        event_start = parse_timestamp((event or {}).get("start"))
        if event_title == norm_text(title) and event_start is not None and abs(event_start - start) <= 60:
            found.append(event)
    evidence["matches"] = len(found)
    if len(found) == 1:
        return _result("calendar_create", VERIFIED, ["L'événement existe avec le titre et l'horaire attendus."], evidence)
    if len(found) > 1:
        return _result("calendar_create", INDETERMINATE, ["Plusieurs événements correspondent."], evidence)
    return _result("calendar_create", UNVERIFIED, ["Aucun événement correspondant n'a été observé."], evidence)


def verify_app_running(expected: dict, observed: Optional[dict] = None, *, probe: Optional[Callable] = None, llm_claim: Optional[str] = None) -> VerificationResult:
    name = str((expected or {}).get("name") or "").strip()
    evidence = note_llm_claim({"name": name}, llm_claim)
    if not name:
        return _result("app_open", UNVERIFIED, ["Nom d'application absent."], evidence)
    if observed is None and probe is not None:
        observed = {"running": bool(probe(name))}
    running = bool((observed or {}).get("running"))
    evidence["running"] = running
    if running:
        return _result("app_open", VERIFIED, [f"{name} est en cours d'exécution."], evidence)
    return _result("app_open", UNVERIFIED, [f"{name} n'a pas été observé en cours d'exécution."], evidence)


def reject_unproven_success(claim: str, verification: VerificationResult) -> VerificationResult:
    """Une phrase du modèle ne change jamais un statut non vérifié."""
    if verification.verified:
        return verification
    verification.evidence = note_llm_claim(verification.evidence, claim or "succès")
    verification.reasons = list(verification.reasons) + ["L'annonce du modèle n'est pas une preuve."]
    return verification


VERIFIERS = {
    "email_send": verify_sent_email,
    "file_create": verify_file_created,
    "file_move": verify_file_moved,
    "calendar_create": verify_calendar_event,
    "app_open": verify_app_running,
}


def verify_task(task_type: str, expected: dict, observed=None, **kwargs) -> VerificationResult:
    verifier = VERIFIERS.get(task_type)
    if verifier is None:
        return _result(
            task_type or "unknown",
            UNVERIFIED,
            ["Aucun vérificateur indépendant pour cette action."],
            {"task_type": task_type},
            effect="none",
        )
    if task_type == "email_send":
        return verifier(expected, observed or [], **kwargs)
    if task_type == "calendar_create":
        return verifier(expected, observed or [], **kwargs)
    return verifier(expected, observed, **kwargs)
