import os

import pytest

from ghost.connectors.calendar.adapter import CalendarConnector, build_create_script
from ghost.connectors.finder.adapter import FinderConnector
from ghost.connectors.registry import build_registry
from ghost.core.memory_policy import fact_allowed, reliability_entry, summary_for_memory
from ghost.core.operation_journal import OperationJournal
from ghost.core.orchestrator import ControlSession
from ghost.core.prompt import implemented_tool_names, system_prompt
from ghost.core.protocol import SCHEMAS, parse_action
from ghost.core.recovery_engine import RecoveryEngine
from ghost.core.results import (
    ACTION_FAILED,
    ACTION_INDETERMINATE,
    ACTION_UNVERIFIED,
    PROCESS_FAILED,
    PROCESS_SUCCEEDED,
    make_result,
)
from ghost.core.task_manager import classify_task, extract_email_expected


def _ok(tool="bash"):
    return make_result(tool, process_status=PROCESS_SUCCEEDED, action_status=ACTION_UNVERIFIED, return_code=0)


def _fail(tool="bash", text="syntax error"):
    return make_result(
        tool,
        process_status=PROCESS_FAILED,
        action_status=ACTION_FAILED,
        return_code=1,
        error=f"ERREUR: {text}",
        stderr=text,
    )


def test_two_similar_errors_switch_strategy():
    engine = RecoveryEngine()
    first = engine.observe(_fail(), "applescript", external=True)
    second = engine.observe(_fail(), "applescript", external=True)
    assert first.switch_strategy is False
    assert second.switch_strategy is True
    assert "stratégie" in second.message


def test_uncertain_external_action_is_not_retried():
    engine = RecoveryEngine()
    result = make_result(
        "outlook.send_email",
        process_status=PROCESS_SUCCEEDED,
        action_status=ACTION_INDETERMINATE,
        evidence={"category": "unknown_result"},
    )
    decision = engine.observe(result, "outlook.send_email", external=True)
    assert decision.retry is False
    assert decision.stop is True
    assert engine.exhausted is True


def test_read_action_can_be_retried():
    engine = RecoveryEngine()
    result = make_result(
        "outlook.search_emails",
        process_status=PROCESS_FAILED,
        action_status=ACTION_FAILED,
        error="ERREUR: network is unreachable",
    )
    decision = engine.observe(result, "outlook.search_emails", external=False)
    assert decision.category == "network_timeout"
    assert decision.stop is False


def test_journal_blocks_identical_uncertain_send(tmp_path):
    journal = OperationJournal(tmp_path / "ops.json", ttl=100)
    journal.record("abc", "INDETERMINATE", "graph", now=50)
    assert journal.has_blocking("abc", now=60) is True
    assert journal.has_blocking("abc", now=1000) is False
    journal.record("other", "FAILED", "graph", now=60)
    assert journal.has_blocking("other", now=70) is False
    saved = (tmp_path / "ops.json").read_text()
    assert "corps" not in saved
    assert "@" not in saved


def test_protocol_accepts_structured_mail_and_rejects_shell_bypass():
    action, error = parse_action(
        {
            "thought": "envoyer",
            "tool": "outlook.send_email",
            "args": {"to": "oscar.beaucamp@skema.edu", "subject": "s", "body": "b"},
        }
    )
    assert error == ""
    assert action.tool == "outlook.send_email"
    rejected, problem = parse_action(
        {"tool": "outlook.send_email", "code": "osascript -e 'do shell script \"rm -rf /\"'"}
    )
    assert rejected is None
    assert "code" in problem
    unknown, problem = parse_action({"tool": "outlook.delete_mailbox", "args": {}})
    assert unknown is None
    missing, problem = parse_action({"tool": "outlook.send_email", "args": {"subject": "s"}})
    assert missing is None
    assert "to" in problem


def test_prompt_lists_only_real_tools_and_reliability_rules():
    prompt = system_prompt()
    for index in range(1, 13):
        assert f"{index}." in prompt
    assert "Une commande exécutée n'est pas une tâche accomplie." in prompt
    assert "La décision de réussite appartient au moteur Python" in prompt
    for name in implemented_tool_names():
        assert name in prompt
    assert "outlook.delete_mailbox" not in prompt
    assert set(implemented_tool_names()) == set(SCHEMAS)


def test_catalog_matches_registry_names():
    registry = build_registry(
        outlook=_stub_outlook(),
        finder=FinderConnector(opener=lambda path: _ok("finder.open"), probe=lambda path: True),
        calendar=_stub_calendar(),
    )
    for name in SCHEMAS:
        if name in {"bash", "python", "applescript", "done"}:
            continue
        assert name in registry.names()


def test_done_without_verification_is_rejected():
    goal = (
        "Ouvrir Outlook et envoyer un mail à oscar.beaucamp@skema.edu "
        "Sujet : dassaut c'est dassaut Corps : dassaut c'est dassaut"
    )
    session = ControlSession(goal, email_checker=lambda expected, started: _unverified_email())
    decision = session.authorize_done()
    assert decision.allow is False
    expected = extract_email_expected(goal)
    assert expected["recipient"] == "oscar.beaucamp@skema.edu"
    assert expected["subject"] == "dassaut c'est dassaut"
    assert expected["body"] == "dassaut c'est dassaut"


def test_script_without_effect_is_unverified_not_completed(tmp_path):
    target = tmp_path / "absent.txt"
    session = ControlSession(f"crée le fichier {target}")
    action, error = session.parse({"thought": "créer", "tool": "bash", "code": "true"})
    assert error == ""
    result = session.execute(action, lambda tool, code: _ok(tool))
    assert result.action_verified is False
    assert session.snapshot()["status"] == "UNVERIFIED"
    first = session.authorize_done()
    assert first.allow is False
    second = session.authorize_done()
    assert second.allow is True
    assert second.status == "UNVERIFIED"
    text = session.final_text("Tâche terminée ! Le fichier est créé.", second)
    assert "NON VÉRIFIÉ" in text
    assert "Tâche terminée !" not in text
    assert second.notify != "Ghost Control : Tâche terminée !"


