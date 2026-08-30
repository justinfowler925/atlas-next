from __future__ import annotations

import json

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_authenticated import (
    AUTHENTICATED_GET_ACTION,
    SalesforceAuthenticatedGet,
)


def _payload(**overrides):
    payload = {
        "credential_parameter": "Token",
        "expected_status": 200,
        "external_credential": "HubSpot_PrivateApp",
        "named_credential": "HubSpot_API",
        "path": "/crm/v3/objects/contacts?limit=1",
    }
    payload.update(overrides)
    return payload


def test_authenticated_get_proves_status_without_recording_body(tmp_path):
    commands = []

    def runner(argv, _timeout):
        commands.append(list(argv))
        logs = (
            "00:00|USER_DEBUG|[10]|DEBUG|ATLAS_AUTH_GET_STATUS=200\n"
            + "00:00|USER_DEBUG|[11]|DEBUG|ATLAS_AUTH_GET_BODY_SHA256="
            + "a" * 64
            + "\n00:00|USER_DEBUG|[12]|DEBUG|ATLAS_AUTH_GET_BODY_BYTES=321\n"
        )
        return CommandResult(
            0,
            json.dumps({"result": {"compiled": True, "success": True, "logs": logs}}),
            "",
        )

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(AUTHENTICATED_GET_ACTION, _payload())
        completed = Engine(
            store,
            {
                AUTHENTICATED_GET_ACTION: SalesforceAuthenticatedGet(
                    partial_alias="dod-check",
                    artifact_root=tmp_path / "artifacts",
                    runner=runner,
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert commands[0][:4] == ["sf", "apex", "run", "--file"]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["http_status"] == 200
    assert completed.result["body_bytes"] == 321
    assert "body" not in completed.result
    assert completed.evidence[0]["authenticated"] is True


def test_authenticated_get_refuses_credential_name_injection_before_org_call(tmp_path):
    called = False

    def runner(*_args):
        nonlocal called
        called = True
        return CommandResult(0, "{}", "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(
            AUTHENTICATED_GET_ACTION,
            _payload(external_credential="HubSpot.PrivateApp'; System.abortJob('x')"),
        )
        completed = Engine(
            store,
            {
                AUTHENTICATED_GET_ACTION: SalesforceAuthenticatedGet(
                    partial_alias="dod-check",
                    artifact_root=tmp_path / "artifacts",
                    runner=runner,
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert called is False
    assert completed is not None and completed.state is WorkState.FAILED
    assert "safe Salesforce developer names" in (completed.error or "")


def test_authenticated_get_refuses_parent_path_before_org_call(tmp_path):
    called = False

    def runner(*_args):
        nonlocal called
        called = True
        return CommandResult(0, "{}", "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(AUTHENTICATED_GET_ACTION, _payload(path="/crm/../secrets"))
        completed = Engine(
            store,
            {
                AUTHENTICATED_GET_ACTION: SalesforceAuthenticatedGet(
                    partial_alias="dod-check",
                    artifact_root=tmp_path / "artifacts",
                    runner=runner,
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert called is False
    assert completed is not None and completed.state is WorkState.FAILED
    assert "bounded relative HTTP path" in (completed.error or "")
