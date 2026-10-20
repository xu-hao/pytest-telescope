"""Declared lookup paths for isolated children without granting source files."""

import json
import re

import pytest

from pytest_telescope import controller, runtime
from pytest_telescope.model import DeclarationError, UnsupportedCapability
from pytest_telescope.plan import make_plan


def put(root, path, text):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


def configure(root, python=None):
    text = '[tool.pytest_telescope]\ntest_roots=["tests"]\n'
    if python is not None:
        text += "[tool.pytest_telescope.python]\n"
        text += "\n".join(f"{key}={json.dumps(value)}" for key, value in python.items()) + "\n"
    put(root, "pyproject.toml", text)
    put(root, "tests/test_example.py", "def test_example(): pass\n")
    for name in ("first", "second"):
        (root / name).mkdir(exist_ok=True)


def forbid_backend(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("static Python profile validation reached backend or cache")

    for name in ("command", "identity", "execute", "recover"):
        monkeypatch.setattr(runtime, name, forbidden)
    monkeypatch.setattr(controller, "CacheStore", forbidden)


def static_row(suite, profile="default", roots=()):
    return {"suite": suite, "profile": profile, "source_roots": list(roots), "identity": None}


def test_static_defaults_order_and_invocation_replacement(tmp_path, monkeypatch):
    configure(tmp_path)
    forbid_backend(monkeypatch)
    assert make_plan(tmp_path)["python"] == {"suites": [static_row("default")]}
    configure(tmp_path, {"profile": "isolated-source-v1", "source_roots": ["second", "first"]})
    original = (tmp_path / "pyproject.toml").read_bytes()
    assert make_plan(tmp_path)["python"] == {
        "suites": [static_row("default", "isolated-source-v1", ["second", "first"])]
    }
    assert make_plan(tmp_path, source_roots=[])["python"] == {
        "suites": [static_row("default", "isolated-source-v1")]
    }
    assert make_plan(tmp_path, python_profile="default", source_roots=[])["python"] == {
        "suites": [static_row("default")]
    }
    assert (tmp_path / "pyproject.toml").read_bytes() == original


def suites(root):
    put(
        root,
        "pyproject.toml",
        """
[tool.pytest_telescope.python]
profile="isolated-source-v1"
source_roots=["first"]
[tool.pytest_telescope.suites.core]
root="tests/core"
config="pyproject.toml"
test_roots=["tests/core"]
[tool.pytest_telescope.suites.addon]
root="tests/addon"
config="pyproject.toml"
test_roots=["tests/addon"]
[tool.pytest_telescope.suites.addon.python]
profile="isolated-source-v1"
source_roots=["second"]
""",
    )
    for suite in ("core", "addon"):
        put(root, f"tests/{suite}/test_example.py", "def test_example(): pass\n")
    for path in ("first", "second"):
        (root / path).mkdir(exist_ok=True)


def test_suite_inheritance_is_replaced_without_root_union(tmp_path, monkeypatch):
    suites(tmp_path)
    forbid_backend(monkeypatch)
    result = make_plan(tmp_path)
    order = [row["name"] for row in result["suites"]]
    expected = {"core": ["first"], "addon": ["second"]}
    assert result["python"]["suites"] == [
        static_row(name, "isolated-source-v1", expected[name]) for name in order
    ]
    result = make_plan(tmp_path, suites=["addon"], source_roots=["first", "second"])
    assert result["python"]["suites"] == [
        static_row("addon", "isolated-source-v1", ["first", "second"])
    ]
    with pytest.raises(DeclarationError):
        make_plan(tmp_path, python_profile="default")


@pytest.mark.parametrize(
    "table",
    [
        {"profile": "default", "source_roots": ["first"]},
        {"profile": "isolated-source-v1", "source_roots": "first"},
        {"profile": "isolated-source-v1", "source_roots": ["first", "first"]},
        {"profile": "isolated-source-v1", "source_roots": [1]},
        {"profile": "isolated-source-v1", "source_roots": [], "unknown": True},
    ],
)
def test_malformed_project_profile_refuses_before_backend(tmp_path, monkeypatch, table):
    configure(tmp_path, table)
    forbid_backend(monkeypatch)
    for invoke in (make_plan, controller.run):
        with pytest.raises(DeclarationError):
            invoke(tmp_path)


@pytest.mark.parametrize("field", ["profile", "source_roots"])
def test_partial_suite_python_table_is_not_an_inheritance_merge(tmp_path, monkeypatch, field):
    suites(tmp_path)
    configuration = tmp_path / "pyproject.toml"
    value = 'profile="isolated-source-v1"' if field == "profile" else 'source_roots=["second"]'
    configuration.write_text(
        configuration.read_text().split("[tool.pytest_telescope.suites.addon.python]")[0]
        + "[tool.pytest_telescope.suites.addon.python]\n"
        + value
        + "\n"
    )
    forbid_backend(monkeypatch)
    with pytest.raises(DeclarationError):
        make_plan(tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/tmp",
        "./first",
        "first/../second",
        "first//nested",
        "first\\nested",
        "first\nsecond",
        "missing",
        "pyproject.toml",
    ],
)
def test_noncanonical_or_non_directory_source_roots_refuse(tmp_path, monkeypatch, path):
    configure(tmp_path)
    forbid_backend(monkeypatch)
    for invoke in (make_plan, controller.run):
        with pytest.raises(DeclarationError):
            invoke(tmp_path, python_profile="isolated-source-v1", source_roots=[path])


