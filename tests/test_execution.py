"""Required real Docker integration tests; never skip missing enforcement."""

import concurrent.futures
import json

import pytest

from pytest_telescope import controller, runtime
from pytest_telescope.model import DeclarationError, load, snapshot


@pytest.fixture
def project(tmp_path):
    metadata = runtime.build_runtime(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        "[tool.pytest_telescope.bundles.unit]\n"
        'inputs = ["data.json", "helper.py"]\n'
    )
    (tmp_path / "data.json").write_text('{"value": 7}')
    (tmp_path / "helper.py").write_text("VALUE = 7\n")
    (tmp_path / "test_unit.py").write_text(
        'import pytest\npytestmark = pytest.mark.telescope(bundles=["unit"])\n'
        "import json\nfrom pathlib import Path\nfrom helper import VALUE\n"
        'def test_unit():\n    assert json.loads(Path("data.json").read_text())["value"] == VALUE\n'
    )
    yield tmp_path
    runtime.command(["docker", "image", "rm", metadata["image"]])


def run(project, **options):
    result = controller.run(project, explain=True, **options)
    assert result["counts"]["infrastructure_error"] == 0, result
    return result


def test_real_cold_warm_force_and_no_cache(project):
    cold = run(project)
    assert cold["counts"]["executed"] == 1 and cold["cases"]["passed"] == 1
    assert run(project)["counts"]["reused"] == 1
    assert run(project, force=True)["counts"]["executed"] == 1
    assert run(project, no_cache=True)["counts"]["executed"] == 1


def test_boundaries_cover_collection_native_subprocess_fixtures_and_teardown(project):
    (project / "undeclared.txt").write_text("synthetic private project input")
    (project / "conftest.py").write_text(
        "import ctypes, os, socket, subprocess, sys\nfrom pathlib import Path\nimport pytest\n"
        "def verify():\n"
        '    assert not Path("undeclared.txt").exists()\n'
        '    assert ctypes.CDLL(None).open(b"/project/undeclared.txt", 0) == -1\n'
        "    assert os.getuid() != 0\n"
        '    with pytest.raises(OSError): Path("data.json").write_text("blocked")\n'
        '    with pytest.raises(OSError): Path("/etc/blocked").write_text("blocked")\n'
        "    child = subprocess.run([sys.executable, '-I', '-c', 'from pathlib import Path; assert not Path(\\\"undeclared.txt\\\").exists()'], timeout=5)\n"
        "    assert child.returncode == 0\n"
        "    connection = socket.socket(); connection.settimeout(1)\n"
        '    with pytest.raises(OSError): connection.connect(("1.1.1.1", 443))\n'
        "    connection.close()\n"
        "verify()\n"
        "@pytest.fixture(autouse=True)\n"
        "def boundary():\n    verify()\n    yield\n    verify()\n"
    )
    manifest = project / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace('"helper.py"]', '"helper.py", "conftest.py"]'))
    result = run(project)
    assert result["status"] == "passed" and result["cases"]["passed"] == 1
    assert run(project)["counts"]["reused"] == 1


def test_content_and_metadata_invalidation(project):
    assert run(project)["counts"]["executed"] == 1
    (project / "unrelated.txt").write_text("unrelated")
    assert run(project)["counts"]["reused"] == 1
    (project / "helper.py").write_text("VALUE = 7  # changed code\n")
    assert run(project)["counts"]["executed"] == 1
    (project / "data.json").write_text('{"value": 7, "other": true}')
    assert run(project)["counts"]["executed"] == 1
    (project / "helper.py").chmod(0o755)
    assert run(project)["counts"]["executed"] == 1


def test_new_tests_and_undeclared_conftest_never_disappear(project):
    run(project)
    (project / "test_new.py").write_text("def test_new(): assert False\n")
    added = controller.run(project)
    assert added["collection"]["collected"] == 2
    assert added["counts"]["failed"] == 1 and added["counts"]["reused"] == 1
    (project / "test_new.py").unlink()
    (project / "conftest.py").write_text("raise AssertionError('must not import on host')\n")
    with pytest.raises(DeclarationError):
        controller.run(project)


def test_failed_and_corrupt_records_execute_again(project):
    run(project)
    record = next((project / ".pytest-telescope/cache").glob("*.json"))
    record.write_text('{"incomplete":')
    assert run(project)["counts"]["executed"] == 1
    (project / "test_unit.py").write_text("def test_unit(): assert False\n")
    first, second = run(project), run(project)
    assert first["counts"]["failed"] == second["counts"]["failed"] == 1
    assert second["counts"]["reused"] == 0


def test_concurrent_invocations_accept_only_complete_success(project):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: controller.run(project), range(2)))
    assert sorted(item["counts"]["executed"] for item in results) == [0, 1]
    assert all(item["cases"]["passed"] == 1 for item in results)


def test_explicit_environment_and_selectors_invalidate(project, monkeypatch):
    manifest = project / "pyproject.toml"
    manifest.write_text('[tool.pytest_telescope]\nenv = ["VARIANT"]\n' + manifest.read_text())
    monkeypatch.setenv("VARIANT", "one")
    run(project)
    assert run(project)["counts"]["reused"] == 1
    monkeypatch.setenv("VARIANT", "two")
    assert run(project)["counts"]["executed"] == 1
    assert run(project, arguments=["-k", "unit"])["counts"]["executed"] == 1
    assert run(project, arguments=["-k", "unit"])["counts"]["reused"] == 1
    assert run(project, arguments=["test_unit.py::test_unit"])["cases"]["passed"] == 1


