"""Connecteur Outlook : Graph, puis AppleScript validé, puis Accessibilité."""

from __future__ import annotations

import time
from pathlib import Path

from ghost.connectors.base import BaseConnector
from ghost.connectors.outlook.accessibility_adapter import AccessibilityOutlook
from ghost.connectors.outlook.applescript_adapter import AppleScriptOutlook, outlook_installed
from ghost.connectors.outlook.graph_client import GraphMailClient
from ghost.core.operation_journal import OperationJournal
from ghost.core.results import (
    ACTION_FAILED,
    ACTION_INDETERMINATE,
    ACTION_UNVERIFIED,
    ACTION_VERIFIED,
    PROCESS_FAILED,
    PROCESS_SUCCEEDED,
    PROCESS_TIMEOUT,
    REQUEST_ACCEPTED,
    make_result,
)
from ghost.core.status_text import email_progress
from ghost.core.verification_engine import (
    INDETERMINATE,
    UNVERIFIED,
    VERIFIED,
    email_fingerprint,
    verify_sent_email,
)

_ACTION_STATUS = {
    "VERIFIED": ACTION_VERIFIED,
    "FAILED": ACTION_FAILED,
    "UNVERIFIED": ACTION_UNVERIFIED,
    "INDETERMINATE": ACTION_INDETERMINATE,
}


