"""Secrets via l'environnement ou le trousseau macOS. Rien n'est codé en dur."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

GROQ_SERVICE = "ghost.groq"
GROQ_ACCOUNT = "api_key"
MS_SERVICE = "ghost.microsoft"


def load_dotenv_files() -> None:
    """Charge des .env locaux sans écraser l'environnement déjà défini."""
    roots = [
        Path.home() / "Desktop" / "inline",
        Path(__file__).resolve().parents[2],
        Path.cwd(),
        Path.home() / "Library" / "Application Support" / "inline",
    ]
    seen = set()
    for root in roots:
        path = root / ".env"
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        _apply_env_file(path)


def _apply_env_file(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


def keychain_get(service: str, account: str) -> str:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def keychain_set(service: str, account: str, secret: str) -> bool:
    if not secret:
        return False
    try:
        proc = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                service,
                "-a",
                account,
                "-w",
                secret,
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def load_groq_api_key() -> str:
    load_dotenv_files()
    env = (os.environ.get("GROQ_API_KEY") or "").strip()
    if env:
        return env
    return keychain_get(GROQ_SERVICE, GROQ_ACCOUNT)


def load_microsoft_config() -> dict:
    """Client public OAuth. Le secret d'application n'est pas requis pour le device code."""
    load_dotenv_files()
    client_id = (os.environ.get("GHOST_MS_CLIENT_ID") or "").strip()
    if not client_id:
        client_id = keychain_get(MS_SERVICE, "client_id")
    tenant = (os.environ.get("GHOST_MS_TENANT") or "").strip() or "common"
    return {
        "client_id": client_id,
        "tenant": tenant,
        "configured": bool(client_id),
    }