def test_glob_membership_absence_and_symlinks(project):
    manifest = project / "pyproject.toml"
    manifest.write_text(
        manifest.read_text().replace('"data.json"', '"*.json"') + 'absent = ["optional.txt"]\n'
    )
    run(project)
    (project / "extra.json").write_text("{}")
    assert run(project)["counts"]["executed"] == 1
    (project / "extra.json").unlink()
    assert run(project)["counts"]["reused"] == 1
    (project / "optional.txt").touch()
    with pytest.raises(DeclarationError):
        controller.run(project)
    (project / "optional.txt").unlink()
    (project / "alias.py").symlink_to("helper.py")
    manifest.write_text(manifest.read_text().replace('"helper.py"]', '"helper.py", "alias.py"]'))
    assert run(project)["counts"]["executed"] == 1
    (project / "alias.py").unlink()
    (project / "alias.py").symlink_to("data.json")
    assert run(project)["counts"]["executed"] == 1


def test_runtime_identity_and_noncacheable_target(project):
    run(project)
    metadata = project / ".pytest-telescope/runtime.json"
    content = json.loads(metadata.read_text())
    content["worker"] = "0" * 64
    metadata.write_text(json.dumps(content))
    report = controller.run(project)
    assert report["status"] == "infrastructure_error"
    assert report["collection"]["outcome"] == "infrastructure_error"
    assert report["units"] == []


def test_noncacheable_target_always_runs(project):
    manifest = project / "pyproject.toml"
    manifest.write_text(
        manifest.read_text() + "\n[tool.pytest_telescope.defaults]\ncache = false\n"
    )
    assert run(project)["counts"]["executed"] == 1
    assert run(project)["counts"]["executed"] == 1
    assert not list((project / ".pytest-telescope/cache").glob("*.json"))


def test_snapshot_metadata_is_normalized_and_source_mutation_does_not_change_frozen(
    project, tmp_path_factory
):
    manifest = load(project)
    destination = tmp_path_factory.mktemp("snapshot") / "project"
    snapshot(manifest, manifest.units[0], destination)
    frozen = (destination / "helper.py").read_bytes()
    (project / "helper.py").write_text("VALUE = 99\n")
    assert (destination / "helper.py").read_bytes() == frozen
    assert (destination / "helper.py").stat().st_mtime_ns == 0
    assert (destination / "helper.py").stat().st_mode & 0o222 == 0


def test_cache_storage_is_bounded(project):
    run(project)
    cache = project / ".pytest-telescope/cache"
    for number in range(30):
        (cache / f"{number:064x}.json").write_text("{}")
    controller.CacheStore(cache, count=16, size=1024)
    assert len(list(cache.glob("*.json"))) <= 16
    assert sum(path.stat().st_size for path in cache.glob("*.json")) <= 1024


def test_runner_identity_is_part_of_reuse_key(project, monkeypatch):
    run(project)
    assert run(project)["counts"]["reused"] == 1
    original = controller.runner_identity()
    monkeypatch.setattr(controller, "runner_identity", lambda: original | {"version": "changed"})
    assert run(project)["counts"]["executed"] == 1


def test_keyword_selection_accounts_nonmatching_targets(project):
    (project / "test_other.py").write_text("def test_other(): assert True\n")
    result = run(project, arguments=["-k", "unit"])
    assert result["status"] == "passed"
    assert result["counts"]["executed"] == 1
    assert result["counts"]["not-selected"] == 1
    assert result["cases"]["collected"] == 1
    assert run(project, arguments=["-k", "nothing_matches"])["status"] == "no_tests"


def test_session_fixture_is_fresh_for_every_case(project):
    (project / "conftest.py").write_text(
        'import pytest\nstate = []\n@pytest.fixture(scope="session")\ndef shared():\n    return state\n'
    )
    (project / "test_first.py").write_text(
        'def test_first(shared):\n    assert shared == []\n    shared.append("first")\n'
    )
    (project / "test_second.py").write_text("def test_second(shared):\n    assert shared == []\n")
    (project / "test_unit.py").unlink()
    (project / "pyproject.toml").write_text(
        '[tool.pytest_telescope.defaults]\ninputs = ["conftest.py"]\n'
    )
    result = run(project)
    assert result["status"] == "passed" and result["cases"]["passed"] == 2
    assert run(project)["counts"]["reused"] == 2


@pytest.mark.parametrize("corruption", ["zero-cases", "wrong-case"])
def test_single_case_cache_cannot_reuse_empty_or_unrelated_success(project, corruption):
    from pytest_telescope.model import fingerprint

    assert run(project)["counts"]["executed"] == 1
    path = next((project / ".pytest-telescope/cache").glob("*.json"))
    stored = json.loads(path.read_text())
    assert controller.read_record(path, stored["key"]) == stored["result"]
    if corruption == "zero-cases":
        stored["result"].update(collected=0, passed=0, failed=0, skipped=0, case_ids=[])
    else:
        stored["result"]["case_ids"] = ["f" * 64]
    stored["checksum"] = fingerprint(stored["result"])
    path.write_text(json.dumps(stored))
    rerun = run(project)
    assert rerun["counts"]["executed"] == 1
    assert rerun["counts"]["reused"] == 0
    assert rerun["cases"]["passed"] == 1
