"""Garde-fous partagés par les connecteurs. Ne remplace pas ceux de bash."""

from __future__ import annotations

import os

_FORBIDDEN = {
    "/",
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/var",
    "/etc",
    "/Library",
    "/Applications",
    "/Users",
    "/private",
    "/Volumes",
}


def resolve_path(raw: str) -> str:
    home = os.path.expanduser("~")
    text = str(raw or "").replace("${HOME}", home).replace("$HOME", home)
    return os.path.abspath(os.path.expanduser(text))


def forbidden_target(raw: str) -> bool:
    if not raw or any(ch in raw for ch in "*?["):
        return True
    path = resolve_path(raw)
    home = os.path.abspath(os.path.expanduser("~"))
    if path in {home, "/"} or home.startswith(path + os.sep):
        return True
    if path in _FORBIDDEN:
        return True
    return False


def protected_folder(path: str) -> bool:
    home = os.path.abspath(os.path.expanduser("~"))
    expanded = resolve_path(path)
    for name in ("Desktop", "Documents", "Downloads"):
        root = os.path.join(home, name)
        if expanded == root or expanded.startswith(root + os.sep):
            return True
    return False
