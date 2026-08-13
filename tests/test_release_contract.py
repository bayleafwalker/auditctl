from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from verification.validate_release_contract import (
    _adapter_requirement,
    _locked_adapter_requirement,
    _validate_adapter_pin,
    validate_wheel,
)

WORKFLOW = ROOT / ".github" / "workflows" / "release-auditctl.yaml"


def test_adapter_dependency_is_an_immutable_github_wheel_pin() -> None:
    requirement = _adapter_requirement()
    lock_url, lock_digest = _locked_adapter_requirement()
    digest = _validate_adapter_pin(requirement)

    assert requirement.split("#", 1)[0] == lock_url
    assert digest == lock_digest
    assert digest == "0037898a4c9f01720a42302365b0172ecd203732070326ea2abdf549a44bf0c2"


def test_release_workflow_is_tag_only_and_github_only() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'tags:\n      - "auditctl-v*"' in workflow
    assert "workflow_dispatch" not in workflow
    assert "pypi" not in workflow.lower()
    assert "gh release upload" not in workflow
    assert "uv publish" not in workflow
    assert "attestations: write" in workflow
    assert "contents: write" in workflow
    assert "id-token: write" in workflow
    assert "--verify-tag" in workflow
    assert "uses: actions/attest@v4" in workflow


def test_release_workflow_runs_tests_before_one_gated_build() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count("uv build --wheel --out-dir dist") == 1
    assert workflow.index("uv run pytest -q") < workflow.index("uv build --wheel --out-dir dist")
    assert workflow.index("uv build --wheel --out-dir dist") < workflow.index(
        "verification/validate_release_contract.py"
    )
    assert workflow.index("uses: actions/attest@v4") < workflow.index(
        'gh release create "$tag"'
    )


@pytest.mark.skipif(
    not list((ROOT / "dist").glob("auditctl-0.1.1-*.whl")),
    reason="release wheel is built by the release workflow",
)
def test_built_wheel_satisfies_release_contract() -> None:
    wheels = sorted((ROOT / "dist").glob("auditctl-0.1.1-*.whl"))
    assert len(wheels) == 1
    validate_wheel(wheels[0], tag=f"auditctl-v{wheels[0].name.split('-')[1]}")
