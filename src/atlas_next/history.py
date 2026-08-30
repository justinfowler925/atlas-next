from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FAMILIES = (
    "investigation_query",
    "metadata_schema_access",
    "flow_automation",
    "apex_logic_tests",
    "lwc_page_experience",
    "reporting_analytics",
    "data_repair_migration",
    "integration_pipeline",
    "delivery_ci_release",
)

CAPABILITY_FAMILY = {
    "salesforce.describe": "metadata_schema_access",
    "salesforce.count": "investigation_query",
    "salesforce.picklist_counts": "investigation_query",
    "salesforce.query": "investigation_query",
    "salesforce.metadata_diff": "metadata_schema_access",
    "salesforce.metadata_content_diff": "metadata_schema_access",
    # Admitted only at the end of the evidence-linked commit -> PR -> CI -> Partial chain.
    "delivery.verify_sandbox_deploy": "delivery_ci_release",
    # Admitted for Apex only after hash-locked authoring, governed delivery, and live tests.
    "salesforce.author_source": "apex_logic_tests",
    # Admitted only after deployed lineage, activation parity, and runtime output proof.
    "salesforce.run_created_flow": "flow_automation",
    # Admitted after live schema validation, all-or-none update, postcheck, and rollback proof.
    "salesforce.update_records": "data_repair_migration",
    # Admitted after Shine usability, behavioral Jest, governed deploy, and live metadata proof.
    "salesforce.verify_lwc_deployment": "lwc_page_experience",
    # A test runner alone is necessary evidence for Apex work, but cannot author or repair it.
    "salesforce.apex_test": None,
}

_SYNTHETIC = re.compile(r"\[atlas5 proof|burn[- ]?in|acceptance probe", re.I)
_ATLAS_INTERNAL = re.compile(r"\batlas5?\b|\bbrutus\b|worker|queue_depth|ingest_linear", re.I)
_OUT_OF_LANE = re.compile(r"lane:\s*`?out-of-lane", re.I)
_TEMPLATE = re.compile(r"\btemplate\b|operating rhythm|auto-authored specs", re.I)

_RULES = {
    "investigation_query": re.compile(
        r"salesforce_soql|\bsoql\b|audit|investigat|reconcil|drift|fault|failure|broken|"
        r"root cause|missing|required field|inactive|stale|health score|coverage", re.I
    ),
    "metadata_schema_access": re.compile(
        r"salesforce_metadata|metadata|custom field|\bfield\b|picklist|validation rule|"
        r"permission|\bfls\b|profile|role|layout|flexipage|record type|schema", re.I
    ),
    "flow_automation": re.compile(
        r"salesforce_flow|\bflow\b|automation|workflow|approval|renewal copier|intake|"
        r"stage gate|stage-gate|notification", re.I
    ),
    "apex_logic_tests": re.compile(
        r"salesforce_apex|\bapex\b|\btrigger\b|controller|queueable|batchable|schedulable|"
        r"test class|failing tests", re.I
    ),
    "lwc_page_experience": re.compile(
        r"salesforce_lwc|\blwc\b|lightning|wizard|cockpit|workspace|page layout|\bui\b|"
        r"client facing|client-facing", re.I
    ),
    "reporting_analytics": re.compile(
        r"salesforce_reporting|\breport\b|dashboard|analytics|scorecard|metric|counter|"
        r"tracker|worklist", re.I
    ),
    "data_repair_migration": re.compile(
        r"data_loader|bulk import|\bimport\b|backfill|dedup|duplicate|data hygiene|"
        r"migration|re-parent|reparent|delete .*record|assignment|write-back|sync contacts", re.I
    ),
    "integration_pipeline": re.compile(
        r"salesforce_integration|integration|zoom|teams meeting|hubspot|slack|google|"
        r"calendar|webhook|warehouse|superset|pipeline|rest endpoint|api", re.I
    ),
    "delivery_ci_release": re.compile(
        r"salesforce_deploy|salesforce_deployment|\bdeploy\b|\bci\b|github|prod rollout|"
        r"source control|\bgit\b|release|go-live|go live", re.I
    ),
}


@dataclass(frozen=True)
class HistoricalTicket:
    external_id: str
    title: str
    goal: str
    status: str


def load_tickets(database: Path) -> list[HistoricalTicket]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT external_id, title, goal, status FROM work_items "
            "WHERE external_id GLOB 'REV-[0-9]*' ORDER BY CAST(SUBSTR(external_id, 5) AS INTEGER)"
        ).fetchall()
    finally:
        connection.close()
    return [HistoricalTicket(*(str(value or "") for value in row)) for row in rows]


def exclusion_reason(ticket: HistoricalTicket) -> str | None:
    text = f"{ticket.title}\n{ticket.goal}"
    if _SYNTHETIC.search(text):
        return "synthetic_acceptance"
    if _OUT_OF_LANE.search(text):
        return "out_of_salesforce_lane"
    if _ATLAS_INTERNAL.search(ticket.title):
        return "atlas_or_brutus_internal"
    if _TEMPLATE.search(ticket.title):
        return "template_or_governance"
    return None


def classify(ticket: HistoricalTicket) -> tuple[str, ...]:
    text = f"{ticket.title}\n{ticket.goal}"
    families = tuple(family for family, pattern in _RULES.items() if pattern.search(text))
    return families or ("investigation_query",)


def coverage_report(tickets: list[HistoricalTicket], capabilities: set[str]) -> dict[str, Any]:
    admitted_families = {
        family
        for capability in capabilities
        if (family := CAPABILITY_FAMILY.get(capability)) is not None
    }
    excluded = Counter()
    included = []
    family_counts = Counter()
    covered = []
    for ticket in tickets:
        reason = exclusion_reason(ticket)
        if reason:
            excluded[reason] += 1
            continue
        families = classify(ticket)
        family_counts.update(families)
        row = {"ticket": ticket.external_id, "families": list(families)}
        included.append(row)
        if set(families) <= admitted_families:
            covered.append(row)
    denominator = len(included)
    numerator = len(covered)
    return {
        "source_ticket_count": len(tickets),
        "included_ticket_count": denominator,
        "excluded_ticket_count": sum(excluded.values()),
        "excluded_by_reason": dict(sorted(excluded.items())),
        "family_occurrences": dict(sorted(family_counts.items())),
        "admitted_families": sorted(admitted_families),
        "covered_ticket_count": numerator,
        "weighted_ticket_coverage": 0 if denominator == 0 else round(numerator / denominator, 4),
        "covered_tickets": covered,
        "uncovered_tickets": [row for row in included if row not in covered],
    }


def render_report(database: Path, capabilities: set[str]) -> str:
    return json.dumps(coverage_report(load_tickets(database), capabilities), indent=2, sort_keys=True)
