"""Présence d'une application, sans conclure qu'une action a réussi."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def app_bundle_exists(name: str) -> bool:
    slug = (name or "").strip()
    if not slug:
        return False
    roots = [Path("/Applications"), Path.home() / "Applications"]
    for root in roots:
        if not root.is_dir():
            continue
        for app in root.glob("*.app"):
            if app.stem.lower() == slug.lower():
                return True
    return False


def command_exists(name: str) -> bool:
    return bool(shutil.which((name or "").strip()))


def app_or_command_present(name: str) -> bool:
    return app_bundle_exists(name) or command_exists(name)