class OutlookConnector(BaseConnector):
    name = "outlook"

    def __init__(self, graph=None, applescript=None, accessibility=None, journal=None, clock=None) -> None:
        self.graph = graph if graph is not None else GraphMailClient()
        self.applescript = applescript if applescript is not None else AppleScriptOutlook()
        self.accessibility = accessibility if accessibility is not None else AccessibilityOutlook()
        if journal is None:
            journal = OperationJournal(Path.home() / ".ghost_operations.json")
        self.journal = journal
        self.clock = clock or time.time

    def now(self) -> float:
        value = self.clock() if callable(self.clock) else self.clock
        return float(value)

    def is_available(self) -> bool:
        return bool(
            outlook_installed()
            or self.graph.is_available()
            or self.applescript.is_available()
            or self.accessibility.is_available()
        )

    def get_capabilities(self) -> list:
        caps = []
        if self.graph.is_available():
            caps.append("graph")
        if self.applescript.is_available():
            caps.append("applescript")
        if self.accessibility.is_available():
            caps.append("accessibility")
        return caps

    def get_status(self) -> dict:
        return {
            "name": self.name,
            "installed": outlook_installed(),
            "graph_configured": self.graph.configured(),
            "graph_token": self.graph.has_token(),
            "applescript": self.applescript.is_available(),
            "accessibility": self.accessibility.is_available(),
            "capabilities": self.get_capabilities(),
        }

    def execute(self, action: str, arguments: dict):
        method = {
            "send_email": self.send_email,
            "create_draft": self.create_draft,
            "search_emails": self.search_emails,
            "read_email": self.read_email,
            "search_sent_items": self.search_sent_items,
            "verify_sent_email": self.verify_sent_email,
            "login": self.login,
        }.get(action)
        if method is None:
            return _failed("outlook." + str(action), f"Action Outlook inconnue : {action}")
        return method(arguments or {})

    def verify(self, action: str, expected_state: dict):
        if action in {"send_email", "verify_sent_email", "search_sent_items"}:
            return self.verify_sent_email(expected_state)
        return self.execute(action, expected_state)

    def login(self, _args=None):
        if self.graph.has_token():
            receipt = self.graph.poll_device_flow() if self.graph._device else None
            if receipt is None:
                return _ok_read("outlook.login", "Compte Microsoft déjà connecté.")
        receipt = self.graph.poll_device_flow()
        status = ACTION_VERIFIED if receipt.outcome == "REQUEST_ACCEPTED" and "connecté" in (receipt.error or "") else ACTION_UNVERIFIED
        if receipt.outcome == "REQUEST_ACCEPTED" and self.graph.has_token():
            status = ACTION_VERIFIED
        process = PROCESS_SUCCEEDED if receipt.outcome != "FAILED" else PROCESS_FAILED
        return make_result(
            "outlook.login",
            process_status=process,
            action_status=status,
            stdout=receipt.error or "Connexion Microsoft.",
            error=None if process == PROCESS_SUCCEEDED else f"ERREUR: {receipt.error}",
            evidence={"effect": "read", "category": "authentication" if status != ACTION_VERIFIED else ""},
        )

    def send_email(self, args: dict):
        expected = _expected(args)
        started = self.now()
        fingerprint = email_fingerprint(expected["recipient"], expected["subject"], expected["body"])
        lines = ["🤖 Étape 1 : Identification du compte Outlook."]
        if self.journal.has_blocking(fingerprint, now=started):
            lines.append("⚠️ Une opération identique est déjà acceptée ou incertaine.")
            lines.append(email_progress("search"))
            verdict = self._verify_messages(expected, started, REQUEST_ACCEPTED)
            lines.append(_verdict_text(verdict))
            return _from_verdict("outlook.send_email", verdict, lines, connector="journal")
        if self.graph.configured() and self.graph.has_token():
            lines.append(email_progress("account_ok") + " Via Microsoft Graph.")
        elif self.graph.configured():
            lines.append("⚠️ Identifiant Graph présent, mais le compte n'est pas connecté.")
        else:
            lines.append("Microsoft Graph n'est pas configuré. Les autres adaptateurs restent utilisables.")
        adapters = self._send_adapters()
        if not adapters:
            verdict = verify_sent_email(
                expected, [], started_at=started, now=self.now(), receipt="UNAVAILABLE"
            )
            lines.append("Outlook est inaccessible (Graph, AppleScript et Accessibilité).")
            result = _from_verdict("outlook.send_email", verdict, lines, connector="none")
            result.evidence["adapters_exhausted"] = True
            return result
        last = None
        for index, (name, runner) in enumerate(adapters):
            result = runner(expected, started, fingerprint, lines)
            last = result
            if result.evidence.get("stop"):
                return result
            if index < len(adapters) - 1:
                lines.append(f"↩️ Bascule depuis {name}.")
        if last is not None:
            last.evidence["adapters_exhausted"] = True
            last.stdout = "\n".join(lines + [last.stdout]).strip()
            return last
        return _failed("outlook.send_email", "Aucun adaptateur Outlook n'a pu être utilisé.")

    def _send_adapters(self):
        adapters = []
        if self.graph.configured():
            adapters.append(("graph", self._send_graph))
        if self.applescript.is_available():
            adapters.append(("applescript", self._send_applescript))
        if self.accessibility.is_available():
            adapters.append(("accessibility", self._send_accessibility))
        return adapters

    def _send_graph(self, expected, started, fingerprint, lines):
        lines.append("🤖 Étape 2 : Préparation du message.")
        lines.append(email_progress("fields_ok"))
        lines.append("🤖 Étape 3 : Envoi du message.")
        receipt = self.graph.send_mail(expected["recipient"], expected["subject"], expected["body"])
        if receipt.outcome == "TIMEOUT":
            self.journal.record(fingerprint, "TIMEOUT_AFTER_SEND", "graph", now=self.now())
            lines.append("⚠️ Délai pendant l'appel Graph. Recherche du message avant toute autre tentative.")
            lines.append(email_progress("search"))
            verdict = self._verify_messages(expected, started, "TIMEOUT_AFTER_SEND")
            if verdict.verified:
                self.journal.record(fingerprint, "VERIFIED", "graph", now=self.now())
            lines.append(_verdict_text(verdict))
            result = _from_verdict(
                "outlook.send_email", verdict, lines, connector="graph", process_status=PROCESS_TIMEOUT
            )
            result.evidence["stop"] = True
            return result
        if receipt.outcome == "REQUEST_ACCEPTED":
            self.journal.record(fingerprint, REQUEST_ACCEPTED, "graph", now=self.now())
            lines.append("✅ Requête acceptée par Microsoft Graph (HTTP %s)." % (receipt.http_status or 202))
            lines.append("Ceci prouve l'acceptation de la requête, pas la livraison au destinataire.")
            lines.append("🤖 Étape 4 : Vérification indépendante.")
            lines.append(email_progress("search"))
            verdict = self._verify_messages(expected, started, REQUEST_ACCEPTED)
            if verdict.verified:
                self.journal.record(fingerprint, "VERIFIED", "graph", now=self.now())
            elif verdict.status == INDETERMINATE:
                self.journal.record(fingerprint, "INDETERMINATE", "graph", now=self.now())
            lines.append(_verdict_text(verdict))
            result = _from_verdict("outlook.send_email", verdict, lines, connector="graph")
            result.evidence["stop"] = True
            result.evidence["http_status"] = receipt.http_status
            return result
        self.journal.record(fingerprint, "FAILED", "graph", now=self.now())
        lines.append(f"⚠️ Microsoft Graph : {receipt.outcome}. {receipt.error}")
        category = {
            "AUTH": "authentication",
            "PERMISSION": "permission_denied",
            "RATE_LIMIT": "api_rate_limit",
        }.get(receipt.outcome, "action_failed")
        result = _failed("outlook.send_email", receipt.error or receipt.outcome, category=category)
        result.stdout = "\n".join(lines)
        result.evidence["stop"] = receipt.outcome == "RATE_LIMIT"
        result.evidence["sent"] = False
        result.evidence["connector"] = "graph"
        return result

    def _send_applescript(self, expected, started, fingerprint, lines):
        lines.append("🤖 Étape 2 : Préparation du message dans Outlook.")
        probe = self.applescript.account_name()
        if probe.process_status == PROCESS_SUCCEEDED and (probe.stdout or "").strip():
            lines.append(email_progress("account_ok"))
        else:
            lines.append(email_progress("account_missing"))
        lines.append(email_progress("fields_ok"))
        lines.append("🤖 Étape 3 : Envoi du message après relecture des champs.")
        sent = self.applescript.send(expected["recipient"], expected["subject"], expected["body"])
        blob = f"{sent.stdout}\n{sent.stderr}\n{sent.error or ''}"
        if "GHOST_FIELD_MISMATCH" in blob or sent.process_status != PROCESS_SUCCEEDED:
            lines.append("🛑 Champs non conformes ou erreur AppleScript. Le message n'a pas été envoyé.")
            result = _failed(
                "outlook.send_email",
                sent.error or sent.stderr or sent.stdout or "AppleScript Outlook a échoué.",
                category="invalid_applescript_property" if "GHOST_FIELD_MISMATCH" in blob or "-2741" in blob else "action_failed",
            )
            result.stdout = "\n".join(lines + [sent.render()])
            result.evidence["sent"] = False
            result.evidence["connector"] = "applescript"
            result.evidence["stop"] = False
            if "not authorized" in blob.lower() or "-1743" in blob:
                result.evidence["category"] = "permission_denied"
                result.permission_denied = True
                result.stderr += (
                    "\nAutorise Python dans Réglages Système → Confidentialité → Automatisation, pour Microsoft Outlook."
                )
            return result
        self.journal.record(fingerprint, REQUEST_ACCEPTED, "applescript", now=self.now())
        lines.append("🤖 Étape 4 : Vérification indépendante.")
        lines.append(email_progress("search"))
        verdict = self._verify_messages(expected, started, REQUEST_ACCEPTED)
        if verdict.verified:
            self.journal.record(fingerprint, "VERIFIED", "applescript", now=self.now())
        elif verdict.status != "FAILED":
            self.journal.record(fingerprint, "INDETERMINATE", "applescript", now=self.now())
            verdict = verify_sent_email(
                expected,
                verdict.evidence.get("messages") or [],
                started_at=started,
                now=self.now(),
                receipt="TIMEOUT_AFTER_SEND" if verdict.status == UNVERIFIED else REQUEST_ACCEPTED,
            )
        lines.append(_verdict_text(verdict))
        result = _from_verdict("outlook.send_email", verdict, lines, connector="applescript")
        result.evidence["stop"] = True
        result.evidence["sent"] = True
        return result

    def _send_accessibility(self, expected, started, fingerprint, lines):
        lines.append("🤖 Étape 2 : Lecture des champs accessibles.")
        ok, reason, pressed, _nodes = self.accessibility.send(expected)
        if not pressed:
            result = _failed(
                "outlook.send_email",
                reason or "Champs Outlook non identifiés. Envoi annulé.",
                category="accessibility_permission" if "Accessibilité" in (reason or "") else "ui_element_not_found",
            )
            result.stdout = "\n".join(lines + [reason or ""])
            result.evidence["sent"] = False
            result.evidence["connector"] = "accessibility"
            result.evidence["stop"] = False
            return result
        self.journal.record(fingerprint, REQUEST_ACCEPTED, "accessibility", now=self.now())
        lines.append("🤖 Étape 3 : Bouton Envoyer actionné après lecture conforme des champs.")
        lines.append(email_progress("search"))
        verdict = self._verify_messages(expected, started, REQUEST_ACCEPTED)
        if not verdict.verified and verdict.status == UNVERIFIED:
            verdict = verify_sent_email(
                expected, [], started_at=started, now=self.now(), receipt="TIMEOUT_AFTER_SEND"
            )
            self.journal.record(fingerprint, "INDETERMINATE", "accessibility", now=self.now())
        elif verdict.verified:
            self.journal.record(fingerprint, "VERIFIED", "accessibility", now=self.now())
        lines.append(_verdict_text(verdict))
        result = _from_verdict("outlook.send_email", verdict, lines, connector="accessibility")
        result.evidence["stop"] = True
        return result

    def create_draft(self, args: dict):
        expected = _expected(args)
        lines = ["🤖 Préparation d'un brouillon Outlook.", email_progress("fields_ok")]
        if self.graph.configured() and self.graph.has_token():
            receipt = self.graph.create_draft(expected["recipient"], expected["subject"], expected["body"])
            if receipt.outcome == "REQUEST_ACCEPTED" and receipt.message_id:
                observed = self.graph.get_message(receipt.message_id)
                if observed and _draft_matches(observed, expected):
                    lines.append("✅ Brouillon relu. Il n'a pas été envoyé.")
                    return _ok_external("outlook.create_draft", "\n".join(lines), {"connector": "graph", "sent": False})
            if receipt.outcome == "TIMEOUT":
                return _indeterminate("outlook.create_draft", "Délai pendant la création du brouillon. Aucun second essai.")
        if not self.applescript.is_available():
            return _failed("outlook.create_draft", "Impossible de créer un brouillon Outlook.", category="app_not_responding")
        created = self.applescript.create_draft(expected["recipient"], expected["subject"], expected["body"])
        blob = f"{created.stdout}\n{created.stderr}\n{created.error or ''}"
        if created.process_status != PROCESS_SUCCEEDED or "DRAFT" not in created.stdout:
            return _failed("outlook.create_draft", created.error or blob or "Brouillon non créé.", category="invalid_applescript_property")
        try:
            drafts = self.applescript.list_folder("drafts")
        except Exception:
            drafts = []
        if any(_draft_matches(item, expected) for item in drafts):
            lines.append("✅ Brouillon retrouvé. Il n'a pas été envoyé.")
            return _ok_external("outlook.create_draft", "\n".join(lines), {"connector": "applescript", "sent": False})
        lines.append("⚠️ Le brouillon n'a pas été relu dans le dossier Brouillons.")
        return make_result(
            "outlook.create_draft",
            process_status=PROCESS_SUCCEEDED,
            action_status=ACTION_UNVERIFIED,
            stdout="\n".join(lines),
            evidence={"effect": "external", "task_type": "email_draft", "sent": False, "connector": "applescript"},
        )

    def search_emails(self, args: dict):
        query = str(args.get("query") or "")
        folder = str(args.get("folder") or "inbox")
        messages = self._search(query, folder)
        preview = _preview(messages)
        return _ok_read("outlook.search_emails", preview or "Aucun message correspondant.")

    def read_email(self, args: dict):
        query = args.get("query") or args.get("subject") or args.get("sender") or ""
        messages = self._search(str(query), "inbox")
        sender = str(args.get("sender") or "").lower()
        subject = str(args.get("subject") or "")
        picked = []
        for item in messages:
            if sender and sender not in str(item.get("sender") or "").lower():
                continue
            if subject and subject not in str(item.get("subject") or ""):
                continue
            picked.append(item)
        return _ok_read("outlook.read_email", _preview(picked or messages) or "Message introuvable.")

    def search_sent_items(self, args: dict):
        return self.verify_sent_email(args)

    def verify_sent_email(self, args: dict):
        expected = {
            "recipient": args.get("recipient") or args.get("to") or "",
            "subject": args.get("subject"),
            "body": args.get("body"),
        }
        started = float(args.get("not_before") or self.now() - 3600)
        verdict = self._verify_messages(expected, started, None)
        lines = [email_progress("search"), _verdict_text(verdict)]
        return _from_verdict("outlook.verify_sent_email", verdict, lines, connector="verify")

    def _verify_messages(self, expected, started, receipt):
        messages = []
        errors = []
        if self.graph.has_token():
            try:
                messages = self.graph.list_recent_sent()
            except Exception as exc:
                errors.append(str(exc)[:120])
        if not messages and self.applescript.is_available():
            try:
                messages = self.applescript.list_folder("sent items")
            except Exception as exc:
                errors.append(str(exc)[:120])
        verdict = verify_sent_email(
            expected,
            messages,
            started_at=started,
            now=self.now(),
            receipt=receipt,
        )
        if errors:
            verdict.evidence["search_errors"] = errors
        return verdict

    def _search(self, query: str, folder: str) -> list:
        if self.graph.has_token():
            try:
                messages = self.graph.list_recent_messages(folder)
            except Exception:
                messages = []
            if query:
                folded = query.lower()
                messages = [
                    item
                    for item in messages
                    if folded in str(item.get("subject") or "").lower()
                    or folded in str(item.get("body") or "").lower()
                    or folded in str(item.get("sender") or "").lower()
                ]
            return messages
        if self.applescript.is_available():
            prop = "inbox" if folder in {"inbox", "boîte de réception"} else "sent items"
            try:
                messages = self.applescript.list_folder(prop)
            except Exception:
                return []
            folded = query.lower()
            if not folded:
                return messages
            return [
                item
                for item in messages
                if folded in str(item.get("subject") or "").lower() or folded in str(item.get("body") or "").lower()
            ]
        return []


