import ast
from pathlib import Path

import pytest

from ghost.core.execution_engine import assess_process, blocked_blind_input, run_subprocess
from ghost.core.results import (
    ACTION_FAILED,
    ACTION_INDETERMINATE,
    ACTION_UNVERIFIED,
    PROCESS_FAILED,
    PROCESS_INTERRUPTED,
    PROCESS_SUCCEEDED,
    PROCESS_TIMEOUT,
)

ROOT = Path(__file__).resolve().parents[1]


def test_empty_success_is_not_business_success():
    result = run_subprocess(tool="bash", shell_command="true", timeout=5)
    assert result.process_status == PROCESS_SUCCEEDED
    assert result.execution_success is True
    assert result.action_status == ACTION_UNVERIFIED
    assert result.action_verified is False
    rendered = result.render()
    assert "Exécuté avec succès" not in rendered
    assert "ACTION_UNVERIFIED" in rendered
    assert "(aucune sortie)" in rendered


def test_nonzero_exit_is_process_failure():
    result = run_subprocess(tool="bash", shell_command="echo non >&2; exit 3", timeout=5)
    assert result.return_code == 3
    assert result.process_status == PROCESS_FAILED
    assert result.action_status == ACTION_FAILED
    assert result.action_verified is False
    assert "ERREUR" in result.render()
    assert "non" in result.stderr


def test_applescript_error_is_not_success():
    result = run_subprocess(
        tool="applescript",
        argv=["osascript", "-e", 'error "boom"'],
        timeout=8,
    )
    assert result.execution_success is False
    assert result.action_status == ACTION_FAILED
    assert result.action_verified is False


def test_timeout_is_indeterminate_not_success():
    result = run_subprocess(tool="bash", shell_command="sleep 2", timeout=0.2)
    assert result.process_status == PROCESS_TIMEOUT
    assert result.action_status == ACTION_INDETERMINATE
    assert result.action_verified is False
    assert result.execution_success is False


def test_permission_denied_is_not_success():
    judged = assess_process(1, "", "Operation not permitted")
    assert judged["permission_denied"] is True
    assert judged["process_status"] == PROCESS_FAILED
    assert judged["action_status"] == ACTION_FAILED
    assert judged["execution_success"] is False


def test_interrupted_process_is_not_success():
    judged = assess_process(-15, "", "")
    assert judged["process_status"] == PROCESS_INTERRUPTED
    assert judged["interrupted"] is True
    assert judged["action_status"] == ACTION_INDETERMINATE


def test_real_interrupt_signal():
    result = run_subprocess(
        tool="python",
        argv=["python3", "-c", "import os; os.kill(os.getpid(), 15)"],
        timeout=5,
    )
    assert result.interrupted or result.process_status == PROCESS_INTERRUPTED
    assert result.action_verified is False


def test_blind_send_shortcut_is_blocked():
    blocked = blocked_blind_input('tell application "System Events" to keystroke "d" using command down')
    assert "refusé" in blocked
    result = run_subprocess(
        tool="applescript",
        shell_command='keystroke "d" using command down',
        timeout=2,
    )
    assert result.action_status == ACTION_FAILED
    assert result.action_verified is False


def test_inline_source_has_no_hardcoded_secret_and_keeps_shell_tools():
    """Check the macOS host only when its source is a regular file in the repo.

    Commit 661747a stored inline.py as a symlink to
    /Users/romainrivierre/Library/Application Support/inline/inline.py.
    That target is not in Git and was not found on this machine. A replacement
    module is not generated here. When the real file is added as a normal file,
    the assertions below apply.
    """
    path = ROOT / "inline.py"
    if path.is_symlink() or not path.is_file():
        pytest.skip(
            "inline.py source was not recovered; Git only had a machine-local symlink"
        )
    source = path.read_text(encoding="utf-8")
    assert "gsk_" not in source
    assert "GROQ_API_KEY" in source
    assert "def execute_os_tool" in source
    assert "def run_control_agent" in source
    assert "def ghost_window_session" in source
    assert "NSWindow" in source
    assert "Tâche terminée !" not in source
    assert "[Exécuté avec succès]" not in source
    ast.parse(source)
