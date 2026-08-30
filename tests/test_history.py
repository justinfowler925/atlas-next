from __future__ import annotations

import sqlite3

from atlas_next.history import HistoricalTicket, classify, coverage_report, exclusion_reason


def _ticket(ticket, title, goal="", status="done"):
    return HistoricalTicket(ticket, title, goal, status)


def test_history_classifier_excludes_non_user_work():
    assert exclusion_reason(_ticket("REV-1", "[Atlas5 proof burn-in] flow_create")) == (
        "synthetic_acceptance"
    )
    assert exclusion_reason(_ticket("REV-2", "Brutus conversation stack")) == (
        "atlas_or_brutus_internal"
    )
    assert exclusion_reason(_ticket("REV-3", "Video library", "Lane: `out-of-lane`")) == (
        "out_of_salesforce_lane"
    )


def test_history_classifier_is_multilabel_for_real_compound_work():
    ticket = _ticket(
        "REV-393",
        "CS Handoff Executive Sponsor lookup must change",
        "salesforce_soql.md → salesforce_lwc.md → salesforce_apex.md; field and flow update; deploy",
    )
    assert set(classify(ticket)) == {
        "investigation_query",
        "metadata_schema_access",
        "flow_automation",
        "apex_logic_tests",
        "lwc_page_experience",
        "delivery_ci_release",
    }


def test_coverage_requires_every_family_for_a_ticket():
    tickets = [
        _ticket("REV-1", "Audit stale field", "salesforce_soql and metadata"),
        _ticket("REV-2", "Repair Flow", "salesforce_soql then salesforce_flow"),
    ]
    report = coverage_report(
        tickets,
        {"salesforce.query", "salesforce.metadata_diff", "salesforce.apex_test"},
    )
    assert report["included_ticket_count"] == 2
    assert report["covered_ticket_count"] == 1
    assert report["weighted_ticket_coverage"] == 0.5
    assert report["admitted_families"] == ["investigation_query", "metadata_schema_access"]


def test_history_database_is_opened_read_only(tmp_path):
    database = tmp_path / "history.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE work_items (external_id TEXT, title TEXT, goal TEXT, status TEXT)"
    )
    connection.execute("INSERT INTO work_items VALUES ('REV-9','Report','salesforce_reporting','done')")
    connection.commit()
    connection.close()

    from atlas_next.history import load_tickets

    assert load_tickets(database)[0].external_id == "REV-9"
