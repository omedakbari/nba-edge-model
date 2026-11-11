"""Fails if any GitHub workflow regresses on the hardening rules (repo is public)."""

import re
from pathlib import Path

import pytest
import yaml

GH = Path(__file__).resolve().parents[2] / ".github"
WORKFLOWS = sorted((GH / "workflows").glob("*.yml")) + sorted((GH / "workflows").glob("*.yaml"))
SHA = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")
PRIVILEGED_TRIGGERS = {"pull_request_target", "workflow_run"}


def _load(path):
    doc = yaml.safe_load(path.read_text())
    doc["on"] = doc.pop(True, doc.get("on"))       # YAML 1.1 reads the bare key `on` as True
    return doc


def _steps(doc):
    for name, job in doc["jobs"].items():
        for i, step in enumerate(job.get("steps", [])):
            yield name, i, step


def test_there_are_workflows_to_check():
    assert {p.name for p in WORKFLOWS} >= {"ingest.yml", "nightly.yml", "healthcheck.yml", "tests.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit_sha(path):
    for job, i, step in _steps(_load(path)):
        uses = step.get("uses")
        if uses and not uses.startswith("./"):
            assert SHA.match(uses), f"{path.name} {job} step {i}: {uses!r} is not pinned to a 40-char SHA"
    for line in path.read_text().splitlines():                  # and the version is noted for humans
        if re.search(r"uses:\s*\S+@[0-9a-f]{40}", line):
            assert re.search(r"#\s*v\d", line), f"{path.name}: add a '# vX.Y.Z' comment: {line.strip()}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_least_privilege_permissions(path):
    doc = _load(path)
    assert doc.get("permissions") == {}, f"{path.name}: top-level 'permissions: {{}}' required"
    for name, job in doc["jobs"].items():
        perms = job.get("permissions")
        assert isinstance(perms, dict), f"{path.name} {name}: job must declare its permissions"
        assert all(v in ("read", "none") for v in perms.values()), f"{path.name} {name}: write scope {perms}"
        assert set(perms) <= {"contents"}, f"{path.name} {name}: unexpected scopes {perms}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_privileged_triggers(path):
    triggers = _load(path)["on"]
    names = set(triggers) if isinstance(triggers, (dict, list)) else {triggers}
    assert not names & PRIVILEGED_TRIGGERS, f"{path.name}: uses {names & PRIVILEGED_TRIGGERS}"
    assert "pull_request_target" not in path.read_text()


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_expressions_inside_run_scripts(path):
    """${{ }} in a run: script is pasted into the shell before it runs (script injection). Use env:."""
    for job, i, step in _steps(_load(path)):
        assert "${{" not in str(step.get("run", "")), f"{path.name} {job} step {i}: move ${{{{ }}}} into env:"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_does_not_keep_the_token(path):
    for job, i, step in _steps(_load(path)):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert (step.get("with") or {}).get("persist-credentials") is False, \
                f"{path.name} {job}: checkout needs persist-credentials: false"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_secret_values_are_masked_before_anything_else_runs(path):
    for name, job in _load(path)["jobs"].items():
        steps = job.get("steps", [])
        uses_secrets = [i for i, s in enumerate(steps) if "secrets." in str(s.get("env", ""))]
        if not uses_secrets:
            continue
        mask = [i for i, s in enumerate(steps) if "prop_worker.masking" in str(s.get("run", ""))]
        assert mask, f"{path.name} {name}: uses secrets but has no masking step"
        assert mask[0] == uses_secrets[0] == 1, \
            f"{path.name} {name}: the masking step must be the first step after checkout and the first to see secrets"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_artifacts_expire_quickly(path):
    for job, i, step in _steps(_load(path)):
        if str(step.get("uses", "")).startswith("actions/upload-artifact@"):
            assert int((step.get("with") or {}).get("retention-days", 90)) <= 7, f"{path.name}: retention-days <= 7"


def test_dependabot_watches_actions_and_pip():
    cfg = yaml.safe_load((GH / "dependabot.yml").read_text())
    assert {u["package-ecosystem"] for u in cfg["updates"]} >= {"github-actions", "pip"}