def _expected(args: dict) -> dict:
    return {
        "recipient": str(args.get("to") or args.get("recipient") or ""),
        "subject": args.get("subject"),
        "body": args.get("body"),
    }


def _draft_matches(message: dict, expected: dict) -> bool:
    recipients = [str(item).lower() for item in message.get("to") or []]
    if expected["recipient"].lower() not in recipients and recipients != [expected["recipient"].lower()]:
        return False
    return str(message.get("subject") or "") == str(expected.get("subject") or "")


def _preview(messages: list) -> str:
    rows = []
    for item in (messages or [])[:8]:
        subject = " ".join(str(item.get("subject") or "").split())[:80]
        sender = " ".join(str(item.get("sender") or ",".join(item.get("to") or [])).split())[:60]
        body = " ".join(str(item.get("body") or "").split())[:120]
        rows.append(f"- {sender} | {subject} | {body}")
    return "\n".join(rows)


def _verdict_text(verdict) -> str:
    if verdict.status == VERIFIED:
        return email_progress("found") + "\n" + verdict.summary
    if verdict.status == INDETERMINATE:
        return email_progress("missing")
    return verdict.summary or verdict.status


def _from_verdict(tool: str, verdict, lines, *, connector: str, process_status: str = ""):
    action = _ACTION_STATUS.get(verdict.status, ACTION_UNVERIFIED)
    if not process_status:
        if verdict.status == "FAILED":
            process_status = PROCESS_FAILED
        else:
            process_status = PROCESS_SUCCEEDED
    error = None
    if verdict.status == "FAILED":
        error = verdict.summary if str(verdict.summary).startswith("ERREUR") else f"ERREUR: {verdict.summary}"
    evidence = dict(verdict.evidence)
    evidence.update(
        {
            "effect": "external",
            "task_type": "email_send",
            "connector": connector,
            "verifier": "email_send",
            "stop": evidence.get("stop", True),
        }
    )
    return make_result(
        tool,
        process_status=process_status,
        action_status=action,
        stdout="\n".join([line for line in lines if line]),
        error=error,
        evidence=evidence,
    )


def _failed(tool: str, message: str, category: str = "action_failed"):
    text = message if str(message).startswith("ERREUR") else f"ERREUR: {message}"
    return make_result(
        tool,
        process_status=PROCESS_FAILED,
        action_status=ACTION_FAILED,
        error=text,
        evidence={"effect": "external", "category": category, "sent": False, "connector": "outlook"},
    )


def _ok_read(tool: str, text: str):
    return make_result(
        tool,
        process_status=PROCESS_SUCCEEDED,
        action_status=ACTION_VERIFIED,
        stdout=text,
        evidence={"effect": "read", "task_type": "email_read"},
    )


def _ok_external(tool: str, text: str, evidence: dict):
    payload = {"effect": "external", "task_type": "email_draft"}
    payload.update(evidence)
    return make_result(
        tool,
        process_status=PROCESS_SUCCEEDED,
        action_status=ACTION_VERIFIED,
        stdout=text,
        evidence=payload,
    )


def _indeterminate(tool: str, text: str):
    return make_result(
        tool,
        process_status=PROCESS_TIMEOUT,
        action_status=ACTION_INDETERMINATE,
        stdout=text,
        evidence={"effect": "external", "allow_resend": False, "stop": True},
    )
