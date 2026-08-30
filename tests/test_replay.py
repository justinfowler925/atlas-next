import json
import sqlite3

import pytest

from atlas_next.replay import FAMILIES, REQUIRED_ACTIONS, replay_report


def _history(path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE work_items (external_id TEXT, title TEXT, goal TEXT, status TEXT)"
    )
    rows = [
        ("REV-1", "Audit Salesforce flow metadata", "SOQL report", "done"),
        ("REV-2", "Apex LWC integration deploy", "backfill", "done"),
    ]
    connection.executemany("INSERT INTO work_items VALUES (?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


def _receipt_db(path, rows):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE work_items (id TEXT, action TEXT, state TEXT, updated_at REAL, "
        "result_json TEXT, evidence_json TEXT)"
    )
    connection.executemany("INSERT INTO work_items VALUES (?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


def _manifests(tmp_path, *, stale_action=None, failed_action=None):
    history = tmp_path / "history.sqlite"
    receipts_db = tmp_path / "receipts.sqlite"
    sample = tmp_path / "sample.json"
    receipts = tmp_path / "receipts.json"
    _history(history)
    sample.write_text(json.dumps({"tickets": ["REV-1", "REV-2"]}))
    rows = []
    families = {}
    counter = 0
    for family in FAMILIES:
        families[family] = []
        for action in REQUIRED_ACTIONS[family]:
            counter += 1
            work_id = f"work-{counter}"
            updated_at = 5.0 if action == stale_action else 20.0
            state = "failed" if action == failed_action else "succeeded"
            rows.append((work_id, action, state, updated_at, "{}", '[{"ok":true}]'))
            families[family].append(
                {"action": action, "database": str(receipts_db), "work_id": work_id}
            )
    _receipt_db(receipts_db, rows)
    receipts.write_text(json.dumps({"families": families, "started_at": 10.0}))
    return history, sample, receipts


def test_replay_requires_nonempty_exact_sample(tmp_path):
    history, sample, receipts = _manifests(tmp_path)
    sample.write_text('{"tickets": []}')
    with pytest.raises(ValueError, match="at least one ticket"):
        replay_report(history, sample, receipts, expected_count=0)


def test_replay_rejects_unknown_ticket(tmp_path):
    history, sample, receipts = _manifests(tmp_path)
    sample.write_text('{"tickets": ["REV-1", "REV-999"]}')
    with pytest.raises(ValueError, match="missing or excluded"):
        replay_report(history, sample, receipts, expected_count=2)


def test_replay_rejects_missing_family_receipt(tmp_path):
    history, sample, receipts = _manifests(tmp_path)
    manifest = json.loads(receipts.read_text())
    del manifest["families"]["integration_pipeline"]
    receipts.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="no evidence for integration_pipeline"):
        replay_report(history, sample, receipts, expected_count=2)


def test_replay_rejects_stale_terminal_receipt(tmp_path):
    history, sample, receipts = _manifests(
        tmp_path, stale_action="salesforce.verify_report_execution"
    )
    with pytest.raises(ValueError, match="reporting_analytics is stale"):
        replay_report(history, sample, receipts, expected_count=2)


def test_replay_rejects_failed_receipt(tmp_path):
    history, sample, receipts = _manifests(
        tmp_path, failed_action="salesforce.rollback_update"
    )
    with pytest.raises(ValueError, match="is not successful evidence"):
        replay_report(history, sample, receipts, expected_count=2)


def test_replay_accepts_all_families_and_both_tickets(tmp_path):
    history, sample, receipts = _manifests(tmp_path)
    report = replay_report(history, sample, receipts, expected_count=2)
    assert report["success"] is True
    assert report["replayed_ticket_count"] == 2
    assert set(report["receipt_families"]) == set(FAMILIES)
