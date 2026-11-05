from ghost.core.verification_engine import (
    FAILED,
    INDETERMINATE,
    UNVERIFIED,
    VERIFIED,
    reject_unproven_success,
    verify_calendar_event,
    verify_file_created,
    verify_file_moved,
    verify_sent_email,
)


def _mail(recipient, subject, body, sent_at):
    return {"to": [recipient], "subject": subject, "body": body, "sent_at": sent_at}


EXPECTED = {
    "recipient": "oscar.beaucamp@skema.edu",
    "subject": "dassaut c'est dassaut",
    "body": "dassaut c'est dassaut",
}


def test_verified_file(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("bonjour")
    result = verify_file_created({"path": str(path), "content": "bonjour"})
    assert result.status == VERIFIED
    assert result.verified is True


def test_missing_file_is_unverified(tmp_path):
    result = verify_file_created({"path": str(tmp_path / "absent.txt")})
    assert result.status == UNVERIFIED
    assert result.verified is False


def test_wrong_content_is_failed(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("autre")
    result = verify_file_created({"path": str(path), "content": "attendu"})
    assert result.status == FAILED
    assert result.verified is False


def test_partial_proof_is_unverified():
    result = verify_file_created(
        {"path": "/tmp/ghost-partial", "content": "attendu"},
        observed={"exists": True, "readable": True, "location_ok": True},
        probe=False,
    )
    assert result.status == UNVERIFIED
    assert result.evidence.get("partial") is True


def test_llm_claim_does_not_verify(tmp_path):
    result = verify_file_created(
        {"path": str(tmp_path / "nope.txt")},
        llm_claim="Tout semble fonctionner, le fichier a bien été créé.",
    )
    result = reject_unproven_success("succès", result)
    assert result.status == UNVERIFIED
    assert result.verified is False
    assert result.evidence.get("llm_claim_ignored") is True


def test_move_verified(tmp_path):
    source = tmp_path / "a.txt"
    dest = tmp_path / "b.txt"
    source.write_text("same")
    source.rename(dest)
    result = verify_file_moved({"source": str(source), "destination": str(dest)})
    assert result.status == VERIFIED


def test_move_not_done_is_unverified(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("same")
    result = verify_file_moved({"source": str(source), "destination": str(tmp_path / "b.txt")})
    assert result.status == UNVERIFIED
    assert result.verified is False


def test_email_found_is_verified_not_delivered():
    result = verify_sent_email(
        EXPECTED,
        [_mail(EXPECTED["recipient"], EXPECTED["subject"], EXPECTED["body"], 1005)],
        started_at=1000,
        now=1010,
        receipt="REQUEST_ACCEPTED",
    )
    assert result.status == VERIFIED
    assert result.evidence["delivery"] == "SENT_ITEM_FOUND"
    assert result.evidence["delivery"] != "DELIVERY_CONFIRMED"
    assert result.evidence["allow_resend"] is False


def test_email_accepted_but_missing_is_indeterminate():
    result = verify_sent_email(EXPECTED, [], started_at=1000, now=1010, receipt="REQUEST_ACCEPTED")
    assert result.status == INDETERMINATE
    assert result.evidence["allow_resend"] is False


def test_wrong_recipient_fails():
    result = verify_sent_email(
        EXPECTED,
        [_mail("autre@skema.edu", EXPECTED["subject"], EXPECTED["body"], 1005)],
        started_at=1000,
        now=1010,
        receipt="REQUEST_ACCEPTED",
    )
    assert result.status == FAILED
    assert result.verified is False


def test_wrong_subject_fails():
    result = verify_sent_email(
        EXPECTED,
        [_mail(EXPECTED["recipient"], "autre sujet", EXPECTED["body"], 1005)],
        started_at=1000,
        now=1010,
        receipt="REQUEST_ACCEPTED",
    )
    assert result.status == FAILED


def test_wrong_body_fails():
    result = verify_sent_email(
        EXPECTED,
        [_mail(EXPECTED["recipient"], EXPECTED["subject"], "autre corps", 1005)],
        started_at=1000,
        now=1010,
        receipt="REQUEST_ACCEPTED",
    )
    assert result.status == FAILED


def test_old_identical_message_is_not_proof():
    result = verify_sent_email(
        EXPECTED,
        [_mail(EXPECTED["recipient"], EXPECTED["subject"], EXPECTED["body"], 100)],
        started_at=1000,
        now=1010,
        receipt="REQUEST_ACCEPTED",
    )
    assert result.verified is False
    assert result.status == INDETERMINATE
    assert result.evidence["ignored_old"] == 1


def test_timeout_without_message_does_not_allow_resend():
    result = verify_sent_email(EXPECTED, [], started_at=1000, now=1010, receipt="TIMEOUT_AFTER_SEND")
    assert result.status == INDETERMINATE
    assert result.evidence["allow_resend"] is False


def test_two_identical_messages_are_indeterminate():
    message = _mail(EXPECTED["recipient"], EXPECTED["subject"], EXPECTED["body"], 1005)
    result = verify_sent_email(EXPECTED, [message, dict(message)], started_at=1000, now=1010, receipt="REQUEST_ACCEPTED")
    assert result.status == INDETERMINATE
    assert result.verified is False


def test_auth_permission_and_unavailable_fail():
    for receipt, category in (
        ("AUTH", "authentication"),
        ("PERMISSION", "permission_denied"),
        ("UNAVAILABLE", "app_not_responding"),
        ("ACCESSIBILITY", "accessibility_permission"),
    ):
        result = verify_sent_email(EXPECTED, [], started_at=1000, now=1010, receipt=receipt)
        assert result.status == FAILED
        assert result.evidence["category"] == category


def test_llm_cannot_verify_email():
    result = verify_sent_email(
        EXPECTED,
        [],
        started_at=1000,
        now=1010,
        llm_claim="Le mail est bien parti, tout semble fonctionner.",
    )
    assert result.status == UNVERIFIED
    assert result.evidence.get("llm_claim_ignored") is True


def test_calendar_match_and_miss():
    found = verify_calendar_event(
        {"title": "Cours", "start": "2026-11-05T10:00:00"},
        [{"title": "Cours", "start": "2026-11-05T10:00:30"}],
    )
    missing = verify_calendar_event(
        {"title": "Cours", "start": "2026-11-05T10:00:00"},
        [],
        llm_claim="L'événement est créé.",
    )
    assert found.status == VERIFIED
    assert missing.status == UNVERIFIED
    assert missing.evidence.get("llm_claim_ignored") is True
