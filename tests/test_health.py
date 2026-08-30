from __future__ import annotations

from atlas_next import Store
from atlas_next.health import snapshot


def test_running_process_is_not_enough_when_execution_is_disabled(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        report = snapshot(
            store,
            execution_enabled=False,
            executor_running=True,
            registered_actions={"inspect"},
            worker_id="pid-3170",
            now=10,
        )

    assert report["overall"] == "paused"
    assert report["executor"]["enabled"] is False
    assert report["executor"]["running"] is True
    assert report["executor"]["ready"] is False
    assert report["reason"] == "execution is disabled"


def test_health_fails_handler_coverage_for_real_queued_population(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        store.enqueue("salesforce.inspect", {}, now=10)
        store.enqueue("github.checks", {}, now=10)
        report = snapshot(
            store,
            execution_enabled=True,
            executor_running=True,
            registered_actions={"salesforce.inspect"},
            worker_id="worker-1",
            now=11,
        )

    assert report["overall"] == "degraded"
    assert report["uncovered_actions"] == ["github.checks"]
    assert report["executor"]["ready"] is False


def test_health_reports_ok_only_when_enabled_covered_and_unexpired(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        store.enqueue("salesforce.inspect", {}, now=10)
        report = snapshot(
            store,
            execution_enabled=True,
            executor_running=True,
            registered_actions={"salesforce.inspect"},
            worker_id="worker-1",
            now=11,
        )

    assert report["overall"] == "ok"
    assert report["executor"]["ready"] is True
    assert report["counts"]["queued"] == 1


def test_enabled_configuration_is_unhealthy_without_a_running_executor(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        report = snapshot(
            store,
            execution_enabled=True,
            executor_running=False,
            registered_actions={"salesforce.inspect"},
            worker_id="missing",
            now=11,
        )

    assert report["overall"] == "unhealthy"
    assert report["executor"]["ready"] is False
    assert report["reason"] == "execution is enabled but no executor is running"
