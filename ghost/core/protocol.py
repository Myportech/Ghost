"""Actions structurées. Les outils inconnus et les arguments hors schéma sont rejetés."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SHELL_TOOLS = {"bash", "python", "applescript", "done"}

SCHEMAS = {
    "bash": {"required": {"code": "str"}, "optional": {}},
    "python": {"required": {"code": "str"}, "optional": {}},
    "applescript": {"required": {"code": "str"}, "optional": {}},
    "done": {"required": {}, "optional": {"code": "str"}},
    "outlook.send_email": {"required": {"to": "email", "subject": "str", "body": "str"}, "optional": {}},
    "outlook.create_draft": {"required": {"to": "email", "subject": "str", "body": "str"}, "optional": {}},
    "outlook.search_emails": {"required": {"query": "str"}, "optional": {"folder": "str"}},
    "outlook.read_email": {"required": {}, "optional": {"query": "str", "subject": "str", "sender": "email"}},
    "outlook.search_sent_items": {
        "required": {"recipient": "email", "subject": "str"},
        "optional": {"body": "str"},
    },
    "outlook.verify_sent_email": {
        "required": {"recipient": "email", "subject": "str", "body": "str"},
        "optional": {},
    },
    "outlook.login": {"required": {}, "optional": {}},
    "finder.list": {"required": {"path": "str"}, "optional": {}},
    "finder.open": {"required": {"path": "str"}, "optional": {}},
    "finder.move": {"required": {"source": "str", "destination": "str"}, "optional": {}},
    "calendar.create_event": {
        "required": {"title": "str", "start": "str", "end": "str"},
        "optional": {"calendar": "str", "notes": "str"},
    },
    "calendar.verify_event": {"required": {"title": "str", "start": "str"}, "optional": {}},
}

_TOP_LEVEL = {"thought", "tool", "code", "args"}


@dataclass
class Action:
    thought: str
    tool: str
    code: str = ""
    args: dict = field(default_factory=dict)

    def exact_key(self) -> tuple:
        if self.tool in SHELL_TOOLS:
            return (self.tool, " ".join(self.code.split()))
        items = tuple(sorted((str(key), repr(value)) for key, value in self.args.items()))
        return (self.tool, items)


def _check_type(kind: str, value):
    if kind == "str":
        if not isinstance(value, str):
            return "doit être une chaîne"
        return ""
    if kind == "email":
        if not isinstance(value, str) or not _EMAIL.match(value.strip()):
            return "doit être une adresse e-mail"
        return ""
    return "type inconnu"


def parse_action(payload: dict) -> tuple:
    if not isinstance(payload, dict):
        return None, "Réponse invalide : un objet JSON est requis."
    extra = set(payload) - _TOP_LEVEL
    if extra:
        return None, "Champs refusés : " + ", ".join(sorted(extra))
    tool = str(payload.get("tool") or "").strip()
    if tool not in SCHEMAS:
        return None, f"Outil non autorisé : {tool or '(vide)'}."
    schema = SCHEMAS[tool]
    thought = str(payload.get("thought") or "")
    if tool in {"bash", "python", "applescript"}:
        if payload.get("args"):
            return None, f"{tool} n'accepte pas d'arguments structurés. Utilise code."
        code = payload.get("code")
        if not isinstance(code, str) or not code.strip():
            return None, "Paramètre manquant : code."
        return Action(thought, tool, code.strip(), {}), ""
    if tool == "done":
        code = payload.get("code") or ""
        if not isinstance(code, str):
            return None, "Le champ code de done doit être une chaîne."
        if payload.get("args"):
            return None, "done n'accepte pas args."
        return Action(thought, "done", code, {}), ""
    if payload.get("code"):
        return None, f"{tool} refuse le champ code : ce n'est pas une commande shell."
    args = payload.get("args")
    if args is None:
        return None, "Paramètre manquant : args."
    if not isinstance(args, dict):
        return None, "args doit être un objet."
    unknown = set(args) - set(schema["required"]) - set(schema["optional"])
    if unknown:
        return None, "Arguments refusés : " + ", ".join(sorted(unknown))
    clean = {}
    for name, kind in schema["required"].items():
        if name not in args:
            return None, f"Paramètre manquant : {name}."
        problem = _check_type(kind, args[name])
        if problem:
            return None, f"{name} {problem}."
        clean[name] = args[name].strip() if isinstance(args[name], str) else args[name]
    for name, kind in schema["optional"].items():
        if name not in args or args[name] in (None, ""):
            continue
        problem = _check_type(kind, args[name])
        if problem:
            return None, f"{name} {problem}."
        clean[name] = args[name].strip() if isinstance(args[name], str) else args[name]
    if tool == "outlook.read_email" and not any(clean.get(key) for key in ("query", "subject", "sender")):
        return None, "Paramètre manquant : query, subject ou sender."
    return Action(thought, tool, "", clean), ""


def public_args(action: Action) -> str:
    if action.tool in SHELL_TOOLS:
        return action.code
    shown = {}
    for key, value in action.args.items():
        if key == "body":
            shown[key] = f"({len(value)} caractères)"
        else:
            shown[key] = value
    parts = [f"{key}={value}" for key, value in shown.items()]
    return " ".join(parts)