@pytest.mark.parametrize("ancestor", [False, True])
def test_source_root_links_refuse(tmp_path, monkeypatch, ancestor):
    configure(tmp_path)
    (tmp_path / "first/nested").mkdir()
    (tmp_path / "alias").symlink_to("first", target_is_directory=True)
    forbid_backend(monkeypatch)
    with pytest.raises(DeclarationError):
        make_plan(
            tmp_path,
            python_profile="isolated-source-v1",
            source_roots=["alias/nested" if ancestor else "alias"],
        )


def test_source_root_bound_and_dot_are_static(tmp_path, monkeypatch):
    configure(tmp_path)
    roots = [f"root_{number:02d}" for number in range(65)]
    for root in roots:
        (tmp_path / root).mkdir()
    forbid_backend(monkeypatch)
    assert make_plan(tmp_path, python_profile="isolated-source-v1", source_roots=["."])["python"][
        "suites"
    ][0]["source_roots"] == ["."]
    assert (
        make_plan(tmp_path, python_profile="isolated-source-v1", source_roots=roots[:64])["python"][
            "suites"
        ][0]["source_roots"]
        == roots[:64]
    )
    with pytest.raises(DeclarationError):
        controller.run(tmp_path, python_profile="isolated-source-v1", source_roots=roots)


@pytest.mark.parametrize("command", ["plan", "run"])
def test_unknown_python_profile_cli_refusal(tmp_path, monkeypatch, capsys, command):
    configure(tmp_path)
    monkeypatch.chdir(tmp_path)
    forbid_backend(monkeypatch)
    assert controller.main([command, "--python-profile", "isolated-source-v2"]) == 4
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == 2 and result["kind"] == "refusal"
    assert result["capability"] == "python:profile"


