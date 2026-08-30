from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .history import FAMILIES, classify, exclusion_reason, load_tickets


REQUIRED_ACTIONS = {
    "investigation_query": {"salesforce.query"},
    "metadata_schema_access": {"salesforce.metadata_diff"},
    "flow_automation": {"salesforce.run_created_flow"},
    "apex_logic_tests": {"salesforce.author_source", "salesforce.apex_test"},
    "lwc_page_experience": {"salesforce.verify_lwc_deployment"},
    "reporting_analytics": {"salesforce.verify_report_execution"},
    "data_repair_migration": {
        "salesforce.update_records",
        "salesforce.rollback_update",
    },
    "integration_pipeline": {
        "salesforce.authenticated_get",
        "salesforce.verify_integration_execution",
    },
    "delivery_ci_release": {"delivery.verify_sandbox_deploy"},
}

FRESH_ACTIONS = {
    family: actions.copy() for family, actions in REQUIRED_ACTIONS.items()
}
FRESH_ACTIONS["apex_logic_tests"] = {"salesforce.apex_test"}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _load_receipt(reference: dict[str, Any], started_at: float) -> dict[str, Any]:
    database = Path(str(reference.get("database", ""))).resolve()
    work_id = str(reference.get("work_id", ""))
    expected_action = str(reference.get("action", ""))
    if not database.is_file() or not work_id or not expected_action:
        raise ValueError("receipt reference is incomplete")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT action, state, updated_at, result_json, evidence_json "
            "FROM work_items WHERE id = ?",
            (work_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError(f"receipt {work_id} does not exist")
    action, state, updated_at, result_json, evidence_json = row
    if action != expected_action:
        raise ValueError(f"receipt {work_id} action does not match its reference")
    if state != "succeeded" or not result_json or not json.loads(evidence_json):
        raise ValueError(f"receipt {work_id} is not successful evidence")
    fresh = float(updated_at) >= started_at
    return {
        "action": action,
        "database": str(database),
        "fresh": fresh,
        "updated_at": float(updated_at),
        "work_id": work_id,
    }


def replay_report(
    history_database: Path,
    sample_path: Path,
    receipts_path: Path,
    *,
    expected_count: int = 15,
) -> dict[str, Any]:
    sample = _read_json(sample_path)
    ticket_ids = sample.get("tickets")
    if not isinstance(ticket_ids, list) or not ticket_ids:
        raise ValueError("replay sample must contain at least one ticket")
    if len(ticket_ids) != expected_count or len(set(ticket_ids)) != len(ticket_ids):
        raise ValueError(f"replay sample must contain {expected_count} unique tickets")

    tickets = {ticket.external_id: ticket for ticket in load_tickets(history_database)}
    selected = []
    family_counts: Counter[str] = Counter()
    for ticket_id in ticket_ids:
        ticket = tickets.get(str(ticket_id))
        if ticket is None or exclusion_reason(ticket):
            raise ValueError(f"replay ticket is missing or excluded: {ticket_id}")
        families = classify(ticket)
        selected.append((ticket, families))
        family_counts.update(families)

    missing_sample_families = set(FAMILIES) - set(family_counts)
    if missing_sample_families:
        raise ValueError(
            "replay sample does not represent every family: "
            + ", ".join(sorted(missing_sample_families))
        )

    receipt_manifest = _read_json(receipts_path)
    started_at = receipt_manifest.get("started_at")
    family_references = receipt_manifest.get("families")
    if not isinstance(started_at, (int, float)) or not isinstance(family_references, dict):
        raise ValueError("receipt manifest is missing started_at or families")

    evidence: dict[str, list[dict[str, Any]]] = {}
    for family in FAMILIES:
        references = family_references.get(family)
        if not isinstance(references, list) or not references:
            raise ValueError(f"receipt manifest has no evidence for {family}")
        receipts = [_load_receipt(reference, float(started_at)) for reference in references]
        actions = {receipt["action"] for receipt in receipts}
        missing_actions = REQUIRED_ACTIONS[family] - actions
        if missing_actions:
            raise ValueError(
                f"receipt evidence for {family} is incomplete: "
                + ", ".join(sorted(missing_actions))
            )
        fresh_actions = {
            receipt["action"] for receipt in receipts if receipt["fresh"]
        }
        missing_fresh = FRESH_ACTIONS[family] - fresh_actions
        if missing_fresh:
            raise ValueError(
                f"receipt evidence for {family} is stale: "
                + ", ".join(sorted(missing_fresh))
            )
        evidence[family] = receipts

    replayed = [
        {
            "families": list(families),
            "ticket": ticket.external_id,
            "title": ticket.title,
        }
        for ticket, families in selected
        if set(families) <= set(evidence)
    ]
    return {
        "expected_ticket_count": expected_count,
        "family_occurrences": dict(sorted(family_counts.items())),
        "fresh_replay_started_at": float(started_at),
        "receipt_families": evidence,
        "replayed_ticket_count": len(replayed),
        "replayed_tickets": replayed,
        "success": len(replayed) == expected_count,
    }


def render_replay_report(
    history_database: Path,
    sample_path: Path,
    receipts_path: Path,
    *,
    expected_count: int = 15,
) -> str:
    return json.dumps(
        replay_report(
            history_database,
            sample_path,
            receipts_path,
            expected_count=expected_count,
        ),
        indent=2,
        sort_keys=True,
    )
