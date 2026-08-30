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


def test_history_classifier_does_not_treat_sf_api_names_or_ci_as_integrations():
    ticket = _ticket(
        "REV-4",
        "Repair Flow and deploy",
        "Query FlowDefinitionView ApiName, then use the CI deployment pipeline",
    )
    assert "integration_pipeline" not in classify(ticket)


def test_history_classifier_detects_external_salesforce_callout_work():
    ticket = _ticket(
        "REV-5",
        "Repair HubSpot catalog callout",
        "Update the Named Credential and live sync",
    )
    assert "integration_pipeline" in classify(ticket)


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


def test_delivery_counts_only_after_governed_sandbox_deploy_receipt():
    ticket = _ticket("REV-9", "Deploy release", "git PR CI go-live")
    before = coverage_report([ticket], {"delivery.verify_pr"})
    after = coverage_report([ticket], {"delivery.verify_sandbox_deploy"})
    assert before["covered_ticket_count"] == 0
    assert after["covered_ticket_count"] == 1
    assert after["admitted_families"] == ["delivery_ci_release"]


def test_apex_counts_only_after_source_authoring_is_admitted():
    ticket = _ticket("REV-10", "Repair Apex controller", "test class and deploy")
    before = coverage_report(
        [ticket], {"salesforce.apex_test", "delivery.verify_sandbox_deploy"}
    )
    after = coverage_report(
        [ticket],
        {
            "salesforce.author_source",
            "salesforce.apex_test",
            "delivery.verify_sandbox_deploy",
        },
    )
    assert before["covered_ticket_count"] == 0
    assert after["covered_ticket_count"] == 1


def test_flow_counts_only_after_runtime_proof_is_admitted():
    ticket = _ticket("REV-11", "Repair renewal Flow", "automation deploy")
    before = coverage_report([ticket], {"salesforce.verify_flow_activation"})
    after = coverage_report(
        [ticket],
        {"salesforce.run_created_flow", "delivery.verify_sandbox_deploy"},
    )
    assert before["covered_ticket_count"] == 0
    assert after["covered_ticket_count"] == 1


def test_data_repair_counts_only_after_verified_update_is_admitted():
    ticket = _ticket("REV-12", "Backfill stale Account values", "data repair")
    before = coverage_report([ticket], {"salesforce.query"})
    after = coverage_report(
        [ticket], {"salesforce.query", "salesforce.update_records"}
    )
    assert before["covered_ticket_count"] == 0
    assert after["covered_ticket_count"] == 1


def test_lwc_counts_only_after_live_deployment_proof_is_admitted():
    ticket = _ticket("REV-13", "Repair Lightning wizard UI", "LWC deploy")
    before = coverage_report([ticket], {"salesforce.create_lwc_source"})
    after = coverage_report(
        [ticket],
        {"salesforce.verify_lwc_deployment", "delivery.verify_sandbox_deploy"},
    )
    assert before["covered_ticket_count"] == 0
    assert after["covered_ticket_count"] == 1


def test_reporting_counts_only_after_live_execution_is_admitted():
    ticket = _ticket("REV-14", "Build opportunity report", "analytics deploy")
    before = coverage_report([ticket], {"salesforce.create_report_source"})
    after = coverage_report(
        [ticket],
        {"salesforce.verify_report_execution", "delivery.verify_sandbox_deploy"},
    )
    assert before["covered_ticket_count"] == 0
    assert after["covered_ticket_count"] == 1


def test_integration_counts_only_after_live_external_execution_is_admitted():
    ticket = _ticket("REV-15", "Build exchange-rate callout", "REST endpoint integration")
    before = coverage_report([ticket], {"salesforce.create_integration_source"})
    after = coverage_report(
        [ticket],
        {"salesforce.verify_integration_execution", "delivery.verify_sandbox_deploy"},
    )
    assert before["covered_ticket_count"] == 0
    assert after["covered_ticket_count"] == 1


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
