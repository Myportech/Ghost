"""Calendrier : création puis relecture. L'écho de la création n'est pas une preuve."""

from __future__ import annotations

from datetime import datetime

from ghost.connectors.base import BaseConnector
from ghost.connectors.outlook.applescript_adapter import applescript_text
from ghost.core.execution_engine import run_subprocess
from ghost.core.results import (
    ACTION_FAILED,
    ACTION_INDETERMINATE,
    ACTION_UNVERIFIED,
    ACTION_VERIFIED,
    PROCESS_FAILED,
    PROCESS_SUCCEEDED,
    failed_result,
    make_result,
)
from ghost.core.verification_engine import parse_timestamp, verify_calendar_event


def build_create_script(title: str, start: float, end: float, calendar: str, notes: str) -> str:
    cal = applescript_text(calendar) if calendar else ""
    choose = f"set targetCal to calendar {cal}" if calendar else "set targetCal to first calendar"
    return f"""
set unixNow to (do shell script "date +%s") as integer
set startDate to (current date) + ({int(start)} - unixNow)
set endDate to (current date) + ({int(end)} - unixNow)
tell application "Calendar"
  {choose}
  tell targetCal
    make new event with properties {{summary:{applescript_text(title)}, start date:startDate, end date:endDate, description:{applescript_text(notes)}}}
  end tell
end tell
"""


def build_find_script(title: str, start: float, calendar: str) -> str:
    cal = f"calendar {applescript_text(calendar)}" if calendar else "first calendar"
    return f"""
set unixNow to (do shell script "date +%s") as integer
set startDate to (current date) + ({int(start)} - unixNow)
tell application "Calendar"
  tell {cal}
    set hits to (every event whose summary is {applescript_text(title)} and start date ≥ (startDate - 60) and start date ≤ (startDate + 60))
    set AppleScript's text item delimiters to linefeed
    set namesText to ""
    repeat with ev in hits
      set namesText to namesText & (summary of ev) & linefeed
    end repeat
    return namesText
  end tell
end tell
"""


class AppleScriptCalendar:
    def __init__(self, runner=None) -> None:
        self.runner = runner or _default_runner

    def create(self, spec: dict) -> None:
        start = parse_timestamp(spec.get("start"))
        end = parse_timestamp(spec.get("end"))
        if start is None or end is None:
            raise ValueError("horaire illisible")
        result = self.runner(
            build_create_script(
                spec.get("title") or "",
                start,
                end,
                spec.get("calendar") or "",
                spec.get("notes") or "",
            )
        )
        if result.process_status != PROCESS_SUCCEEDED:
            raise RuntimeError(result.error or result.stderr or "Calendar a refusé la création")

    def find(self, title: str, start: str, calendar: str = "") -> list:
        moment = parse_timestamp(start)
        if moment is None:
            return []
        result = self.runner(build_find_script(title, moment, calendar))
        if result.process_status != PROCESS_SUCCEEDED:
            raise RuntimeError(result.error or "lecture du calendrier impossible")
        return [
            {"title": line.strip(), "start": start}
            for line in (result.stdout or "").splitlines()
            if line.strip()
        ]


def _default_runner(script: str):
    return run_subprocess(tool="applescript", argv=["osascript", "-e", script], timeout=25)


class CalendarConnector(BaseConnector):
    name = "calendar"

    def __init__(self, backend=None) -> None:
        self.backend = backend or AppleScriptCalendar()

    def is_available(self) -> bool:
        return True

    def get_capabilities(self) -> list:
        return ["create_event", "verify_event"]

    def execute(self, action: str, arguments: dict):
        if action == "create_event":
            return self.create_event(arguments or {})
        if action == "verify_event":
            return self.verify_event(arguments or {})
        return failed_result("calendar." + action, f"Action calendrier inconnue : {action}")

    def verify(self, action: str, expected_state: dict):
        return self.verify_event(expected_state)

    def create_event(self, args: dict):
        spec = {
            "title": args.get("title"),
            "start": args.get("start"),
            "end": args.get("end"),
            "calendar": args.get("calendar") or "",
            "notes": args.get("notes") or "",
        }
        if parse_timestamp(spec["start"]) is None or parse_timestamp(spec["end"]) is None:
            return failed_result("calendar.create_event", "start et end doivent être des dates ISO 8601.")
        try:
            self.backend.create(spec)
        except Exception as exc:
            return failed_result("calendar.create_event", str(exc))
        return self._verify_created(spec)

    def verify_event(self, args: dict):
        spec = {"title": args.get("title"), "start": args.get("start"), "calendar": args.get("calendar") or ""}
        return self._verify_created(spec, created=False)

    def _verify_created(self, spec: dict, created: bool = True):
        try:
            found = self.backend.find(spec.get("title") or "", spec.get("start") or "", spec.get("calendar") or "")
        except Exception as exc:
            verdict_status = ACTION_INDETERMINATE if created else ACTION_UNVERIFIED
            return make_result(
                "calendar.create_event",
                process_status=PROCESS_SUCCEEDED if created else PROCESS_FAILED,
                action_status=verdict_status,
                stdout=f"L'événement n'a pas pu être relu : {exc}",
                evidence={"effect": "external", "task_type": "calendar_create", "allow_resend": False},
            )
        verdict = verify_calendar_event({"title": spec.get("title"), "start": spec.get("start")}, found)
        if created and verdict.status == "UNVERIFIED":
            verdict.status = "INDETERMINATE"
            verdict.verified = False
            verdict.evidence["allow_resend"] = False
            verdict.summary = "L'événement n'a pas été relu. Aucune seconde création automatique."
        status = {
            "VERIFIED": ACTION_VERIFIED,
            "FAILED": ACTION_FAILED,
            "UNVERIFIED": ACTION_UNVERIFIED,
            "INDETERMINATE": ACTION_INDETERMINATE,
        }[verdict.status]
        return make_result(
            "calendar.verify_event" if not created else "calendar.create_event",
            process_status=PROCESS_SUCCEEDED if status != ACTION_FAILED else PROCESS_FAILED,
            action_status=status,
            stdout=verdict.summary,
            error=None if status != ACTION_FAILED else f"ERREUR: {verdict.summary}",
            evidence={
                "effect": "external",
                "task_type": "calendar_create",
                "verifier": "calendar_create",
                "allow_resend": verdict.evidence.get("allow_resend", False),
                **{key: value for key, value in verdict.evidence.items() if key != "allow_resend"},
            },
        )


def iso_now_example() -> str:
    return datetime.now().replace(microsecond=0).isoformat()