def test_cli_source_root_order_and_separate_runtime_envelope(tmp_path, monkeypatch, capsys):
    configure(tmp_path)
    monkeypatch.chdir(tmp_path)
    forbid_backend(monkeypatch)
    assert (
        controller.main(
            [
                "plan",
                "--python-profile",
                "isolated-source-v1",
                "--source-root",
                "second",
                "--source-root",
                "first",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["python"]["suites"] == [
        static_row("default", "isolated-source-v1", ["second", "first"])
    ]
    assert result["runtime"] == {
        "profile": "python-v1",
        "required_capabilities": [],
        "identity": None,
    }
    assert result["execution"]["profile"] == "case-v1"
    with pytest.raises(DeclarationError):
        make_plan(tmp_path, source_roots=["first"])


@pytest.fixture
def prepared(tmp_path):
    images = set()

    def prepare(profile="python-v1"):
        image = runtime.build_runtime(tmp_path, runtime_profile=profile)["image"]
        images.add(image)

    yield tmp_path, prepare
    for image in images:
        runtime._image_info(image, runtime._project(tmp_path))
        runtime.command(["docker", "image", "rm", image])


def passed(root, **kwargs):
    result = controller.run(root, **kwargs)
    assert result["status"] == "passed", result
    assert result["schema"] == 2 and result["inventory_complete"] and result["grouping_complete"]
    assert set(result["runtime"]) == {"profile", "required_capabilities", "identity"}
    assert [row["suite"] for row in result["python"]["suites"]] == result["suites"]
    assert all(re.fullmatch("[0-9a-f]{64}", row["identity"]) for row in result["python"]["suites"])
    return result


CHILD_CASES = r"""
import json, os, pathlib, shutil, subprocess, sys
import pytest

def child(code, *, flags=("-I",), env=None):
    result = subprocess.run([sys.executable, *flags, "-c", code], capture_output=True, text=True, timeout=10, env=env)
    assert result.returncode == 0
    return json.loads(result.stdout)

@pytest.mark.telescope(inputs=["src/example_core" , "src/inert.pth"], distributions=["core"])
@pytest.mark.parametrize("variant", [0, 1])
def test_complete(tmp_path, variant):
    assert child("import example_core,json; print(json.dumps(example_core.value))") in ("initial", "changed")
    assert child("import importlib.metadata as m,json; print(json.dumps(m.version('example-core')))") in ("1.0", "2.0")
    assert child("import importlib.metadata as m,json; print(json.dumps(next(iter(m.entry_points(group='example.plugins',name='core'))).load()))") in ("initial", "changed")
    executed = subprocess.run([sys.executable, "-I", "-m", "example_core"], capture_output=True, text=True, timeout=10)
    assert executed.returncode == 0 and executed.stdout.strip() in ("initial", "changed")
    assert child("import importlib.util,json; print(json.dumps(importlib.util.find_spec('example_core') is None))", flags=("-I", "-S"))
    assert child("import importlib.util,json; print(json.dumps(importlib.util.find_spec('example_hidden') is None))")
    assert child("import importlib.metadata as m,json; print(json.dumps(not any(d.metadata['Name']=='example-hidden' for d in m.distributions())))")
    with pytest.raises(OSError):
        pathlib.Path('/project/src/example_core/__init__.py').write_text('forbidden')
    assert not pathlib.Path('/project/private.txt').exists()
    assert not pathlib.Path('/tmp/source-pth-ran').exists()
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / 'hostile_path.py').write_text('value=1')
    usersite = tmp_path / 'user/lib' / ('python' + '.'.join(map(str, sys.version_info[:2]))) / 'site-packages'
    usersite.mkdir(parents=True)
    (usersite / 'hostile_user.py').write_text('value=1')
    environment = dict(os.environ, PYTHONPATH=str(hostile), PYTHONUSERBASE=str(tmp_path / 'user'))
    assert child("import importlib.util,json,sys; print(json.dumps(sys.flags.isolated == 1 and importlib.util.find_spec('hostile_path') is None and importlib.util.find_spec('hostile_user') is None))", env=environment)
    executable = tmp_path / 'copied-python'
    shutil.copyfile(sys.executable, executable)
    executable.chmod(0o755)
    with pytest.raises(PermissionError):
        subprocess.run([str(executable), '-V'], capture_output=True, timeout=10)

@pytest.mark.telescope(inputs=["src/example_core"], distributions=[])
def test_source_only():
    assert child("import example_core,json; print(json.dumps(example_core.value))") in ("initial", "changed")
    assert child("import importlib.metadata as m,json; print(json.dumps(not any(d.metadata['Name']=='example-core' for d in m.distributions())))")

@pytest.mark.telescope(inputs=[], distributions=["core"])
def test_metadata_only():
    assert child("import importlib.metadata as m,json; print(json.dumps(m.version('example-core')))") in ("1.0", "2.0")
    assert child("import importlib.util,json; print(json.dumps(importlib.util.find_spec('example_core') is None))")
    failed = subprocess.run([sys.executable, '-I', '-c', "import importlib.metadata as m; next(iter(m.entry_points(group='example.plugins',name='core'))).load()"], capture_output=True, timeout=10)
    assert failed.returncode != 0

@pytest.mark.telescope(inputs=[], distributions=[])
def test_omitted():
    assert child("import importlib.util,json; print(json.dumps(importlib.util.find_spec('example_core') is None))")
    assert child("import importlib.metadata as m,json; print(json.dumps(not any(d.metadata['Name']=='example-core' for d in m.distributions())))")
"""


def source_project(root):
    configure(root, {"profile": "isolated-source-v1", "source_roots": ["src"]})
    config = root / "pyproject.toml"
    config.write_text(
        config.read_text()
        + """
[tool.pytest_telescope.distributions.core]
metadata="metadata/core/pyproject.toml"
format="pep621-static-v1"
[tool.pytest_telescope.distributions.hidden]
metadata="metadata/hidden/pyproject.toml"
format="pep621-static-v1"
"""
    )
    put(
        root,
        "metadata/core/pyproject.toml",
        '[project]\nname="example-core"\nversion="1.0"\n[project.entry-points."example.plugins"]\ncore="example_core:value"\n',
    )
    put(root, "metadata/hidden/pyproject.toml", '[project]\nname="example-hidden"\nversion="1.0"\n')
    put(root, "src/example_core/__init__.py", 'value="initial"\n')
    put(root, "src/example_core/__main__.py", "from . import value\nprint(value)\n")
    put(root, "src/example_hidden.py", 'value="not declared"\n')
    put(root, "src/inert.pth", "import pathlib; pathlib.Path('/tmp/source-pth-ran').touch()\n")
    put(root, "private.txt", "synthetic undeclared input\n")
    put(root, "tests/test_example.py", CHILD_CASES)


@pytest.mark.parametrize("runtime_profile", ["python-v1", "git-local-v1"])
@pytest.mark.parametrize("mode", ["case", "batch"])
def test_isolated_children_receive_exact_source_metadata_views_and_reuse(
    prepared, monkeypatch, runtime_profile, mode
):
    root, prepare = prepared
    source_project(root)
    prepare(runtime_profile)
    options = {"runtime_profile": runtime_profile, "execution_mode": mode}
    cold = passed(root, **options)
    assert cold["counts"]["executed"] == 5
    assert len(cold["groups"]) == (5 if mode == "case" else 4)
    assert cold["runtime"]["profile"] == runtime_profile
    with monkeypatch.context() as warm_guard:

        def no_worker(*args, **kwargs):
            pytest.fail("warm evidence launched a worker or prepared an image")

        warm_guard.setattr(runtime, "execute", no_worker)
        warm_guard.setattr(runtime, "build_runtime", no_worker)
        warm = passed(root, **options)
    assert warm["counts"]["reused"] == 5 and warm["collection"]["outcome"] == "reused"
    assert warm["python"] == cold["python"]
    put(root, "src/example_core/__init__.py", 'value="changed"\n')
    changed = passed(root, **options)
    assert changed["counts"]["executed"] == 3 and changed["counts"]["reused"] == 2
    metadata = root / "metadata/core/pyproject.toml"
    metadata.write_text(metadata.read_text().replace('version="1.0"', 'version="2.0"'))
    changed_metadata = passed(root, **options)
    assert changed_metadata["counts"]["executed"] == 3 and changed_metadata["counts"]["reused"] == 2
    uncached = passed(root, no_cache=True, **options)
    assert uncached["counts"]["executed"] == 5
    assert all(not group["cache_eligible"] for group in uncached["groups"])


def test_independent_suite_roots_and_namespaces_do_not_union(prepared):
    root, prepare = prepared
    suites(root)
    for directory, value in (("first", "core"), ("second", "addon")):
        put(root, f"{directory}/same_name.py", f"value={value!r}\n")
        put(root, f"{directory}/shared_namespace/{value}.py", f"value={value!r}\n")
    for suite, directory, other in (("core", "first", "addon"), ("addon", "second", "core")):
        put(
            root,
            f"tests/{suite}/test_example.py",
            f'''
import json, subprocess, sys
import pytest
pytestmark=pytest.mark.telescope(inputs=["{directory}"])
def test_child():
    code="import same_name, shared_namespace.{suite}, importlib.util, json; print(json.dumps([same_name.value, shared_namespace.{suite}.value, importlib.util.find_spec('shared_namespace.{other}') is None]))"
    result=subprocess.run([sys.executable,"-I","-c",code],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == ["{suite}", "{suite}", True]
''',
        )
    prepare()
    result = passed(root)
    assert result["counts"]["executed"] == 2
    settings = {row["suite"]: row for row in result["python"]["suites"]}
    assert settings["core"]["source_roots"] == ["first"]
    assert settings["addon"]["source_roots"] == ["second"]
    assert settings["core"]["identity"] != settings["addon"]["identity"]
    put(root, "first/unrelated.txt", "changed declared root input\n")
    changed = passed(root)
    assert changed["counts"]["executed"] == changed["counts"]["reused"] == 1
    assert next(row for row in changed["units"] if row["suite"] == "addon")["outcome"] == "reused"


def test_ordered_roots_bind_results_and_namespace_portions(prepared):
    root, prepare = prepared
    configure(root, {"profile": "isolated-source-v1", "source_roots": ["first", "second"]})
    put(root, "first/choice.py", 'value="first"\n')
    put(root, "second/choice.py", 'value="second"\n')
    put(root, "first/shared_namespace/one.py", "value=1\n")
    put(root, "second/shared_namespace/two.py", "value=2\n")
    put(
        root,
        "tests/test_example.py",
        """
import json, subprocess, sys
import pytest
pytestmark=pytest.mark.telescope(inputs=["first", "second"])
def test_order():
    code="import choice,shared_namespace.one,shared_namespace.two,json; print(json.dumps([choice.value,shared_namespace.one.value,shared_namespace.two.value]))"
    result=subprocess.run([sys.executable,"-I","-c",code],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == ["first",1,2]
""",
    )
    prepare()
    first = passed(root)
    assert passed(root)["counts"]["reused"] == 1
    reordered = controller.run(root, source_roots=["second", "first"])
    assert reordered["status"] == "failed" and reordered["counts"]["reused"] == 0
    assert reordered["python"]["suites"][0]["identity"] != first["python"]["suites"][0]["identity"]
    assert passed(root)["counts"]["reused"] == 1
    disabled = controller.run(root, python_profile="default", source_roots=[])
    assert disabled["status"] == "failed" and disabled["counts"]["reused"] == 0


def test_empty_roots_project_only_selected_metadata(prepared):
    root, prepare = prepared
    source_project(root)
    prepare()
    result = passed(root, source_roots=[], arguments=["-k", "metadata_only or omitted"])
    assert result["counts"]["executed"] == 2 and result["counts"]["not-selected"] == 3
    assert result["python"]["suites"][0]["source_roots"] == []


def test_all_visible_startup_layouts_refuse_before_any_worker_or_warm_acceptance(
    prepared, monkeypatch
):
    root, prepare = prepared
    suites(root)
    for suite, directory in (("core", "first"), ("addon", "second")):
        put(
            root,
            f"tests/{suite}/test_example.py",
            f'import pytest\npytestmark=pytest.mark.telescope(inputs=["{directory}"])\ndef test_example(): pass\n',
        )
        put(root, f"{directory}/ordinary.py", "value=1\n")
    prepare()
    assert passed(root)["counts"]["executed"] == 2

    def forbidden(*args, **kwargs):
        pytest.fail("a worker started before all proposed source views passed preflight")

    monkeypatch.setattr(runtime, "execute", forbidden)
    for path in (
        "sitecustomize.py",
        "usercustomize.py",
        "sitecustomize/__init__.py",
        "usercustomize/__init__.py",
        "example.dist-info/METADATA",
        "example.egg-info/PKG-INFO",
        "example.egg-link",
    ):
        target = root / "second" / path
        put(root, "second/" + path, "raise RuntimeError('startup must never execute')\n")
        with pytest.raises(UnsupportedCapability) as error:
            controller.run(root)
        assert error.value.capability == "python:source-layout"
        target.unlink()
        if target.parent != root / "second":
            target.parent.rmdir()


def test_invisible_startup_files_do_not_grant_scope_or_block_narrow_views(prepared):
    root, prepare = prepared
    configure(root, {"profile": "isolated-source-v1", "source_roots": ["first"]})
    put(root, "first/sitecustomize.py", "raise RuntimeError('outside declared input scope')\n")
    put(root, "first/safe_module.py", "value=1\n")
    put(
        root,
        "tests/test_example.py",
        """
import subprocess,sys
import pytest
pytestmark=pytest.mark.telescope(inputs=["first/safe_module.py"],cache=False)
def test_source():
    completed=subprocess.run([sys.executable,"-I","-c","import safe_module; assert safe_module.value == 1"],capture_output=True,timeout=10)
    assert completed.returncode == 0
""",
    )
    prepare()
    for _ in range(2):
        result = passed(root)
        assert result["counts"]["executed"] == 1 and result["counts"]["reused"] == 0
        assert result["groups"][0]["cache_eligible"] is False
