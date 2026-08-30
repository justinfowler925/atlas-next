from __future__ import annotations

import hashlib

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.report_source import (
    CREATE_REPORT_SOURCE_ACTION,
    CreateReportSource,
    CreateReportSourceRequest,
)
from atlas_next.salesforce import CommandResult


REPORT = """<?xml version="1.0" encoding="UTF-8"?>
<Report xmlns="http://soap.sforce.com/2006/04/metadata">
    <columns><field>OPPORTUNITY_NAME</field></columns>
    <columns><field>STAGE_NAME</field></columns>
    <format>Tabular</format>
    <name>Atlas Acceptance Opportunity Report</name>
    <reportType>Opportunity</reportType>
    <scope>organization</scope>
    <showDetails>true</showDetails>
</Report>
"""


def test_report_contract_has_no_folder_path_target_or_command_escape():
    for key in ("folder", "path", "target_org", "command", "deploy"):
        with pytest.raises(ValueError, match="only name and content"):
            CreateReportSourceRequest.from_payload(
                {"name": "Atlas_Acceptance_Opportunity_Report", "content": REPORT, key: "x"}
            )


def test_report_creation_writes_one_derived_public_report(tmp_path):
    git_root = tmp_path / "worktree"
    project = git_root / "salesforce"
    project.mkdir(parents=True)
    (project / "sfdx-project.json").write_text("{}")
    relative = (
        "salesforce/force-app/main/default/reports/unfiled$public/"
        "Atlas_Acceptance_Opportunity_Report.report-meta.xml"
    )

    def runner(argv, _cwd, _timeout):
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--show-toplevel":
            return CommandResult(0, str(git_root), "")
        if argv == ["git", "branch", "--show-current"]:
            return CommandResult(0, "justin-fowler/report\n", "")
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--git-dir":
            return CommandResult(0, str(tmp_path / "repo/.git/worktrees/report"), "")
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--git-common-dir":
            return CommandResult(0, str(tmp_path / "repo/.git"), "")
        if argv == ["git", "status", "--porcelain=v1"]:
            return CommandResult(0, "", "")
        if argv == ["git", "status", "--porcelain=v1", "--untracked-files=all"]:
            return CommandResult(0, f"?? {relative}\n", "")
        raise AssertionError(argv)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(
            CREATE_REPORT_SOURCE_ACTION,
            {"name": "Atlas_Acceptance_Opportunity_Report", "content": REPORT},
        )
        completed = Engine(
            store,
            {CREATE_REPORT_SOURCE_ACTION: CreateReportSource(project_dir=project, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    created = git_root / relative
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["folder"] == "unfiled$public"
    assert completed.result["files"][0]["sha256"] == hashlib.sha256(
        created.read_bytes()
    ).hexdigest()


def test_report_rejects_unbounded_columns_and_wrong_root():
    columns = "".join("<columns><field>NAME</field></columns>" for _ in range(13))
    with pytest.raises(ValueError, match="1 to 12"):
        CreateReportSourceRequest.from_payload(
            {
                "name": "Atlas_Report",
                "content": REPORT.replace(
                    "<columns><field>OPPORTUNITY_NAME</field></columns>\n    "
                    "<columns><field>STAGE_NAME</field></columns>",
                    columns,
                ),
            }
        )
    with pytest.raises(ValueError, match="Report document"):
        CreateReportSourceRequest.from_payload(
            {
                "name": "Atlas_Report",
                "content": REPORT.replace("<Report ", "<Dashboard ").replace(
                    "</Report>", "</Dashboard>"
                ),
            }
        )
