"""Installed-package host-runtime controls, invoked separately from functional tests."""

import base64
import csv
import hashlib
import io
import json
from pathlib import Path

import pytest
from frozen_tool_fixtures import ToolProject, assert_counts, cache_bytes, put
from frozen_tool_runtime_fixtures import installed_plugin, owned_command
from frozen_tool_runtime_manifest import RuntimeManifest


def update_record(purelib, *, bytecode=None):
    record = purelib / "owned_plugin-1.0.0.dist-info/RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text())))
    if bytecode is not None:
        name = bytecode.relative_to(purelib).as_posix()
        if not any(row[0] == name for row in rows):
            rows.append([name, "", ""])
    for row in rows:
        path = purelib / row[0]
        if path == record or path.suffix == ".pyc":
            row[1:] = ["", ""]
        else:
            content = path.read_bytes()
            hashed = (
                base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            )
            row[1:] = ["sha256=" + hashed, str(len(content))]
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record.write_text(output.getvalue())


def register(project, interpreter, purelib):
    runtime = RuntimeManifest(project, "plugin", interpreter=interpreter)
    package = runtime.add_tree("package", purelib / "owned_plugin")
    metadata = runtime.add_tree("metadata", purelib / "owned_plugin-1.0.0.dist-info")
    runtime.finish(
        python=[
            {
                "name": "owned-plugin",
                "version": "1.0.0",
                "metadata": runtime.reference(metadata),
                "packages": [runtime.reference(package)],
                "dependencies": [],
                "runtime": [
                    runtime.reference(interpreter),
                    runtime.reference(runtime.stdlib),
                    {"root": "system", "path": "."},
                    {"root": "invoking_environment", "path": "."},
                ],
            }
        ]
    )
    return runtime


def invoke(project, interpreter, label):
    output = owned_command(
        [
            str(interpreter),
            "-I",
            "-m",
            "pytest_telescope",
            "run",
            "--timeout",
            "120",
            "--explain",
        ],
        project.root,
        label,
        timeout=240,
        accepted=(0, 1, 3, 4),
    )
    return json.loads(output)


@pytest.fixture
def installed(tmp_path):
    interpreter, purelib = installed_plugin(tmp_path / "plugin")
    project = ToolProject(tmp_path / "project", tmp_path / "installed")
    register(project, interpreter, purelib)
    return project, interpreter, purelib


def test_real_installed_package_resources_metadata_and_entrypoint_in_isolated_child(installed):
    project, interpreter, purelib = installed
    package = purelib / "owned_plugin"
    live_inode = (package / "__init__.py").stat().st_ino
    project.module(
        "test_installed.py",
        f"""import json
import subprocess
import sys
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['plugin'])
def test_child():
    code = '''import csv, io, json, os, stat
from importlib import metadata, resources
import owned_plugin
distribution = metadata.distribution('owned-plugin')
entry = next(item for item in distribution.entry_points if item.group == 'owned.tools' and item.name == 'value')
record = list(csv.reader(io.StringIO(distribution.read_text('RECORD'))))
self_row = next(row for row in record if row[0] == 'owned_plugin-1.0.0.dist-info/RECORD')
info = os.stat(owned_plugin.__file__)
print(json.dumps(dict(value=owned_plugin.VALUE, resource=resources.files('owned_plugin').joinpath('message.txt').read_text(), version=distribution.version, entry=entry.load(), file=owned_plugin.__file__, inode=info.st_ino, mode=stat.S_IMODE(info.st_mode), record_self=self_row[1:])))'''
    child = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert result['value'] == result['entry'] == 7
    assert result['resource'] == 'ready\\n'
    assert result['version'] == '1.0.0'
    assert result['file'] == {str(package / "__init__.py")!r}
    assert result['inode'] != {live_inode}
    assert result['mode'] == 0o444
    assert result['record_self'] == ['', '']
    assert Path('/tools/plugin/package/message.txt').read_text() == result['resource']
""",
    )
    assert_counts(invoke(project, interpreter, "first"), executed=1)
    assert_counts(invoke(project, interpreter, "warm"), reused=1)
    assert (package / "__init__.py").stat().st_ino == live_inode


