"""Contrat commun des connecteurs d'applications."""

from __future__ import annotations


class BaseConnector:
    name = "base"

    def is_available(self) -> bool:
        return False

    def get_capabilities(self) -> list:
        return []

    def execute(self, action: str, arguments: dict):
        raise NotImplementedError(action)

    def verify(self, action: str, expected_state: dict):
        raise NotImplementedError(action)

    def get_status(self) -> dict:
        return {"name": self.name, "available": self.is_available()}
