from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import Outcome
from .models import WorkItem
from .salesforce import CommandRunner, _failure_detail, require_partial_target, run_command


AUTHENTICATED_GET_ACTION = "salesforce.authenticated_get"
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9_?&=.,%+:/-]{0,499}$")
_STATUS_RE = re.compile(r"\|DEBUG\|ATLAS_AUTH_GET_STATUS=(\d{3})(?:\r?$)", re.M)
_HASH_RE = re.compile(r"\|DEBUG\|ATLAS_AUTH_GET_BODY_SHA256=([0-9a-f]{64})(?:\r?$)", re.M)
_BYTES_RE = re.compile(r"\|DEBUG\|ATLAS_AUTH_GET_BODY_BYTES=(\d+)(?:\r?$)", re.M)


@dataclass(frozen=True)
class AuthenticatedGetRequest:
    named_credential: str
    external_credential: str
    credential_parameter: str
    path: str
    expected_status: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AuthenticatedGetRequest:
        expected_keys = {
            "credential_parameter",
            "expected_status",
            "external_credential",
            "named_credential",
            "path",
        }
        if set(payload) != expected_keys:
            raise ValueError("payload must contain only authenticated GET fields")
        names = [
            payload["named_credential"],
            payload["external_credential"],
            payload["credential_parameter"],
        ]
        if any(not isinstance(value, str) or not _NAME_RE.fullmatch(value) for value in names):
            raise ValueError("credential names must be safe Salesforce developer names")
        path = payload["path"]
        if not isinstance(path, str) or not _PATH_RE.fullmatch(path) or ".." in path:
            raise ValueError("path must be one bounded relative HTTP path")
        status = payload["expected_status"]
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            raise ValueError("expected_status must be an HTTP status from 100 through 599")
        return cls(*names, path, status)


class SalesforceAuthenticatedGet:
    """Perform one secret-safe credential-bound GET from live Partial."""

    def __init__(
        self,
        *,
        partial_alias: str,
        artifact_root: Path,
        runner: CommandRunner = run_command,
        timeout_seconds: float = 120,
    ) -> None:
        self.partial_alias = partial_alias.strip()
        self.artifact_root = artifact_root.resolve()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = AuthenticatedGetRequest.from_payload(item.payload)
            if not self.partial_alias:
                raise ValueError("Partial alias is required")
            partial = require_partial_target(
                self.runner, self.partial_alias, self.timeout_seconds
            )
            endpoint = f"callout:{request.named_credential}{request.path}"
            merge_field = (
                "{!$Credential."
                f"{request.external_credential}.{request.credential_parameter}"
                "}"
            )
            script = (
                "HttpRequest request = new HttpRequest();\n"
                f"request.setEndpoint('{endpoint}');\n"
                "request.setMethod('GET');\n"
                "request.setTimeout(10000);\n"
                f"request.setHeader('Authorization', 'Bearer {merge_field}');\n"
                "HttpResponse response = new Http().send(request);\n"
                "String bodyText = response.getBody();\n"
                "if (bodyText == null) bodyText = '';\n"
                "String bodyHash = EncodingUtil.convertToHex(Crypto.generateDigest("
                "'SHA-256', Blob.valueOf(bodyText)));\n"
                "System.debug('ATLAS_AUTH_GET_STATUS=' + response.getStatusCode());\n"
                "System.debug('ATLAS_AUTH_GET_BODY_SHA256=' + bodyHash);\n"
                "System.debug('ATLAS_AUTH_GET_BODY_BYTES=' + Blob.valueOf(bodyText).size());\n"
                f"System.assertEquals({request.expected_status}, response.getStatusCode(), "
                "'Authenticated GET returned an unexpected status');\n"
            )
            self.artifact_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.artifact_root) as temporary:
                script_path = Path(temporary) / "authenticated-get.apex"
                script_path.write_text(script, encoding="utf-8")
                completed = self.runner(
                    [
                        "sf",
                        "apex",
                        "run",
                        "--file",
                        str(script_path),
                        "--target-org",
                        self.partial_alias,
                        "--json",
                    ],
                    self.timeout_seconds,
                )
            if completed.returncode != 0:
                raise ValueError(f"authenticated GET failed: {_failure_detail(completed)}")
            envelope = json.loads(completed.stdout)
            runtime = envelope.get("result", {})
            if runtime.get("compiled") is not True or runtime.get("success") is not True:
                raise ValueError("authenticated GET Apex did not compile and execute")
            logs = runtime.get("logs")
            if not isinstance(logs, str):
                raise ValueError("authenticated GET returned no Apex logs")
            status_match = _STATUS_RE.search(logs)
            hash_match = _HASH_RE.search(logs)
            bytes_match = _BYTES_RE.search(logs)
            if not status_match or not hash_match or not bytes_match:
                raise ValueError("authenticated GET evidence markers are incomplete")
            status = int(status_match.group(1))
            if status != request.expected_status:
                raise ValueError("authenticated GET status marker does not match expectation")
            body_hash = hash_match.group(1)
            body_bytes = int(bytes_match.group(1))
            log_hash = hashlib.sha256(logs.encode()).hexdigest()
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"authenticated GET refused: {exc}")

        result = {
            "body_bytes": body_bytes,
            "body_sha256": body_hash,
            "environment": "partial",
            "external_credential": request.external_credential,
            "http_status": status,
            "log_sha256": log_hash,
            "named_credential": request.named_credential,
            "path": request.path,
            "target_alias": self.partial_alias,
            "target_org_id": partial["org_id"],
        }
        evidence = [
            {
                "authenticated": True,
                "body_bytes": body_bytes,
                "body_sha256": body_hash,
                "environment": "partial",
                "external_callout": True,
                "http_status": status,
                "kind": AUTHENTICATED_GET_ACTION,
                "named_credential": request.named_credential,
                "production_execution": False,
            }
        ]
        return Outcome.success(result, evidence)
