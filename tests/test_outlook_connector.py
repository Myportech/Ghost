import httpx

from ghost.connectors.outlook.accessibility_adapter import apply_plan, plan_compose
from ghost.connectors.outlook.adapter import OutlookConnector
from ghost.connectors.outlook.applescript_adapter import build_draft_script, build_send_script
from ghost.connectors.outlook.graph_client import GraphMailClient, GraphReceipt, MemoryTokenStore
from ghost.core.operation_journal import OperationJournal
from ghost.core.results import (
    ACTION_FAILED,
    ACTION_INDETERMINATE,
    ACTION_VERIFIED,
    PROCESS_FAILED,
    PROCESS_SUCCEEDED,
    make_result,
)

EXPECTED = {
    "to": "oscar.beaucamp@skema.edu",
    "subject": "dassaut c'est dassaut",
    "body": "dassaut c'est dassaut",
}


class FakeGraph:
    def __init__(self):
        self.sends = 0
        self.mode = "accept"
        self.messages = []
        self._device = {}

    def configured(self):
        return True

    def has_token(self):
        return True

    def is_available(self):
        return True

    def send_mail(self, recipient, subject, body):
        self.sends += 1
        if self.mode == "timeout":
            return GraphReceipt("TIMEOUT", error="délai")
        if self.mode == "auth":
            return GraphReceipt("AUTH", 401, error="unauthorized")
        if self.mode == "permission":
            return GraphReceipt("PERMISSION", 403, error="access denied")
        return GraphReceipt("REQUEST_ACCEPTED", 202)

    def list_recent_sent(self):
        return list(self.messages)

    def list_recent_messages(self, folder="inbox", limit=15):
        return []

    def create_draft(self, recipient, subject, body):
        return GraphReceipt("FAILED", error="non utilisé")

    def get_message(self, message_id):
        return None


class DeadAdapter:
    def is_available(self):
        return False


def _connector(graph, clock=1_700_000_000):
    return OutlookConnector(
        graph=graph,
        applescript=DeadAdapter(),
        accessibility=DeadAdapter(),
        journal=OperationJournal(),
        clock=lambda: clock,
    )


def _sent(sent_at):
    return {
        "to": [EXPECTED["to"]],
        "subject": EXPECTED["subject"],
        "body": EXPECTED["body"],
        "sent_at": sent_at,
    }


def test_graph_202_is_not_delivery_confirmed():
    def handler(request: httpx.Request):
        assert b"password" not in request.content
        assert request.url.path.endswith("/sendMail")
        return httpx.Response(202)

    client = GraphMailClient(
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        store=MemoryTokenStore({"access_token": "token-test", "expires_at": 9_999_999_999}),
        client_id="public-client",
        tenant="common",
        clock=lambda: 1_000,
    )
    receipt = client.send_mail("a@b.c", "sujet", "corps")
    assert receipt.outcome == "REQUEST_ACCEPTED"
    assert receipt.http_status == 202
    assert receipt.outcome != "DELIVERY_CONFIRMED"


def test_device_flow_uses_public_client_without_password():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "device_code": "secret-device",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5,
            },
        )

    client = GraphMailClient(
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        store=MemoryTokenStore(),
        client_id="public-client",
        tenant="organizations",
    )
    receipt = client.start_device_flow()
    assert "password" not in seen["body"]
    assert "public-client" in seen["body"]
    assert "ABCD-EFGH" in receipt.error
    assert "secret-device" not in receipt.error


def test_accepted_and_found_is_verified_without_delivery_claim():
    graph = FakeGraph()
    graph.messages = [_sent(1_700_000_002)]
    result = _connector(graph).send_email(EXPECTED)
    assert result.action_status == ACTION_VERIFIED
    assert result.evidence["delivery"] == "SENT_ITEM_FOUND"
    assert "DELIVERY_CONFIRMED" not in result.render()
    assert "n'est pas confirmée" in result.render() or "pas la livraison" in result.render()


def test_accepted_but_missing_is_indeterminate_and_not_resent():
    graph = FakeGraph()
    connector = _connector(graph)
    first = connector.send_email(EXPECTED)
    second = connector.send_email(EXPECTED)
    assert first.action_status == ACTION_INDETERMINATE
    assert first.evidence["allow_resend"] is False
    assert graph.sends == 1
    assert second.action_status != ACTION_VERIFIED
    assert graph.sends == 1


def test_timeout_searches_before_any_other_send():
    graph = FakeGraph()
    graph.mode = "timeout"
    graph.messages = [_sent(1_700_000_002)]
    connector = _connector(graph)
    result = connector.send_email(EXPECTED)
    assert graph.sends == 1
    assert result.action_status == ACTION_VERIFIED
    again = connector.send_email(EXPECTED)
    assert graph.sends == 1
    assert again.action_verified is True or again.action_status == ACTION_VERIFIED


def test_timeout_without_message_never_resends():
    graph = FakeGraph()
    graph.mode = "timeout"
    connector = _connector(graph)
    first = connector.send_email(EXPECTED)
    connector.send_email(EXPECTED)
    assert first.action_status == ACTION_INDETERMINATE
    assert graph.sends == 1