def test_script_error_is_failed():
    session = ControlSession("crée le fichier /tmp/ghost-failure-case")
    action, _error = session.parse({"tool": "bash", "code": "false"})
    session.execute(action, lambda tool, code: _fail(tool, "code de retour 1"))
    assert session.snapshot()["status"] == "FAILED"
    decision = session.authorize_done()
    assert decision.allow is False
    session.recovery.exhausted = True
    closed = session.authorize_done()
    assert closed.allow is True
    assert closed.status == "FAILED"


def test_verified_file_can_complete(tmp_path):
    target = tmp_path / "fait.txt"
    session = ControlSession(f"crée le fichier {target} fichier")
    target.write_text("ok")
    action, _error = session.parse({"tool": "bash", "code": f"touch {target}"})
    session.execute(action, lambda tool, code: _ok(tool))
    assert session.snapshot()["verified"] is True
    decision = session.authorize_done()
    assert decision.allow is True
    assert decision.status == "VERIFIED"


def test_finder_move_roundtrip(tmp_path):
    source = tmp_path / "a.txt"
    dest_dir = tmp_path / "dest"
    source.write_text("contenu")
    dest_dir.mkdir()
    connector = FinderConnector()
    result = connector.move({"source": str(source), "destination": str(dest_dir)})
    assert result.action_status == "ACTION_VERIFIED"
    assert not source.exists()
    assert (dest_dir / "a.txt").read_text() == "contenu"


def test_finder_refuses_root_move(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("x")
    result = FinderConnector().move({"source": str(source), "destination": "/"})
    assert result.action_status == "ACTION_FAILED"
    assert source.exists()


def test_calendar_create_then_reread_and_missing_reread_is_indeterminate():
    class MemoryCalendar:
        def __init__(self, store=True):
            self.store = store
            self.events = []
            self.creates = 0

        def create(self, spec):
            self.creates += 1
            if self.store:
                self.events.append({"title": spec["title"], "start": spec["start"]})

        def find(self, title, start, calendar=""):
            return [event for event in self.events if event["title"] == title]

    good = MemoryCalendar(True)
    created = CalendarConnector(good).create_event(
        {"title": "Point", "start": "2026-11-05T10:00:00", "end": "2026-11-05T11:00:00"}
    )
    assert created.action_verified is True
    assert good.creates == 1
    missing = MemoryCalendar(False)
    uncertain = CalendarConnector(missing).create_event(
        {"title": "Point", "start": "2026-11-05T10:00:00", "end": "2026-11-05T11:00:00"}
    )
    assert uncertain.action_status == "ACTION_INDETERMINATE"
    assert uncertain.evidence.get("allow_resend") is False
    script = build_create_script("Point", 1, 2, "", "")
    assert "keystroke" not in script.lower()


def test_memory_does_not_store_unverified_success():
    assert fact_allowed("Ghost a envoyé le mail", verified=False) is False
    assert fact_allowed("Le dossier Stats est sur le Bureau", verified=False) is True
    entry = reliability_entry(
        {
            "goal": "envoyer à oscar.beaucamp@skema.edu le corps secret",
            "connector": "graph",
            "tool": "outlook.send_email",
            "status": "INDETERMINATE",
            "verified": False,
        }
    )
    assert entry["verified"] is False
    assert "oscar" not in str(entry)
    assert "secret" not in str(entry)
    summary = summary_for_memory("Bearer abcdef corps secret", entry)
    assert "abcdef" not in summary
    assert "secret" not in summary


def test_informational_task_does_not_require_action_verification():
    spec = classify_task("liste les fichiers du bureau")
    assert spec.verification_required is False
    session = ControlSession("liste les fichiers du bureau")
    action, error = session.parse({"tool": "bash", "code": "echo Bureau"})
    assert error == ""
    session.execute(
        action,
        lambda tool, code: make_result(
            tool,
            process_status=PROCESS_SUCCEEDED,
            action_status=ACTION_UNVERIFIED,
            stdout="Bureau",
            return_code=0,
        ),
    )
    decision = session.authorize_done()
    assert decision.allow is True
    assert decision.status == "COMPLETED"


def test_live_outlook_send_is_opt_in():
    if os.environ.get("GHOST_OUTLOOK_LIVE") != "1":
        pytest.skip("envoi réel désactivé")
    pytest.fail("Le test réel ne doit pas partir sans un scénario explicitement revu.")


def _unverified_email():
    from ghost.core.verification_engine import VerificationResult

    return VerificationResult("email_send", "UNVERIFIED", False, {}, ["aucune preuve"], "aucune preuve")


def _stub_outlook():
    class Outlook:
        def send_email(self, args):
            return _ok("outlook.send_email")

        def create_draft(self, args):
            return _ok("outlook.create_draft")

        def search_emails(self, args):
            return _ok("outlook.search_emails")

        def read_email(self, args):
            return _ok("outlook.read_email")

        def search_sent_items(self, args):
            return _ok("outlook.search_sent_items")

        def verify_sent_email(self, args):
            return _ok("outlook.verify_sent_email")

        def login(self, args):
            return _ok("outlook.login")

    return Outlook()


def _stub_calendar():
    class Calendar:
        def create_event(self, args):
            return _ok("calendar.create_event")

        def verify_event(self, args):
            return _ok("calendar.verify_event")

    return Calendar()
