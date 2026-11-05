"""Finder : liste, ouverture et déplacement, avec vérification locale."""

from __future__ import annotations

import os
import shutil

from ghost.connectors.base import BaseConnector
from ghost.core.execution_engine import run_subprocess
from ghost.core.results import (
    ACTION_FAILED,
    ACTION_UNVERIFIED,
    ACTION_VERIFIED,
    PROCESS_FAILED,
    PROCESS_SUCCEEDED,
    failed_result,
    make_result,
)
from ghost.core.safety import forbidden_target, protected_folder, resolve_path
from ghost.core.verification_engine import verify_file_moved

def _list_with_finder(path: str) -> str:
    script = f'''
    tell application "Finder"
      set theRef to POSIX file "{path.replace('"', '')}" as alias
      set theItem to item theRef
      set AppleScript's text item delimiters to linefeed
      set dirNames to name of every folder of theItem
      set docNames to name of every file of theItem
      set dirsText to ""
      set docsText to ""
      try
        if dirNames is not {{}} then set dirsText to dirNames as text
      end try
      try
        if docNames is not {{}} then set docsText to docNames as text
      end try
      return "Dossiers:" & linefeed & dirsText & linefeed & "Fichiers:" & linefeed & docsText
    end tell
    '''
    result = run_subprocess(tool="applescript", argv=["osascript", "-e", script], timeout=12)
    if result.process_status != PROCESS_SUCCEEDED:
        raise RuntimeError(result.error or result.stderr or "Finder indisponible")
    return result.stdout


class FinderConnector(BaseConnector):
    name = "finder"

    def __init__(self, opener=None, probe=None) -> None:
        self.opener = opener
        self.probe = probe

    def is_available(self) -> bool:
        return True

    def get_capabilities(self) -> list:
        return ["list", "open", "move"]

    def execute(self, action: str, arguments: dict):
        if action == "list":
            return self.list_dir(arguments or {})
        if action == "open":
            return self.open_path(arguments or {})
        if action == "move":
            return self.move(arguments or {})
        return failed_result("finder." + action, f"Action Finder inconnue : {action}")

    def verify(self, action: str, expected_state: dict):
        if action == "move":
            verdict = verify_file_moved(expected_state)
            return make_result(
                "finder.move",
                process_status=PROCESS_SUCCEEDED if verdict.verified else PROCESS_FAILED,
                action_status=ACTION_VERIFIED if verdict.verified else ACTION_UNVERIFIED,
                stdout=verdict.summary,
                evidence=dict(verdict.evidence),
            )
        return self.execute(action, expected_state)

    def list_dir(self, args: dict):
        path = resolve_path(str(args.get("path") or ""))
        if not path:
            return failed_result("finder.list", "Paramètre manquant : path.")
        try:
            names = sorted(os.listdir(path))
            visible = [name for name in names if not name.startswith(".")]
            stdout = "\n".join(visible)
            return make_result(
                "finder.list",
                process_status=PROCESS_SUCCEEDED,
                action_status=ACTION_VERIFIED,
                stdout=stdout or "(dossier vide)",
                evidence={"effect": "read", "path": path, "count": len(visible)},
            )
        except PermissionError:
            if not protected_folder(path):
                return failed_result("finder.list", f"Permission refusée : {path}", permission_denied=True)
        except FileNotFoundError:
            return failed_result("finder.list", f"Dossier introuvable : {path}")
        except NotADirectoryError:
            return failed_result("finder.list", f"Pas un dossier : {path}")
        try:
            stdout = _list_with_finder(path)
        except Exception as exc:
            return failed_result("finder.list", str(exc), permission_denied=True)
        return make_result(
            "finder.list",
            process_status=PROCESS_SUCCEEDED,
            action_status=ACTION_VERIFIED,
            stdout=stdout or "(dossier vide)",
            evidence={"effect": "read", "path": path, "via": "finder"},
        )

    def open_path(self, args: dict):
        path = resolve_path(str(args.get("path") or ""))
        if not path or not os.path.exists(path):
            return failed_result("finder.open", f"Chemin introuvable : {path}")
        opener = self.opener or _default_open
        opened = opener(path)
        if isinstance(opened, str):
            ok = True
            detail = opened
        else:
            ok = opened.execution_success
            detail = opened.render()
        if not ok:
            return failed_result("finder.open", detail or "open a échoué")
        probe = self.probe if self.probe is not None else _default_probe
        observed = probe(path)
        if observed is True:
            status = ACTION_VERIFIED
            summary = f"Ouvert et observé : {path}"
        else:
            status = ACTION_UNVERIFIED
            summary = f"La commande open s'est terminée, mais l'ouverture de {path} n'est pas confirmée."
        return make_result(
            "finder.open",
            process_status=PROCESS_SUCCEEDED,
            action_status=status,
            stdout=summary,
            evidence={"effect": "external", "task_type": "app_open", "path": path, "observed": observed},
        )

    def move(self, args: dict):
        source = resolve_path(str(args.get("source") or ""))
        destination = resolve_path(str(args.get("destination") or ""))
        if forbidden_target(source) or forbidden_target(destination):
            return failed_result("finder.move", "Déplacement refusé par la politique de sécurité.")
        if not os.path.exists(source):
            verdict = verify_file_moved({"source": source, "destination": destination})
            return _move_result(verdict)
        final_destination = destination
        if os.path.isdir(destination):
            final_destination = os.path.join(destination, os.path.basename(source))
        try:
            shutil.move(source, final_destination)
        except PermissionError:
            return failed_result("finder.move", f"Permission refusée pour {source}", permission_denied=True)
        except OSError as exc:
            return failed_result("finder.move", str(exc))
        verdict = verify_file_moved({"source": source, "destination": final_destination})
        return _move_result(verdict)


def _move_result(verdict):
    status = {
        "VERIFIED": ACTION_VERIFIED,
        "FAILED": ACTION_FAILED,
        "UNVERIFIED": ACTION_UNVERIFIED,
        "INDETERMINATE": ACTION_UNVERIFIED,
    }.get(verdict.status, ACTION_UNVERIFIED)
    return make_result(
        "finder.move",
        process_status=PROCESS_SUCCEEDED if status != ACTION_FAILED else PROCESS_FAILED,
        action_status=status,
        stdout=verdict.summary,
        error=None if status != ACTION_FAILED else f"ERREUR: {verdict.summary}",
        evidence={
            "effect": "external",
            "task_type": "file_move",
            "verifier": "file_move",
            **verdict.evidence,
        },
    )


def _default_open(path: str):
    return run_subprocess(tool="bash", argv=["open", path], timeout=10)


def _default_probe(path: str):
    script = '''
    tell application "Finder"
      if (count of Finder windows) is 0 then return ""
      return POSIX path of (target of front window as alias)
    end tell
    '''
    result = run_subprocess(tool="applescript", argv=["osascript", "-e", script], timeout=8)
    if result.process_status != PROCESS_SUCCEEDED:
        return None
    front = (result.stdout or "").strip().rstrip("/")
    target = path.rstrip("/")
    if not front:
        return False
    return front == target or front.startswith(target + "/")
