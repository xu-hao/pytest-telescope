"""Filesystem and collection contracts exercised without importing test code."""

import json
import os
from pathlib import Path

import pytest

from pytest_telescope.model import DeclarationError, expand, load, regular_bytes


def declaration(root, tests):
    for name in tests:
        (root / name).write_text(
            "import pytest\n"
            "pytestmark = pytest.mark.telescope(inputs=[])\n"
            "raise AssertionError('must not import')\n"
        )
    (root / "pyproject.toml").write_text("[tool.pytest_telescope.defaults]\n")


def test_automatic_file_discovery_is_sorted_and_complete(tmp_path):
    declaration(tmp_path, ["test_second.py", "test_first.py"])
    manifest = load(tmp_path)
    assert manifest.inventory == ["test_first.py", "test_second.py"]
    assert manifest.units[0].tests == manifest.inventory
    (tmp_path / "test_added.py").write_text("raise AssertionError('must not import')\n")
    assert load(tmp_path).inventory == ["test_added.py", "test_first.py", "test_second.py"]


def test_custom_python_files_inventory_includes_new_test(tmp_path):
    declaration(tmp_path, ["check_one.py"])
    config = tmp_path / "pyproject.toml"
    config.write_text(
        '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n' + config.read_text()
    )
    assert load(tmp_path).inventory == ["check_one.py"]
    (tmp_path / "check_new.py").write_text("raise AssertionError('must not import')\n")
    (tmp_path / "test_ordinary.py").write_text("raise AssertionError('must not import')\n")
    assert load(tmp_path).inventory == ["check_new.py", "check_one.py"]


@pytest.mark.parametrize("filename", ["pytest.ini", "tox.ini", "setup.cfg", ".pytest.ini"])
def test_unsupported_pytest_configuration_is_rejected(tmp_path, filename):
    declaration(tmp_path, ["test_one.py"])
    (tmp_path / filename).write_text("[pytest]\npython_files = check_*.py\n")
    with pytest.raises(DeclarationError):
        load(tmp_path)


def test_regular_read_cannot_follow_replaced_parent(tmp_path, monkeypatch):
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (inside / "input.txt").write_text("declared")
    (outside / "input.txt").write_text("outside")
    original = os.open
    switched = False

    def race(path, flags, *args, **kwargs):
        nonlocal switched
        if path == "input.txt" and not switched:
            switched = True
            inside.rename(tmp_path / "retained")
            inside.symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    content, _ = regular_bytes(tmp_path, "inside/input.txt", 100)
    assert content == b"declared"


def test_leaf_symlink_is_never_followed_for_regular_read(tmp_path):
    (tmp_path / "outside.txt").write_text("not a regular declared input")
    (tmp_path / "input.txt").symlink_to("outside.txt")
    with pytest.raises(OSError):
        regular_bytes(tmp_path, "input.txt", 100)


def test_equivalent_executed_snapshots_ignore_normalized_source_metadata(tmp_path):
    from pytest_telescope.model import fingerprint, snapshot

    root = tmp_path / "source"
    root.mkdir()
    declaration(root, ["test_one.py"])
    manifest = load(root)
    before = snapshot(manifest, manifest.units[0], tmp_path / "before")
    for path in root.iterdir():
        os.utime(path, ns=(123_000_000_000, 456_000_000_000))
        path.chmod(0o600)
    after = snapshot(load(root), manifest.units[0], tmp_path / "after")
    assert fingerprint(before) == fingerprint(after)
    (root / "test_one.py").chmod(0o700)
    executable = snapshot(load(root), manifest.units[0], tmp_path / "executable")
    assert fingerprint(before) != fingerprint(executable)


def test_absolute_leaf_symlink_is_rejected_before_snapshot(tmp_path):
    declaration(tmp_path, ["test_one.py"])
    (tmp_path / "value.json").write_text("{}")
    (tmp_path / "alias.json").symlink_to(tmp_path / "value.json")
    config = tmp_path / "pyproject.toml"
    config.write_text(config.read_text() + 'inputs = ["value.json", "alias.json"]\n')
    with pytest.raises(DeclarationError, match="relative"):
        load(tmp_path)


