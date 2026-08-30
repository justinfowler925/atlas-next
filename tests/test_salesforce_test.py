from __future__ import annotations

import json

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_test import APEX_TEST_ACTION, ApexTestRequest, SalesforceApexTest


def _classes(names=("ServiceTest",)):
    return json.dumps(
        {
            "status": 0,
            "result": [{"type": "ApexClass", "fullName": name} for name in names],
        }
    )


def _result(**summary_overrides):
    summary = {
        "outcome": "Passed",
        "testsRan": 2,
        "passing": 2,
        "failing": 0,
        "skipped": 0,
        "testRunId": "707000000000001AAA",
        "testRunCoverage": "87%",
        "orgWideCoverage": "76%",
    }
    summary.update(summary_overrides)
    return json.dumps({"status": 0, "result": {"summary": summary, "tests": []}})


def test_apex_test_contract_has_no_org_test_level_or_command_escape():
    for key in ("environment", "target_org", "test_level", "command", "tests"):
        with pytest.raises(ValueError, match=f"unexpected keys: {key}"):
            ApexTestRequest.from_payload({"classes": ["ServiceTest"], key: "anything"})
    with pytest.raises(ValueError, match="one Apex class"):
        ApexTestRequest.from_payload({"classes": ["ServiceTest --target-org prod"]})


def test_apex_test_live_validates_classes_then_runs_named_tests_in_partial(tmp_path):
    calls = []
    responses = iter(
        [CommandResult(0, _classes(("OneTest", "TwoTest")), ""), CommandResult(0, _result(), "")]
    )

    def runner(argv, timeout):
        calls.append((list(argv), timeout))
        return next(responses)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(APEX_TEST_ACTION, {"classes": ["OneTest", "TwoTest"]})
        completed = Engine(
            store,
            {
                APEX_TEST_ACTION: SalesforceApexTest(
                    partial_alias="dod-check", runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert calls[0][0] == [
        "sf", "org", "list", "metadata", "--metadata-type", "ApexClass",
        "--target-org", "dod-check", "--json",
    ]
    assert calls[1][0] == [
        "sf", "apex", "run", "test",
        "--class-names", "OneTest", "--class-names", "TwoTest",
        "--target-org", "dod-check", "--wait", "10",
        "--result-format", "json", "--code-coverage", "--json",
    ]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["tests_ran"] == 2
    assert completed.evidence[0]["production_execution"] is False


def test_missing_live_class_stops_before_test_execution(tmp_path):
    calls = 0

    def runner(_argv, _timeout):
        nonlocal calls
        calls += 1
        return CommandResult(0, _classes(("OtherTest",)), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(APEX_TEST_ACTION, {"classes": ["MissingTest"]})
        completed = Engine(
            store,
            {APEX_TEST_ACTION: SalesforceApexTest(partial_alias="dod-check", runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert calls == 1
    assert completed is not None and completed.state is WorkState.FAILED
    assert "absent from live Partial" in (completed.error or "")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"testsRan": 0, "passing": 0}, "zero tests"),
        ({"outcome": "Failed", "passing": 1, "failing": 1}, "did not prove"),
        ({"testsRan": 3}, "do not reconcile"),
        ({"testRunId": "bad"}, "valid test run id"),
        ({"testRunCoverage": None}, "valid run coverage"),
    ],
)
def test_malformed_or_failed_summary_cannot_mint_success(tmp_path, overrides, reason):
    responses = iter([CommandResult(0, _classes(), ""), CommandResult(0, _result(**overrides), "")])
    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(APEX_TEST_ACTION, {"classes": ["ServiceTest"]})
        completed = Engine(
            store,
            {
                APEX_TEST_ACTION: SalesforceApexTest(
                    partial_alias="dod-check", runner=lambda *_args: next(responses)
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert completed is not None and completed.state is WorkState.FAILED
    assert reason in (completed.error or "")
