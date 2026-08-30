from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from .engine import Outcome
from .models import WorkItem
from .salesforce import CommandRunner, _failure_detail, _json_error, run_command


APEX_TEST_ACTION = "salesforce.apex_test"
_CLASS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


@dataclass(frozen=True)
class ApexTestRequest:
    classes: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ApexTestRequest:
        if set(payload) != {"classes"}:
            unexpected = sorted(set(payload) - {"classes"})
            missing = sorted({"classes"} - set(payload))
            details = []
            if missing:
                details.append("missing keys: classes")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError("invalid salesforce.apex_test payload (" + "; ".join(details) + ")")
        values = payload["classes"]
        if not isinstance(values, list) or not 1 <= len(values) <= 10:
            raise ValueError("classes must be a list containing 1 to 10 class API names")
        classes = tuple(values)
        if any(not isinstance(name, str) or not _CLASS_RE.fullmatch(name) for name in classes):
            raise ValueError("each class must be one Apex class API name")
        if len(classes) != len(set(classes)):
            raise ValueError("classes must not contain duplicates")
        return cls(classes)


class SalesforceApexTest:
    """Run named Apex test classes in Partial only and validate the summary."""

    def __init__(
        self,
        *,
        partial_alias: str,
        runner: CommandRunner = run_command,
        timeout_seconds: float = 660,
    ) -> None:
        self.partial_alias = partial_alias.strip()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = ApexTestRequest.from_payload(item.payload)
            if not self.partial_alias:
                raise ValueError("partial target alias is required")
            listed = self.runner(
                [
                    "sf",
                    "org",
                    "list",
                    "metadata",
                    "--metadata-type",
                    "ApexClass",
                    "--target-org",
                    self.partial_alias,
                    "--json",
                ],
                self.timeout_seconds,
            )
            if listed.returncode != 0:
                return Outcome.failed(
                    f"salesforce Apex class inventory failed@{self.partial_alias}: "
                    f"{_failure_detail(listed)}"
                )
            available = _parse_class_inventory(listed.stdout)
            missing = sorted(set(request.classes) - available)
            if missing:
                raise ValueError(f"classes absent from live Partial metadata: {', '.join(missing)}")
            argv = ["sf", "apex", "run", "test"]
            for name in request.classes:
                argv.extend(["--class-names", name])
            argv.extend(
                [
                    "--target-org",
                    self.partial_alias,
                    "--wait",
                    "10",
                    "--result-format",
                    "json",
                    "--code-coverage",
                    "--json",
                ]
            )
            completed = self.runner(argv, self.timeout_seconds)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"salesforce Apex test refused: {exc}")
        if completed.returncode != 0:
            return Outcome.failed(
                f"salesforce Apex test failed@{self.partial_alias}: {_failure_detail(completed)}"
            )
        try:
            summary = _parse_test_result(completed.stdout)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"salesforce Apex test response rejected: {exc}")

        result = {
            "environment": "partial",
            "target_alias": self.partial_alias,
            "classes": list(request.classes),
            **summary,
        }
        evidence = [
            {
                "kind": APEX_TEST_ACTION,
                "environment": "partial",
                "target_alias": self.partial_alias,
                "class_count": len(request.classes),
                "tests_ran": summary["tests_ran"],
                "passing": summary["passing"],
                "failing": summary["failing"],
                "test_run_id": summary["test_run_id"],
                "test_run_coverage": summary["test_run_coverage"],
                "live_class_inventory_validated": True,
                "production_execution": False,
            }
        ]
        return Outcome.success(result, evidence)


def _parse_class_inventory(stdout: str) -> set[str]:
    envelope = json.loads(stdout)
    if int(envelope.get("status", 0)) != 0:
        raise ValueError(_json_error(envelope) or "Salesforce CLI returned non-zero status")
    rows = envelope["result"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("Apex class inventory must be a non-empty list")
    names = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "ApexClass":
            raise ValueError("Apex class inventory returned an invalid row")
        name = row.get("fullName")
        if not isinstance(name, str) or not _CLASS_RE.fullmatch(name) or name in names:
            raise ValueError("Apex class inventory returned an invalid or duplicate name")
        names.add(name)
    return names


def _parse_test_result(stdout: str) -> dict[str, Any]:
    envelope = json.loads(stdout)
    if int(envelope.get("status", 0)) != 0:
        raise ValueError(_json_error(envelope) or "Salesforce CLI returned non-zero status")
    summary = envelope["result"]["summary"]
    tests_ran = _nonnegative_int(summary, "testsRan")
    passing = _nonnegative_int(summary, "passing")
    failing = _nonnegative_int(summary, "failing")
    skipped = _nonnegative_int(summary, "skipped")
    if tests_ran < 1:
        raise ValueError("Apex test run executed zero tests")
    if tests_ran != passing + failing + skipped:
        raise ValueError("Apex test summary counts do not reconcile")
    if summary.get("outcome") != "Passed" or failing != 0 or passing < 1:
        raise ValueError("Apex test summary did not prove a passing run")
    run_id = summary.get("testRunId")
    if not isinstance(run_id, str) or not run_id.startswith("707"):
        raise ValueError("Apex test summary has no valid test run id")
    coverage = summary.get("testRunCoverage")
    if not isinstance(coverage, str) or not re.fullmatch(r"\d+(?:\.\d+)?%", coverage):
        raise ValueError("Apex test summary has no valid run coverage")
    return {
        "outcome": "Passed",
        "tests_ran": tests_ran,
        "passing": passing,
        "failing": failing,
        "skipped": skipped,
        "test_run_id": run_id,
        "test_run_coverage": coverage,
        "org_wide_coverage": str(summary.get("orgWideCoverage") or ""),
    }


def _nonnegative_int(summary: dict[str, Any], key: str) -> int:
    value = summary[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Apex test summary {key} must be a non-negative integer")
    return value
