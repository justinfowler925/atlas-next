from __future__ import annotations

import hashlib
import json
import re
import subprocess
import xml.sax.saxutils as saxutils
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .engine import Outcome
from .models import WorkItem
from .salesforce import CommandResult, _failure_detail


CREATE_INTEGRATION_SOURCE_ACTION = "salesforce.create_integration_source"
_APEX_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9]{2,35}$")
_JSON_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/\-]{0,300}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{3,253}$")


@dataclass(frozen=True)
class CreateIntegrationSourceRequest:
    name: str
    base_url: str
    path: str
    marker_field: str
    expected_marker: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CreateIntegrationSourceRequest:
        expected = {"name", "base_url", "path", "marker_field", "expected_marker"}
        if set(payload) != expected:
            raise ValueError(
                "payload must contain only name, base_url, path, marker_field, and expected_marker"
            )
        name = payload["name"]
        base_url = payload["base_url"]
        path = payload["path"]
        marker_field = payload["marker_field"]
        expected_marker = payload["expected_marker"]
        if not isinstance(name, str) or not _APEX_NAME_RE.fullmatch(name):
            raise ValueError("name must be one bounded PascalCase Apex class name")
        if not isinstance(base_url, str):
            raise ValueError("base_url must be one HTTPS origin")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not _HOST_RE.fullmatch(parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be one credential-free HTTPS origin")
        normalized_url = f"https://{parsed.hostname.lower()}"
        if not isinstance(path, str) or not _PATH_RE.fullmatch(path) or "//" in path:
            raise ValueError("path must be one bounded absolute URL path without a query")
        if not isinstance(marker_field, str) or not _JSON_FIELD_RE.fullmatch(marker_field):
            raise ValueError("marker_field must be one top-level JSON field")
        if (
            not isinstance(expected_marker, str)
            or not 1 <= len(expected_marker) <= 100
            or any(ord(character) < 32 for character in expected_marker)
        ):
            raise ValueError("expected_marker must be 1 to 100 printable characters")
        return cls(name, normalized_url, path, marker_field, expected_marker)


ProjectRunner = Callable[[Sequence[str], Path, float], CommandResult]


def run_project_command(argv: Sequence[str], cwd: Path, timeout_seconds: float) -> CommandResult:
    completed = subprocess.run(
        list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout_seconds, check=False
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class CreateIntegrationSource:
    """Create one bounded Apex REST GET integration, mock test, and Remote Site."""

    def __init__(
        self,
        *,
        project_dir: Path,
        runner: ProjectRunner = run_project_command,
        timeout_seconds: float = 30,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        created: list[tuple[Path, bytes]] = []
        try:
            request = CreateIntegrationSourceRequest.from_payload(item.payload)
            if not (self.project_dir / "sfdx-project.json").is_file():
                raise ValueError("configured project_dir is not a Salesforce project")
            git_root = Path(
                self._git(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
            ).resolve()
            branch = self._git(["git", "branch", "--show-current"]).stdout.strip()
            if not branch or branch in {"main", "master"}:
                raise ValueError("integration creation requires a named non-main branch")
            git_dir = Path(
                self._git(["git", "rev-parse", "--path-format=absolute", "--git-dir"])
                .stdout.strip()
            ).resolve()
            common_dir = Path(
                self._git(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
                .stdout.strip()
            ).resolve()
            if git_dir == common_dir:
                raise ValueError("integration creation requires an isolated linked worktree")
            if self._git(["git", "status", "--porcelain=v1"]).stdout:
                raise ValueError("integration creation requires a clean worktree")
            project_prefix = self.project_dir.relative_to(git_root).as_posix()
            sources = _render_sources(request)
            paths: dict[str, Path] = {}
            for suffix, content in sources.items():
                if suffix.startswith("remoteSiteSettings/"):
                    relative = f"{project_prefix}/force-app/main/default/{suffix}"
                else:
                    relative = f"{project_prefix}/force-app/main/default/classes/{suffix}"
                absolute = (git_root / relative).resolve()
                if not absolute.is_relative_to(git_root) or absolute.exists():
                    raise ValueError("integration source path already exists or escaped worktree")
                encoded = content.encode("utf-8")
                absolute.parent.mkdir(parents=True, exist_ok=True)
                absolute.write_bytes(encoded)
                created.append((absolute, encoded))
                paths[relative] = absolute
            status = self._git(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"]
            ).stdout
            dirty = {line[3:] for line in status.splitlines() if line.startswith("?? ")}
            if dirty != set(paths) or len(status.splitlines()) != len(paths):
                raise ValueError("integration creation did not produce exactly five source files")
            files = [
                {
                    "path": relative,
                    "sha256": hashlib.sha256(absolute.read_bytes()).hexdigest(),
                    "bytes": absolute.stat().st_size,
                }
                for relative, absolute in sorted(paths.items())
            ]
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            _remove_created(created)
            return Outcome.failed(f"Salesforce integration source creation refused: {exc}")

        result = {
            "environment": "partial",
            "type": "ApexRestCallout",
            "name": request.name,
            "base_url": request.base_url,
            "path": request.path,
            "marker_field": request.marker_field,
            "expected_marker": request.expected_marker,
            "git_root": str(git_root),
            "branch": branch,
            "files": files,
            "file_count": len(files),
            "has_mocked_test": True,
        }
        evidence = [
            {
                "kind": CREATE_INTEGRATION_SOURCE_ACTION,
                "environment": "partial",
                "name": request.name,
                "host": urlsplit(request.base_url).hostname,
                "file_count": len(files),
                "has_mocked_test": True,
                "production_execution": False,
                "metadata_deployed": False,
            }
        ]
        return Outcome.success(result, evidence)

    def _git(self, argv: Sequence[str]) -> CommandResult:
        completed = self.runner(argv, self.project_dir, self.timeout_seconds)
        if completed.returncode != 0:
            raise ValueError(f"integration creation git check failed: {_failure_detail(completed)}")
        return completed


def _render_sources(request: CreateIntegrationSourceRequest) -> dict[str, str]:
    apex_url = _apex_literal(request.base_url + request.path)
    field = _apex_literal(request.marker_field)
    expected = _apex_literal(request.expected_marker)
    mock_json = _apex_literal(json.dumps({request.marker_field: request.expected_marker}))
    service = f"""public with sharing class {request.name} {{
    private static final String ENDPOINT = '{apex_url}';

    public static String fetchMarker() {{
        HttpRequest request = new HttpRequest();
        request.setEndpoint(ENDPOINT);
        request.setMethod('GET');
        request.setTimeout(10000);
        HttpResponse response = new Http().send(request);
        if (response.getStatusCode() < 200 || response.getStatusCode() >= 300) {{
            throw new CalloutException('External service returned HTTP ' + response.getStatusCode());
        }}
        Map<String, Object> body = (Map<String, Object>) JSON.deserializeUntyped(response.getBody());
        Object marker = body.get('{field}');
        if (marker == null) {{
            throw new CalloutException('External response omitted the required marker');
        }}
        return String.valueOf(marker);
    }}
}}
"""
    test = f"""@IsTest
private class {request.name}Test {{
    private class SuccessMock implements HttpCalloutMock {{
        public HttpResponse respond(HttpRequest request) {{
            System.assertEquals('GET', request.getMethod());
            System.assertEquals('{apex_url}', request.getEndpoint());
            HttpResponse response = new HttpResponse();
            response.setStatusCode(200);
            response.setBody('{mock_json}');
            return response;
        }}
    }}

    @IsTest
    static void fetchesExpectedMarker() {{
        Test.setMock(HttpCalloutMock.class, new SuccessMock());
        Test.startTest();
        String actual = {request.name}.fetchMarker();
        Test.stopTest();
        System.assertEquals('{expected}', actual);
    }}
}}
"""
    meta = """<?xml version="1.0" encoding="UTF-8"?>
<ApexClass xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>65.0</apiVersion>
    <status>Active</status>
</ApexClass>
"""
    remote = f"""<?xml version="1.0" encoding="UTF-8"?>
<RemoteSiteSetting xmlns="http://soap.sforce.com/2006/04/metadata">
    <description>Atlas-governed acceptance integration endpoint</description>
    <disableProtocolSecurity>false</disableProtocolSecurity>
    <isActive>true</isActive>
    <url>{saxutils.escape(request.base_url)}</url>
</RemoteSiteSetting>
"""
    return {
        f"{request.name}.cls": service,
        f"{request.name}.cls-meta.xml": meta,
        f"{request.name}Test.cls": test,
        f"{request.name}Test.cls-meta.xml": meta,
        f"remoteSiteSettings/{request.name}.remoteSite-meta.xml": remote,
    }


def _apex_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _remove_created(created: list[tuple[Path, bytes]]) -> None:
    parents: set[Path] = set()
    for path, expected in reversed(created):
        try:
            if path.is_file() and not path.is_symlink() and path.read_bytes() == expected:
                path.unlink()
                parents.add(path.parent)
        except OSError:
            continue
    for parent in sorted(parents, key=lambda value: len(value.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            continue