@pytest.mark.parametrize("changed", ["module", "resource", "entrypoint"])
def test_same_version_package_byte_change_refuses_stale_pins_and_invalidates_valid_consumer(
    installed, changed
):
    project, interpreter, purelib = installed
    project.module(
        "test_value.py",
        """import subprocess
import sys
import pytest
@pytest.mark.telescope(tools=['plugin'])
def test_plugin():
    code = "from importlib import metadata, resources; import owned_plugin; assert owned_plugin.VALUE >= 7; assert metadata.version('owned-plugin') == '1.0.0'; assert resources.files('owned_plugin').joinpath('message.txt').read_text()"
    child = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
@pytest.mark.telescope(tools=[])
def test_unrelated(): pass
""",
    )
    assert_counts(invoke(project, interpreter, "first"), executed=2)
    previous = cache_bytes(project.root)
    if changed == "module":
        put(purelib, "owned_plugin/__init__.py", "VALUE = 8\n")
    elif changed == "resource":
        put(purelib, "owned_plugin/message.txt", "updated resource\n")
    else:
        put(
            purelib,
            "owned_plugin-1.0.0.dist-info/entry_points.txt",
            "[owned.tools]\nvalue = owned_plugin:VALUE\nsecond = owned_plugin:VALUE\n",
        )
    refused = invoke(project, interpreter, "stale")
    assert refused["status"] == "unsupported", refused
    assert refused["capability"] == "tool:identity", refused
    assert cache_bytes(project.root) == previous
    update_record(purelib)
    register(project, interpreter, purelib)
    changed_report = invoke(project, interpreter, "updated")
    assert_counts(changed_report, executed=1, reused=1)


def test_real_generated_bytecode_is_bound_alongside_source_and_runtime(installed):
    project, interpreter, purelib = installed
    source = purelib / "owned_plugin/__init__.py"
    bytecode = Path(
        owned_command(
            [
                str(interpreter),
                "-I",
                "-c",
                "import py_compile; print(py_compile.compile("
                + repr(str(source))
                + ", doraise=True))",
            ],
            project.external,
            "compile-owned-bytecode",
            timeout=30,
        )
        .decode()
        .strip()
    )
    update_record(purelib, bytecode=bytecode)
    register(project, interpreter, purelib)
    expected = hashlib.sha256(bytecode.read_bytes()).hexdigest()
    project.module(
        "test_bytecode.py",
        f"""import subprocess
import sys
import pytest
pytestmark = pytest.mark.telescope(tools=['plugin'])
def test_bytecode():
    code = "import hashlib; from pathlib import Path; import owned_plugin; assert owned_plugin.VALUE == 7; assert hashlib.sha256(Path(owned_plugin.__cached__).read_bytes()).hexdigest() == {expected!r}"
    child = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
""",
    )
    assert_counts(invoke(project, interpreter, "first"), executed=1)
    previous = cache_bytes(project.root)
    bytecode.write_bytes(bytecode.read_bytes() + b"changed")
    report = invoke(project, interpreter, "stale-bytecode")
    assert report["status"] == "unsupported" and report["capability"] == "tool:identity", report
    assert cache_bytes(project.root) == previous


def test_record_source_gap_refuses_before_import_even_when_tree_pin_matches(installed):
    project, interpreter, purelib = installed
    (purelib / "owned_plugin/message.txt").unlink()
    register(project, interpreter, purelib)
    project.module(
        "test_gap.py",
        """import pytest
pytestmark = pytest.mark.telescope(tools=['plugin'])
raise RuntimeError('project import must not run for incomplete installed metadata')
def test_gap(): pass
""",
    )
    report = invoke(project, interpreter, "missing-resource")
    assert report["status"] in {"unsupported", "declaration_error"}, report
    assert not cache_bytes(project.root)
