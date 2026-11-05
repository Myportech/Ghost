"""Client Microsoft Graph. Le mot de passe Outlook n'est jamais demandé."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from ghost.core.redact import redact
from ghost.core.secrets import MS_SERVICE, keychain_get, keychain_set, load_microsoft_config

SCOPES = "offline_access User.Read Mail.Send Mail.Read"
GRAPH = "https://graph.microsoft.com/v1.0"


@dataclass
class GraphReceipt:
    outcome: str
    http_status: Optional[int] = None
    error: str = ""
    message_id: str = ""


class MemoryTokenStore:
    def __init__(self, token: Optional[dict] = None) -> None:
        self.token = token

    def load(self) -> Optional[dict]:
        return self.token

    def save(self, token: dict) -> None:
        self.token = token


class KeychainTokenStore:
    def load(self) -> Optional[dict]:
        raw = keychain_get(MS_SERVICE, "tokens")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def save(self, token: dict) -> None:
        keychain_set(MS_SERVICE, "tokens", json.dumps(token))


class GraphMailClient:
    """OAuth device code (client public) puis /me/sendMail.

    Une réponse 202 signifie seulement que Graph a accepté la requête.
    """

    def __init__(self, http=None, store=None, client_id: str = "", tenant: str = "", clock=None) -> None:
        config = load_microsoft_config()
        self.client_id = client_id or config["client_id"]
        self.tenant = tenant or config["tenant"]
        self.store = store if store is not None else KeychainTokenStore()
        self._http = http
        self.clock = clock or time.time
        self._device: dict = {}

    def configured(self) -> bool:
        return bool(self.client_id)

    def has_token(self) -> bool:
        token = self.store.load() or {}
        return bool(token.get("access_token"))

    def is_available(self) -> bool:
        return self.configured()

    def _now(self) -> float:
        value = self.clock() if callable(self.clock) else self.clock
        return float(value)

    def _request(self, method: str, url: str, **kwargs):
        owns = self._http is None
        client = self._http or httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))
        try:
            return client.request(method, url, **kwargs)
        finally:
            if owns:
                client.close()

    def _token_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"

    def _device_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/devicecode"

    def access_token(self) -> str:
        token = self.store.load() or {}
        access = str(token.get("access_token") or "")
        expires = float(token.get("expires_at") or 0)
        if access and expires > self._now() + 60:
            return access
        refresh = str(token.get("refresh_token") or "")
        if not refresh or not self.client_id:
            return ""
        try:
            response = self._request(
                "POST",
                self._token_url(),
                data={
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "scope": SCOPES,
                },
            )
        except httpx.HTTPError:
            return ""
        if response.status_code != 200:
            return ""
        self._save_token(response.json())
        return str((self.store.load() or {}).get("access_token") or "")

    def _save_token(self, payload: dict) -> None:
        current = self.store.load() or {}
        access = payload.get("access_token") or current.get("access_token")
        refresh = payload.get("refresh_token") or current.get("refresh_token")
        expires_in = float(payload.get("expires_in") or 3600)
        self.store.save(
            {
                "access_token": access,
                "refresh_token": refresh,
                "expires_at": self._now() + expires_in,
            }
        )

    def start_device_flow(self) -> GraphReceipt:
        if not self.client_id:
            return GraphReceipt("UNAVAILABLE", error="GHOST_MS_CLIENT_ID absent.")
        try:
            response = self._request(
                "POST",
                self._device_url(),
                data={"client_id": self.client_id, "scope": SCOPES},
            )
        except httpx.ConnectTimeout:
            return GraphReceipt("FAILED", error="Connexion au service d'identité impossible.")
        except httpx.TimeoutException:
            return GraphReceipt("TIMEOUT", error="Délai pendant la demande de code.")
        except httpx.HTTPError as exc:
            return GraphReceipt("FAILED", error=redact(str(exc))[:180])
        if response.status_code != 200:
            return GraphReceipt("AUTH", response.status_code, error=_safe_body(response))
        data = response.json()
        self._device = {
            "device_code": data.get("device_code") or "",
            "interval": int(data.get("interval") or 5),
        }
        message = (
            "Connecte le compte Microsoft : ouvre "
            f"{data.get('verification_uri') or 'https://microsoft.com/devicelogin'} "
            f"et entre le code {data.get('user_code') or ''}."
        )
        return GraphReceipt("AUTH", 200, error=message)

    def poll_device_flow(self) -> GraphReceipt:
        device_code = self._device.get("device_code") or ""
        if not device_code:
            return self.start_device_flow()
        try:
            response = self._request(
                "POST",
                self._token_url(),
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.client_id,
                    "device_code": device_code,
                },
            )
        except httpx.TimeoutException:
            return GraphReceipt("TIMEOUT", error="Délai pendant l'attente du consentement.")
        except httpx.HTTPError as exc:
            return GraphReceipt("FAILED", error=redact(str(exc))[:180])
        if response.status_code == 200:
            self._save_token(response.json())
            self._device = {}
            return GraphReceipt("REQUEST_ACCEPTED", 200, error="Compte Microsoft connecté.")
        try:
            error = (response.json() or {}).get("error") or ""
        except Exception:
            error = ""
        if error in {"authorization_pending", "slow_down"}:
            return GraphReceipt("AUTH", response.status_code, error="En attente du consentement Microsoft.")
        return GraphReceipt("AUTH", response.status_code, error=_safe_body(response) or error)

    def send_mail(self, recipient: str, subject: str, body: str) -> GraphReceipt:
        token = self.access_token()
        if not token:
            return GraphReceipt("AUTH", error="Aucun jeton Microsoft. Utilise outlook.login.")
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": recipient}}],
            },
            "saveToSentItems": True,
        }
        return self._exchange("POST", f"{GRAPH}/me/sendMail", json=payload, send=True)

    def create_draft(self, recipient: str, subject: str, body: str) -> GraphReceipt:
        token = self.access_token()
        if not token:
            return GraphReceipt("AUTH", error="Aucun jeton Microsoft.")
        payload = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        }
        return self._exchange("POST", f"{GRAPH}/me/messages", json=payload, send=False)

    def get_message(self, message_id: str) -> Optional[dict]:
        token = self.access_token()
        if not token or not message_id:
            return None
        response = self._raw("GET", f"{GRAPH}/me/messages/{message_id}")
        if response is None or response.status_code != 200:
            return None
        return _normalize_graph_message(response.json())

    def list_recent_sent(self, limit: int = 15) -> list:
        return self._list_folder("sentitems", limit)

    def list_recent_messages(self, folder: str = "inbox", limit: int = 15) -> list:
        return self._list_folder(folder or "inbox", limit)

    def _list_folder(self, folder: str, limit: int) -> list:
        token = self.access_token()
        if not token:
            return []
        params = {
            "$top": str(limit),
            "$select": "subject,sentDateTime,toRecipients,body,bodyPreview,from",
            "$orderby": "sentDateTime desc",
        }
        response = self._raw("GET", f"{GRAPH}/me/mailFolders/{folder}/messages", params=params)
        if response is None:
            return []
        if response.status_code == 400:
            params.pop("$orderby", None)
            response = self._raw("GET", f"{GRAPH}/me/mailFolders/{folder}/messages", params=params)
        if response is None or response.status_code != 200:
            raise RuntimeError(_safe_body(response) if response is not None else "lecture impossible")
        values = (response.json() or {}).get("value") or []
        return [_normalize_graph_message(item) for item in values if isinstance(item, dict)]

    def _raw(self, method: str, url: str, **kwargs):
        token = self.access_token()
        if not token:
            return None
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        try:
            return self._request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError:
            return None

    def _exchange(self, method: str, url: str, *, json: dict, send: bool) -> GraphReceipt:
        token = self.access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            response = self._request(method, url, headers=headers, json=json)
        except httpx.ConnectTimeout:
            return GraphReceipt("FAILED", error="Connexion Graph impossible. La requête n'a probablement pas été acceptée.")
        except httpx.TimeoutException:
            return GraphReceipt(
                "TIMEOUT",
                error="Délai Graph. L'acceptation de l'envoi est incertaine." if send else "Délai Graph.",
            )
        except httpx.ConnectError:
            return GraphReceipt("FAILED", error="Réseau indisponible. La requête n'a probablement pas été acceptée.")
        except httpx.HTTPError as exc:
            return GraphReceipt("TIMEOUT" if send else "FAILED", error=redact(str(exc))[:180])
        return _map_status(response, send)


def _map_status(response, send: bool) -> GraphReceipt:
    code = response.status_code
    if code in (200, 202) and send:
        return GraphReceipt("REQUEST_ACCEPTED", code)
    if code in (200, 201) and not send:
        message_id = ""
        try:
            message_id = str((response.json() or {}).get("id") or "")
        except Exception:
            message_id = ""
        return GraphReceipt("REQUEST_ACCEPTED", code, message_id=message_id)
    if code == 401:
        return GraphReceipt("AUTH", code, error=_safe_body(response))
    if code == 403:
        return GraphReceipt("PERMISSION", code, error=_safe_body(response))
    if code == 429:
        return GraphReceipt("RATE_LIMIT", code, error="Quota Microsoft Graph atteint.")
    return GraphReceipt("FAILED", code, error=_safe_body(response))


def _safe_body(response) -> str:
    if response is None:
        return ""
    try:
        text = response.text or ""
    except Exception:
        text = ""
    return redact(" ".join(text.split()))[:180]


def _normalize_graph_message(item: dict) -> dict:
    recipients = []
    for entry in item.get("toRecipients") or []:
        address = ((entry or {}).get("emailAddress") or {}).get("address")
        if address:
            recipients.append(address)
    sender = ((item.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    body = item.get("body")
    if isinstance(body, dict):
        content = body.get("content") or ""
    else:
        content = item.get("bodyPreview") or ""
    return {
        "id": item.get("id") or "",
        "to": recipients,
        "subject": item.get("subject") or "",
        "body": content,
        "sent_at": item.get("sentDateTime"),
        "sender": sender,
    }