def test_auth_failure_does_not_claim_success():
    graph = FakeGraph()
    graph.mode = "auth"
    result = _connector(graph).send_email(EXPECTED)
    assert result.action_status == ACTION_FAILED
    assert result.evidence.get("sent") is False
    assert result.evidence.get("category") == "authentication"
    assert graph.sends == 1


def test_permission_failure():
    graph = FakeGraph()
    graph.mode = "permission"
    result = _connector(graph).send_email(EXPECTED)
    assert result.action_status == ACTION_FAILED
    assert result.evidence.get("category") == "permission_denied"


def test_graph_falls_through_to_applescript_only_before_send():
    graph = FakeGraph()
    graph.mode = "auth"
    calls = []

    def runner(script, timeout):
        calls.append(script)
        if "default account" in script:
            return make_result("applescript", process_status=PROCESS_SUCCEEDED, action_status="ACTION_UNVERIFIED", stdout="Compte")
        return make_result(
            "applescript",
            process_status=PROCESS_FAILED,
            action_status=ACTION_FAILED,
            stdout="GHOST_FIELD_MISMATCH subject",
            error="ERREUR: GHOST_FIELD_MISMATCH subject",
        )

    class Script:
        def is_available(self):
            return True

        def account_name(self):
            return runner('tell application "Microsoft Outlook" to return name of default account', 5)

        def send(self, recipient, subject, body):
            return runner(build_send_script(recipient, subject, body), 5)

        def list_folder(self, folder):
            return []

    connector = OutlookConnector(
        graph=graph,
        applescript=Script(),
        accessibility=DeadAdapter(),
        journal=OperationJournal(),
        clock=lambda: 1_700_000_000,
    )
    result = connector.send_email(EXPECTED)
    assert graph.sends == 1
    assert any("GHOST_FIELD_MISMATCH" in script and script.index("send newMsg") > script.index("GHOST_FIELD_MISMATCH") for script in calls)
    assert result.evidence.get("sent") is False
    assert result.action_verified is False


def test_send_script_checks_fields_before_send_and_draft_does_not_send():
    script = build_send_script('a"@b.c', "sujet\nl2", "corps")
    assert script.index("GHOST_FIELD_MISMATCH") < script.index("send newMsg")
    assert "keystroke" not in script.lower()
    draft = build_draft_script("a@b.c", "sujet", "corps")
    assert "send newMsg" not in draft
    assert "DRAFT" in draft


def test_accessibility_does_not_press_when_fields_cannot_be_read_back():
    presses = []
    nodes = [
        {"role": "AXTextField", "title": "Destinataire", "value": "", "set_value": lambda value: None},
        {"role": "AXTextField", "title": "Objet", "value": "", "set_value": lambda value: None},
        {"role": "AXTextArea", "title": "Corps", "value": "", "set_value": lambda value: None},
        {"role": "AXButton", "title": "Envoyer", "press": lambda: presses.append(1)},
    ]
    # set_value does not stick
    plan = plan_compose(nodes, EXPECTED)
    ok, reason, pressed = apply_plan(plan, {"recipient": EXPECTED["to"], "subject": EXPECTED["subject"], "body": EXPECTED["body"]})
    assert plan["ok"] is True
    assert ok is False
    assert pressed is False
    assert presses == []
    assert "conforme" in reason


def test_accessibility_presses_only_after_matching_readback():
    presses = []

    def writer(node):
        def set_value(value, box=node):
            box["value"] = value

        return set_value

    nodes = [
        {"role": "AXTextField", "title": "To", "value": ""},
        {"role": "AXTextField", "title": "Subject", "value": ""},
        {"role": "AXTextArea", "description": "Message", "value": ""},
        {"role": "AXButton", "title": "Send", "press": lambda: presses.append("send")},
    ]
    for node in nodes:
        node["set_value"] = writer(node)
    plan = plan_compose(nodes, EXPECTED)
    ok, _reason, pressed = apply_plan(
        plan,
        {"recipient": EXPECTED["to"], "subject": EXPECTED["subject"], "body": EXPECTED["body"]},
    )
    assert ok is True
    assert pressed is True
    assert presses == ["send"]
    assert nodes[0]["value"] == EXPECTED["to"]


def test_accessibility_missing_permission_stops():
    class AX:
        def is_available(self):
            return True

        def send(self, expected):
            return False, "Permission Accessibilité absente.", False, []

    graph = FakeGraph()
    graph.configured = lambda: False
    graph.has_token = lambda: False
    connector = OutlookConnector(
        graph=graph,
        applescript=DeadAdapter(),
        accessibility=AX(),
        journal=OperationJournal(),
        clock=lambda: 1_700_000_000,
    )
    result = connector.send_email(EXPECTED)
    assert result.action_status == ACTION_FAILED
    assert result.evidence.get("category") == "accessibility_permission"
    assert result.evidence.get("sent") is False
    assert graph.sends == 0


def test_outlook_unavailable():
    graph = FakeGraph()
    graph.configured = lambda: False
    graph.has_token = lambda: False
    connector = OutlookConnector(
        graph=graph,
        applescript=DeadAdapter(),
        accessibility=DeadAdapter(),
        journal=OperationJournal(),
        clock=lambda: 1,
    )
    result = connector.send_email(EXPECTED)
    assert result.action_verified is False
    assert result.action_status == ACTION_FAILED
