from __future__ import annotations

import hashlib

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.lwc_source import CREATE_LWC_SOURCE_ACTION, CreateLwcSource, CreateLwcSourceRequest
from atlas_next.salesforce import CommandResult


NAME = "atlasAcceptanceCard"
FILES = {
    f"{NAME}.js": "import { LightningElement } from 'lwc'; export default class AtlasAcceptanceCard extends LightningElement {}",
    f"{NAME}.html": "<template><button>Run</button></template>",
    f"{NAME}.css": ":host { color: var(--slds-g-color-on-surface-1, #181818); }",
    f"{NAME}.js-meta.xml": """<LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata"><isExposed>true</isExposed><targets><target>lightning__RecordPage</target></targets></LightningComponentBundle>""",
    f"__tests__/{NAME}.test.js": "createElement(); document.body.appendChild(element); button.click(); expect(value);",
}


def test_lwc_contract_has_no_path_target_or_command_escape():
    for key in ("path", "target_org", "command", "deploy"):
        with pytest.raises(ValueError, match="only name and files"):
            CreateLwcSourceRequest.from_payload({"name": NAME, "files": FILES, key: "x"})
    with pytest.raises(ValueError, match="exact JS"):
        CreateLwcSourceRequest.from_payload(
            {"name": NAME, "files": {key: value for key, value in FILES.items() if not key.endswith(".css")}}
        )


def test_lwc_creation_writes_only_complete_derived_bundle(tmp_path):
    git_root = tmp_path / "worktree"
    project = git_root / "salesforce"
    project.mkdir(parents=True)
    (project / "sfdx-project.json").write_text("{}")
    bundle = f"salesforce/force-app/main/default/lwc/{NAME}"

    def runner(argv, _cwd, _timeout):
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--show-toplevel":
            return CommandResult(0, str(git_root), "")
        if argv == ["git", "branch", "--show-current"]:
            return CommandResult(0, "justin-fowler/lwc\n", "")
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--git-dir":
            return CommandResult(0, str(tmp_path / "repo/.git/worktrees/lwc"), "")
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--git-common-dir":
            return CommandResult(0, str(tmp_path / "repo/.git"), "")
        if argv == ["git", "status", "--porcelain=v1"]:
            rows = []
            for filename in FILES:
                path = f"{bundle}/{filename}"
                if (git_root / path).exists():
                    rows.append(f"?? {path}")
            return CommandResult(0, "\n".join(rows) + ("\n" if rows else ""), "")
        raise AssertionError(argv)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(CREATE_LWC_SOURCE_ACTION, {"name": NAME, "files": FILES})
        completed = Engine(
            store,
            {CREATE_LWC_SOURCE_ACTION: CreateLwcSource(project_dir=project, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["file_count"] == 5
    assert completed.evidence[0]["has_behavioral_test"] is True
    css = git_root / bundle / f"{NAME}.css"
    assert next(row for row in completed.result["files"] if row["path"].endswith(".css"))[
        "sha256"
    ] == hashlib.sha256(css.read_bytes()).hexdigest()
