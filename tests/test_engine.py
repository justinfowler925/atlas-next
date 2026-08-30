from __future__ import annotations

from atlas_next import Engine, Outcome, Store, WorkState


def test_disabled_engine_does_not_claim_work(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        queued = store.enqueue("inspect", {"ticket": "REV-1"}, now=10)
        engine = Engine(store, {}, worker_id="worker-1", execution_enabled=False)

        result = engine.run_once(now=11)

        assert not result.claimed
        assert store.get(queued.id).state is WorkState.QUEUED
        assert store.events(queued.id) == [
            {"seq": 1, "kind": "enqueued", "at": 10.0, "action": "inspect"}
        ]


def test_success_requires_structured_evidence(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue("inspect", {}, now=10)
        engine = Engine(
            store,
            {"inspect": lambda _item: Outcome.success({"answer": "done"}, evidence=[])},
            worker_id="worker-1",
            execution_enabled=True,
        )

        result = engine.run_once(now=11)

        assert result.claimed
        failed = store.get(item.id)
        assert failed.state is WorkState.FAILED
        assert failed.error == "success rejected: no structured evidence"
        assert [event["kind"] for event in store.events(item.id)] == [
            "enqueued",
            "claimed",
            "failed",
        ]


def test_grounded_success_is_terminal_and_auditable(tmp_path):
    evidence = [{"kind": "query", "source": "partial", "count": 7}]
    with Store(tmp_path / "state.sqlite3") as store:
        store.enqueue("inspect", {"object": "Account"}, now=10)
        engine = Engine(
            store,
            {"inspect": lambda _item: Outcome.success({"count": 7}, evidence)},
            worker_id="worker-1",
            execution_enabled=True,
        )

        result = engine.run_once(now=11)

        assert result.item is not None
        assert result.item.state is WorkState.SUCCEEDED
        assert result.item.result == {"count": 7}
        assert result.item.evidence == evidence


def test_retry_is_explicit_bounded_and_visible(tmp_path):
    attempts = 0

    def flaky(_item):
        nonlocal attempts
        attempts += 1
        return Outcome.failed("transient", retryable=True, retry_delay_seconds=5)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue("inspect", {}, max_attempts=2, now=10)
        engine = Engine(
            store,
            {"inspect": flaky},
            worker_id="worker-1",
            execution_enabled=True,
        )

        first = engine.run_once(now=11).item
        assert first is not None and first.state is WorkState.QUEUED
        assert first.available_at == 16
        assert not engine.run_once(now=15).claimed
        second = engine.run_once(now=16).item
        assert second is not None and second.state is WorkState.FAILED
        assert second.attempts == 2
        assert attempts == 2
        assert [event["kind"] for event in store.events(item.id)] == [
            "enqueued",
            "claimed",
            "retry_scheduled",
            "claimed",
            "failed",
        ]


def test_expired_lease_fails_closed_instead_of_silent_reclaim(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue("inspect", {}, now=10)
        claimed = store.claim_next("dead-worker", lease_seconds=5, now=11)
        assert claimed is not None

        expired = store.expire_leases(now=17)

        assert expired == [item.id]
        failed = store.get(item.id)
        assert failed.state is WorkState.FAILED
        assert "operator review required" in (failed.error or "")
        assert store.claim_next("other-worker", now=18) is None


def test_unknown_action_fails_once_without_model_or_retry_loop(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        store.enqueue("invented_action", {}, max_attempts=5, now=10)
        engine = Engine(store, {}, worker_id="worker-1", execution_enabled=True)

        result = engine.run_once(now=11)

        assert result.item is not None
        assert result.item.state is WorkState.FAILED
        assert result.item.attempts == 1
        assert "no registered handler" in (result.item.error or "")