def test_explicit_hidden_directory_glob_declares_actual_resources(tmp_path):
    declaration(tmp_path, ["test_one.py"])
    assets = tmp_path / "data" / ".assets"
    assets.mkdir(parents=True)
    (assets / "value.json").write_text("{}")
    config = tmp_path / "pyproject.toml"
    config.write_text(config.read_text() + 'inputs = ["data/.assets/*.json"]\n')
    (assets / "unrelated.txt").write_text("not selected by the glob")
    inputs = load(tmp_path).units[0].inputs
    assert "data/.assets/value.json" in inputs
    assert "data/.assets/unrelated.txt" not in inputs


def test_total_discovery_budget_bounds_repeated_nonmatching_globs(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    for number in range(100):
        (tmp_path / f"data-{number}.txt").write_text("x")
    patterns = [f"missing-{number}*" for number in range(2000)]
    (tmp_path / "test_subject.py").write_text(
        "import pytest\npytestmark = pytest.mark.telescope(inputs=" + json.dumps(patterns) + ")\n"
        "def test_subject(): pass\n"
    )
    with pytest.raises(DeclarationError, match="discovery"):
        load(tmp_path)


def test_anchored_glob_skips_unrelated_sibling_trees(tmp_path, monkeypatch):
    declaration(tmp_path, ["test_subject.py"])
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\nmax_visits = 50\ntest_roots = ["test_subject.py"]\n'
        '[tool.pytest_telescope.defaults]\ninputs = ["pkg/sub/**/*.py"]\n'
    )
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    for number in range(600):
        (unrelated / f"data-{number}.txt").write_text("x")
    module = tmp_path / "pkg" / "sub" / "mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("VALUE = 1\n")
    scanned = []
    original = os.scandir

    def scandir(path):
        scanned.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert expand(tmp_path, ["pkg/sub/**/*.py"], "inputs") == ["pkg/sub/mod.py"]
    assert tmp_path not in scanned
    assert unrelated not in scanned
    assert "pkg/sub/mod.py" in load(tmp_path).units[0].inputs


def test_glob_directory_self_match_and_missing_literal_prefix(tmp_path):
    directory = tmp_path / "pkg" / "sub"
    directory.mkdir(parents=True)
    (directory / "mod.py").write_text("VALUE = 1\n")
    assert expand(tmp_path, ["pkg/sub/**"], "inputs") == ["pkg/sub", "pkg/sub/mod.py"]
    assert expand(tmp_path, ["absent_dir/*.py"], "inputs") == []
    with pytest.raises(DeclarationError, match="metadata, cache and host environments"):
        expand(tmp_path, [".git/*.py"], "inputs")


def test_shared_pattern_is_expanded_once_across_distinct_declarations(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\nmax_visits = 65\ntest_roots = ["test_subject.py"]\n'
    )
    (tmp_path / "test_subject.py").write_text(
        "import pytest\n"
        '@pytest.mark.telescope(inputs=["shared/*.txt", "a.txt"])\n'
        "def test_a(): pass\n"
        '@pytest.mark.telescope(inputs=["shared/*.txt", "b.txt"])\n'
        "def test_b(): pass\n"
    )
    shared = tmp_path / "shared"
    shared.mkdir()
    expected = {f"shared/data-{number}.txt" for number in range(40)}
    for name in expected | {"a.txt", "b.txt"}:
        (tmp_path / name).write_text("x")
    manifest = load(tmp_path)
    assert {frozenset(unit.inputs) for unit in manifest.declarations.values()} == {
        frozenset(expected | {"a.txt"}),
        frozenset(expected | {"b.txt"}),
    }


