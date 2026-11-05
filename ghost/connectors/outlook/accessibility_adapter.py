"""Accessibilité Outlook : aucune frappe, aucun envoi si les champs ne sont pas relus."""

from __future__ import annotations

import re
from typing import Optional

_TO = ("destinataire", "to")
_SUBJECT = ("objet", "subject")
_BODY = ("corps", "message", "body")
_SEND = ("envoyer", "send")


def _label(node: dict) -> str:
    parts = [
        node.get("title") or "",
        node.get("description") or "",
        node.get("placeholder") or "",
        node.get("role_description") or "",
    ]
    return " ".join(str(part) for part in parts).lower()


def _has_word(label: str, words) -> bool:
    for word in words:
        if re.search(rf"(^|[^a-zà-ÿ]){re.escape(word)}([^a-zà-ÿ]|$)", label):
            return True
    return False


def plan_compose(nodes, expected: dict) -> dict:
    """Associe les champs par leur libellé. L'ordre visuel ne suffit pas."""
    assigned = {"to": None, "subject": None, "body": None}
    button = None
    for node in nodes or []:
        label = _label(node)
        role = str(node.get("role") or "")
        if "button" in role.lower() and _has_word(label, _SEND):
            button = node
            continue
        if "button" in role.lower():
            continue
        hits = []
        if _has_word(label, _TO):
            hits.append("to")
        if _has_word(label, _SUBJECT):
            hits.append("subject")
        if _has_word(label, _BODY):
            hits.append("body")
        if len(hits) != 1:
            continue
        key = hits[0]
        if assigned[key] is not None:
            return {"ok": False, "reason": f"champ ambigu : {key}", "button": None}
        assigned[key] = node
    missing = [name for name, node in assigned.items() if node is None]
    if missing or button is None:
        reason = "champ introuvable : " + ", ".join(missing or ["envoyer"])
        return {"ok": False, "reason": reason, "button": None, **assigned}
    return {"ok": True, "reason": "", "button": button, **assigned}


def _write(node: dict, value: str) -> str:
    setter = node.get("set_value")
    if callable(setter):
        setter(value)
    else:
        node["value"] = value
    return str(node.get("value") if "value" in node else "")


def apply_plan(plan: dict, expected: dict) -> tuple:
    """Écrit, relit, puis seulement alors presse Envoyer."""
    if not plan.get("ok"):
        return False, plan.get("reason") or "champ introuvable", False
    wanted = {
        "to": expected.get("recipient") or expected.get("to") or "",
        "subject": expected.get("subject") or "",
        "body": expected.get("body") or "",
    }
    for key, value in wanted.items():
        current = _write(plan[key], value).replace("\r", " ").strip()
        if " ".join(current.split()) != " ".join(str(value).split()):
            return False, f"lecture retour non conforme : {key}", False
    button = plan.get("button") or {}
    press = button.get("press")
    if not callable(press):
        return False, "bouton Envoyer sans action accessible", False
    press()
    button["pressed"] = True
    return True, "", True


def ax_trusted() -> bool:
    try:
        from ApplicationServices import AXIsProcessTrusted
    except Exception:
        return False
    try:
        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def _ax_value(element, name: str):
    try:
        from ApplicationServices import AXUIElementCopyAttributeValue, kAXErrorSuccess
    except Exception:
        return None
    try:
        result = AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return None
    if isinstance(result, tuple):
        if len(result) >= 2 and result[0] not in (0, None, kAXErrorSuccess):
            return None
        return result[-1]
    return result


def collect_outlook_nodes(max_nodes: int = 250, max_depth: int = 6) -> list:
    """Parcourt l'arbre d'accessibilité d'Outlook. N'appuie sur rien."""
    if not ax_trusted():
        raise PermissionError("accessibility")
    try:
        from AppKit import NSWorkspace
        from ApplicationServices import AXUIElementCreateApplication
    except Exception as exc:
        raise RuntimeError(f"Accessibilité indisponible : {exc}") from exc
    pid = None
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if str(app.bundleIdentifier() or "") == "com.microsoft.Outlook":
            pid = app.processIdentifier()
            break
    if pid is None:
        return []
    root = AXUIElementCreateApplication(pid)
    found = []

    def walk(element, depth: int) -> None:
        if element is None or depth > max_depth or len(found) >= max_nodes:
            return
        role = str(_ax_value(element, "AXRole") or "")
        node = {
            "role": role,
            "title": str(_ax_value(element, "AXTitle") or ""),
            "description": str(_ax_value(element, "AXDescription") or ""),
            "placeholder": str(_ax_value(element, "AXPlaceholderValue") or ""),
            "role_description": str(_ax_value(element, "AXRoleDescription") or ""),
            "value": str(_ax_value(element, "AXValue") or ""),
            "_element": element,
        }

        def set_value(text, target=element, box=node):
            from ApplicationServices import AXUIElementSetAttributeValue

            AXUIElementSetAttributeValue(target, "AXValue", text)
            box["value"] = str(_ax_value(target, "AXValue") or "")

        def press(target=element, box=node):
            from ApplicationServices import AXUIElementPerformAction

            AXUIElementPerformAction(target, "AXPress")
            box["pressed"] = True

        node["set_value"] = set_value
        node["press"] = press
        found.append(node)
        children = _ax_value(element, "AXChildren") or []
        try:
            iterator = list(children)
        except TypeError:
            iterator = []
        for child in iterator:
            walk(child, depth + 1)

    walk(root, 0)
    return found


class AccessibilityOutlook:
    def __init__(self, nodes_provider=None, trusted: Optional[bool] = None) -> None:
        self.nodes_provider = nodes_provider
        self._trusted = trusted

    def is_available(self) -> bool:
        if self.nodes_provider is not None:
            return True
        if self._trusted is not None:
            return self._trusted
        return ax_trusted()

    def send(self, expected: dict) -> tuple:
        if self.nodes_provider is None and not self.is_available():
            return False, "Permission Accessibilité absente.", False, []
        try:
            nodes = self.nodes_provider() if self.nodes_provider else collect_outlook_nodes()
        except PermissionError:
            return False, "Permission Accessibilité absente.", False, []
        except Exception as exc:
            return False, f"Accessibilité indisponible : {exc}", False, []
        plan = plan_compose(nodes, expected)
        pressed_before = bool((plan.get("button") or {}).get("pressed"))
        ok, reason, pressed = apply_plan(plan, expected)
        if pressed_before:
            pressed = False
        return ok, reason, pressed, nodes
