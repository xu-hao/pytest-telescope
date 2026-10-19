"""Filesystem and collection contracts exercised without importing test code."""

import os

import pytest

from pytest_telescope.model import DeclarationError, load, regular_bytes


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
    import json

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
