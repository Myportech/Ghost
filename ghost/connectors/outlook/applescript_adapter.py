"""AppleScript Outlook, calé sur le dictionnaire Outlook.sdef installé.

Le message n'est envoyé qu'après relecture du destinataire, du sujet et du corps.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from ghost.core.execution_engine import run_subprocess
from ghost.core.results import PROCESS_SUCCEEDED, ExecutionResult

_OUTLOOK = "/Applications/Microsoft Outlook.app"
_UNIT = "\x1f"
_RECORD = "\x1e"


def applescript_text(value: str) -> str:
    parts = str(value).split("\n")
    quoted = []
    for part in parts:
        safe = part.replace("\\", "\\\\").replace('"', '\\"')
        quoted.append('"' + safe + '"')
    return " & return & ".join(quoted) if quoted else '""'


def outlook_installed() -> bool:
    return os.path.isdir(_OUTLOOK)


def build_send_script(recipient: str, subject: str, body: str) -> str:
    addr = applescript_text(recipient)
    subj = applescript_text(subject)
    content = applescript_text(body)
    return f"""tell application "Microsoft Outlook"
  set newMsg to make new outgoing message with properties {{subject:{subj}, plain text content:{content}}}
  make new to recipient at newMsg with properties {{email address:{{address:{addr}}}}}
  set gotSubject to subject of newMsg
  set gotBody to plain text content of newMsg
  set gotAddr to address of email address of to recipient 1 of newMsg
  if gotSubject is not {subj} then error "GHOST_FIELD_MISMATCH subject"
  if gotBody is not {content} then error "GHOST_FIELD_MISMATCH body"
  if gotAddr is not {addr} then error "GHOST_FIELD_MISMATCH recipient"
  send newMsg
  return "SENT"
end tell
"""


def build_draft_script(recipient: str, subject: str, body: str) -> str:
    addr = applescript_text(recipient)
    subj = applescript_text(subject)
    content = applescript_text(body)
    return f"""tell application "Microsoft Outlook"
  set newMsg to make new outgoing message with properties {{subject:{subj}, plain text content:{content}}}
  make new to recipient at newMsg with properties {{email address:{{address:{addr}}}}}
  set gotSubject to subject of newMsg
  set gotBody to plain text content of newMsg
  set gotAddr to address of email address of to recipient 1 of newMsg
  if gotSubject is not {subj} then error "GHOST_FIELD_MISMATCH subject"
  if gotBody is not {content} then error "GHOST_FIELD_MISMATCH body"
  if gotAddr is not {addr} then error "GHOST_FIELD_MISMATCH recipient"
  return "DRAFT"
end tell
"""


def build_folder_script(folder_property: str) -> str:
    prop = folder_property if folder_property in {"sent items", "drafts", "inbox"} else "sent items"
    return f"""tell application "Microsoft Outlook"
  set outMsgs to outgoing messages of {prop}
  set n to count of outMsgs
  set scanned to 0
  set collected to ""
  repeat with i from n to 1 by -1
    if scanned ≥ 20 then exit repeat
    set scanned to scanned + 1
    set m to item i of outMsgs
    set addrText to ""
    try
      set addrText to address of email address of to recipient 1 of m
    end try
    set subjText to subject of m
    set bodyText to plain text content of m
    set AppleScript's text item delimiters to {{{applescript_text(_UNIT)}, {applescript_text(_RECORD)}}}
    set bodyText to text items of bodyText as text
    set AppleScript's text item delimiters to ""
    set ts to time sent of m
    set stamp to ((year of ts) as integer) & "-" & ((month of ts) as integer) & "-" & ((day of ts) as integer) & " " & ((hours of ts) as integer) & ":" & ((minutes of ts) as integer) & ":" & ((seconds of ts) as integer)
    set collected to collected & addrText & "{_UNIT}" & subjText & "{_UNIT}" & bodyText & "{_UNIT}" & stamp & "{_RECORD}"
  end repeat
  return collected
end tell
"""


def build_account_script() -> str:
    return 'tell application "Microsoft Outlook" to return name of default account'


def parse_folder_payload(payload: str) -> list:
    messages = []
    for block in str(payload or "").split(_RECORD):
        parts = block.split(_UNIT)
        if len(parts) < 4:
            continue
        address, subject, body, stamp = parts[0].strip(), parts[1], parts[2], parts[3].strip()
        messages.append(
            {
                "to": [address] if address else [],
                "subject": subject,
                "body": body,
                "sent_at": _parse_stamp(stamp),
            }
        )
    return messages


def _parse_stamp(stamp: str):
    match = re.search(r"(\d+)-(\d+)-(\d+) (\d+):(\d+):(\d+)", stamp or "")
    if not match:
        return None
    from datetime import datetime

    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second).timestamp()
    except ValueError:
        return None


def default_runner(script: str, timeout: float = 30) -> ExecutionResult:
    return run_subprocess(
        tool="applescript",
        argv=["osascript", "-e", script],
        timeout=timeout,
    )


class AppleScriptOutlook:
    def __init__(self, runner=None, installed: Optional[bool] = None) -> None:
        self.runner = runner or default_runner
        self.installed = outlook_installed() if installed is None else installed

    def is_available(self) -> bool:
        return bool(self.installed)

    def account_name(self) -> ExecutionResult:
        return self.runner(build_account_script(), 12)

    def send(self, recipient: str, subject: str, body: str) -> ExecutionResult:
        return self.runner(build_send_script(recipient, subject, body), 40)

    def create_draft(self, recipient: str, subject: str, body: str) -> ExecutionResult:
        return self.runner(build_draft_script(recipient, subject, body), 30)

    def list_folder(self, folder_property: str) -> list:
        result = self.runner(build_folder_script(folder_property), 30)
        if result.process_status != PROCESS_SUCCEEDED:
            raise RuntimeError(result.error or result.stderr or "lecture Outlook impossible")
        return parse_folder_payload(result.stdout)
