"""Textes de statut fidèles à la preuve, pas à l'annonce du modèle."""

from __future__ import annotations

import re

from ghost.core.results import DELIVERY_CONFIRMED, SENT_ITEM_FOUND

_FALSE_SUCCESS = re.compile(
    r"(tâche terminée\s*!|envoy[ée] avec succès|c'est envoyé|mail envoyé|succès vérifié|"
    r"livr[ée] au destinataire|delivery confirmed|le message est arrivé)",
    re.I,
)
_DELIVERY_CLAIM = re.compile(
    r"(livr[ée]|reçu par le destinataire|delivery confirmed|boîte de réception du destinataire)",
    re.I,
)


def final_text(status: str, model_text: str, *, delivery: str = "", summary: str = "") -> str:
    body = (model_text or "").strip()
    if status != "VERIFIED":
        body = _FALSE_SUCCESS.sub("", body).strip()
    if delivery != DELIVERY_CONFIRMED:
        body = _DELIVERY_CLAIM.sub("", body).strip()
    if status == "VERIFIED" and delivery == SENT_ITEM_FOUND:
        banner = "🏁 TÂCHE TERMINÉE — ENVOI VÉRIFIÉ."
    else:
        banner = {
            "VERIFIED": "🏁 TÂCHE TERMINÉE — RÉSULTAT VÉRIFIÉ.",
            "COMPLETED": "🏁 Réponse prête.",
            "FAILED": "🛑 ÉCHEC ÉTABLI.",
            "UNVERIFIED": "⚠️ RÉSULTAT NON VÉRIFIÉ.",
            "INDETERMINATE": "⚠️ RÉSULTAT INCERTAIN.",
        }.get(status, "⚠️ RÉSULTAT NON VÉRIFIÉ.")
    parts = [banner]
    if status == "VERIFIED" and delivery == SENT_ITEM_FOUND:
        parts.append(
            "Le message figure dans les éléments envoyés. "
            "La réception par le destinataire n'est pas confirmée."
        )
    if status == "INDETERMINATE":
        parts.append(
            "Ghost ne peut pas confirmer le résultat. "
            "Aucune nouvelle tentative automatique afin d'éviter un double effet."
        )
    if status == "UNVERIFIED":
        parts.append("Aucune preuve indépendante ne permet de déclarer la tâche accomplie.")
    if summary:
        parts.append(summary)
    if body:
        parts.append(body)
    return "\n".join(parts)


def email_progress(stage: str, detail: str = "") -> str:
    lines = {
        "account_ok": "✅ Compte Outlook accessible.",
        "account_missing": "⚠️ Compte Outlook non confirmé.",
        "fields_ok": "✅ Destinataire validé.\n✅ Sujet validé.\n✅ Contenu validé.",
        "accepted": "✅ Requête acceptée. Ceci ne prouve pas la livraison.",
        "search": "🔎 Recherche du message dans les éléments envoyés.",
        "found": "✅ Message retrouvé.\n✅ Destinataire conforme.\n✅ Sujet conforme.\n✅ Contenu conforme.",
        "missing": (
            "⚠️ Envoi déclenché, mais résultat non vérifié.\n\n"
            "Ghost ne peut pas confirmer que le message\n"
            "figure dans les éléments envoyés.\n\n"
            "Aucune nouvelle tentative automatique\n"
            "afin d'éviter un éventuel double envoi."
        ),
    }
    text = lines.get(stage, stage)
    if detail:
        return text + "\n" + detail
    return text
