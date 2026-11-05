"""Registre des outils réellement implémentés."""

from __future__ import annotations

from ghost.connectors.calendar.adapter import CalendarConnector
from ghost.connectors.finder.adapter import FinderConnector
from ghost.connectors.outlook.adapter import OutlookConnector
from ghost.core.results import failed_result


class ToolRegistry:
    def __init__(self) -> None:
        self._handlers = {}

    def register(self, name: str, handler) -> None:
        self._handlers[name] = handler

    def names(self) -> set:
        return set(self._handlers)

    def execute(self, name: str, args: dict):
        handler = self._handlers.get(name)
        if handler is None:
            return failed_result(name or "inconnu", f"Outil non implémenté : {name}")
        try:
            return handler(args or {})
        except Exception as exc:
            return failed_result(name, str(exc))


def build_registry(outlook=None, finder=None, calendar=None) -> ToolRegistry:
    outlook = outlook or OutlookConnector()
    finder = finder or FinderConnector()
    calendar = calendar or CalendarConnector()
    registry = ToolRegistry()
    registry.register("outlook.send_email", outlook.send_email)
    registry.register("outlook.create_draft", outlook.create_draft)
    registry.register("outlook.search_emails", outlook.search_emails)
    registry.register("outlook.read_email", outlook.read_email)
    registry.register("outlook.search_sent_items", outlook.search_sent_items)
    registry.register("outlook.verify_sent_email", outlook.verify_sent_email)
    registry.register("outlook.login", outlook.login)
    registry.register("finder.list", finder.list_dir)
    registry.register("finder.open", finder.open_path)
    registry.register("finder.move", finder.move)
    registry.register("calendar.create_event", calendar.create_event)
    registry.register("calendar.verify_event", calendar.verify_event)
    return registry