def test_distinct_globs_reuse_anchor_scan_but_charge_each_pattern(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\ntest_roots = ["test_subject.py"]\n'
        '[tool.pytest_telescope.defaults]\ninputs = ["shared/*.txt", "shared/*.json"]\n'
    )
    declaration_source = "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\n"
    (tmp_path / "test_subject.py").write_text(declaration_source)
    shared = tmp_path / "shared"
    shared.mkdir()
    for number in range(20):
        for suffix in ("txt", "json"):
            (shared / f"data-{number}.{suffix}").write_text("{}")
    scanned = []
    original = os.scandir

    def scandir(path):
        scanned.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    inputs = load(tmp_path).units[0].inputs
    assert len([name for name in inputs if name.startswith("shared/")]) == 40
    assert scanned.count(shared) == 1
    config = tmp_path / "pyproject.toml"
    config.write_text(
        config.read_text().replace(
            "[tool.pytest_telescope]\n", "[tool.pytest_telescope]\nmax_visits = 65\n"
        )
    )
    with pytest.raises(DeclarationError, match="exceeds 65 entries"):
        load(tmp_path)


@pytest.mark.parametrize("test_roots", [["."], ["test_subject.py"]])
def test_max_visits_can_tighten_directory_discovery(tmp_path, test_roots):
    declaration(tmp_path, ["test_subject.py"])
    for number in range(40):
        (tmp_path / f"data-{number}.txt").write_text("x")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest_telescope]\nmax_visits = 25\n"
        f"test_roots = {json.dumps(test_roots)}\n"
        '[tool.pytest_telescope.defaults]\ninputs = ["*.txt"]\n'
    )
    with pytest.raises(DeclarationError, match="exceeds 25 entries"):
        load(tmp_path)


def test_max_visits_can_raise_repeated_glob_discovery_limit(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\nmax_visits = 300000\n")
    for number in range(100):
        (tmp_path / f"data-{number}.txt").write_text("x")
    patterns = [f"missing-{number}*" for number in range(2000)]
    (tmp_path / "test_subject.py").write_text(
        "import pytest\npytestmark = pytest.mark.telescope(inputs=" + json.dumps(patterns) + ")\n"
        "def test_subject(): pass\n"
    )
    assert load(tmp_path).declarations.popitem()[1].inputs == []


@pytest.mark.parametrize("value", ["true", "0", "-1", "100_000_001", '"200000"', "1.5"])
def test_max_visits_rejects_invalid_values(tmp_path, value):
    (tmp_path / "pyproject.toml").write_text(f"[tool.pytest_telescope]\nmax_visits = {value}\n")
    with pytest.raises(DeclarationError, match="max_visits must be an integer from 1 to 100000000"):
        load(tmp_path)


@pytest.mark.parametrize("value", [1, 100_000_000])
def test_max_visits_accepts_inclusive_bounds(tmp_path, value):
    declaration(tmp_path, ["test_subject.py"])
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.pytest_telescope]\nmax_visits = {value}\ntest_roots = ["test_subject.py"]\n'
    )
    assert load(tmp_path).inventory == ["test_subject.py"]


@pytest.mark.parametrize("kind", ["file", "symlink", "missing"])
def test_literal_glob_prefix_requires_real_directories(tmp_path, kind):
    target = tmp_path / "target"
    target.mkdir()
    (target / "mod.py").write_text("VALUE = 1\n")
    if kind == "file":
        (tmp_path / "prefix").write_text("x")
    elif kind == "symlink":
        (tmp_path / "prefix").symlink_to(target, target_is_directory=True)
    assert expand(tmp_path, ["prefix/*.py"], "inputs") == []


@pytest.mark.parametrize("marker", ["pyvenv.cfg", "conda-meta"])
def test_literal_glob_prefix_refuses_environment_namespaces(tmp_path, marker):
    environment = tmp_path / "env"
    environment.mkdir()
    if marker == "pyvenv.cfg":
        (environment / marker).write_text("home = /usr/bin\n")
    else:
        (environment / marker).mkdir()
    (environment / "mod.py").write_text("VALUE = 1\n")
    with pytest.raises(DeclarationError, match="metadata, cache and host environments"):
        expand(tmp_path, ["env/*.py"], "inputs")
