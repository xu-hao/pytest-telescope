"""Required real-Bubblewrap controls: what a scoped test can see and change.

These tests launch bwrap. They are never skipped: on a host that denies its namespaces
they fail with the sandbox:namespaces refusal.
"""

import json
import os
import subprocess
import sys
import sysconfig
import uuid
from pathlib import Path

import pytest

from pytest_telescope import controller


def put(root, path, text):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    put(root, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(root, "data/declared.json", '{"visible": true}\n')
    put(root, "data/undeclared.json", '{"visible": false}\n')
    put(root, "notes.txt", "undeclared note\n")
    return root


def passed(root, **kwargs):
    report = controller.run(root, explain=True, **kwargs)
    assert report["status"] == "passed", report
    assert report["schema"] == 3 and report["kind"] == "run"
    assert report["sandbox"]["required"] is True
    assert len(report["sandbox"]["identity"]) == 64
    assert report["inventory_complete"] is True and report["grouping_complete"] is True
    return report


def test_declared_inputs_are_visible_and_undeclared_project_files_are_absent(project):
    put(
        project,
        "tests/test_view.py",
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.telescope(inputs=['data/declared.json'])\n"
        "def test_view():\n"
        "    assert json.loads(Path('data/declared.json').read_text()) == {'visible': True}\n"
        "    assert Path.cwd() == Path('/project')\n"
        "    for name in ('data/undeclared.json', 'notes.txt'):\n"
        "        assert not os.path.lexists(name)\n"
        "        assert not os.path.lexists('/project/' + name)\n"
        f"        assert not os.path.lexists({str(project)!r} + '/' + name)\n"
        "        with pytest.raises(FileNotFoundError):\n"
        "            os.stat(name)\n"
        f"    assert not os.path.lexists({str(project)!r})\n"
        "    assert sorted(os.listdir('/project')) == ['data', 'pyproject.toml', 'tests']\n"
        "    assert os.listdir('/project/data') == ['declared.json']\n"
        "    child = __import__('subprocess').run(\n"
        "        ['cat', 'notes.txt'], capture_output=True, timeout=10\n"
        "    )\n"
        "    assert child.returncode != 0\n",
    )
    report = passed(project)
    assert report["route_counts"]["scoped"]["executed"] == 1
    assert report["units"][0]["route"] == "scoped"


def test_host_interpreter_and_installed_packages_are_usable_and_read_only(project):
    pytest_dir = Path(pytest.__file__).parent
    stdlib = sysconfig.get_path("stdlib")
    put(
        project,
        "tests/test_environment.py",
        "import errno\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.telescope(inputs=[])\n"
        "def test_environment():\n"
        f"    assert sys.executable == {sys.executable!r}\n"
        f"    assert sys.prefix == {sys.prefix!r}\n"
        f"    assert Path(pytest.__file__).parent == Path({str(pytest_dir)!r})\n"
        "    child = subprocess.run(\n"
        "        [sys.executable, '-c', 'import json, sqlite3; print(json.dumps([1]))'],\n"
        "        capture_output=True, text=True, timeout=30,\n"
        "    )\n"
        "    assert child.returncode == 0 and child.stdout == '[1]\\n', child.stderr\n"
        "    for directory in (\n"
        f"        {str(pytest_dir)!r}, sys.prefix, {stdlib!r}, os.path.dirname(sys.executable)\n"
        "    ):\n"
        "        with pytest.raises(OSError) as refused:\n"
        "            Path(directory, 'telescope-write-probe').write_text('x')\n"
        "        assert refused.value.errno in (errno.EROFS, errno.EACCES)\n"
        "        assert not os.path.exists(os.path.join(directory, 'telescope-write-probe'))\n",
    )
    passed(project)
    assert not (pytest_dir / "telescope-write-probe").exists()
    assert not Path(sys.prefix, "telescope-write-probe").exists()


def test_writes_outside_private_scratch_are_refused(project):
    put(
        project,
        "tests/test_writes.py",
        "import errno\n"
        "import os\n"
        "import tempfile\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.telescope(inputs=['data/declared.json'])\n"
        "def test_writes(tmp_path):\n"
        "    for target in ('/project/data/declared.json', '/project/new.txt', '/new.txt',\n"
        "                   '/usr/new.txt', '/etc/new.txt', '/dev/new.txt', '/proc/new.txt'):\n"
        "        with pytest.raises(OSError) as refused:\n"
        "            Path(target).write_text('x')\n"
        "        assert refused.value.errno in (errno.EROFS, errno.EACCES, errno.ENOENT)\n"
        "    with pytest.raises(OSError):\n"
        "        os.mkdir('/telescope-new')\n"
        "    assert os.environ['HOME'] == os.environ['TMPDIR'] == '/tmp'\n"
        "    assert tempfile.gettempdir() == '/tmp'\n"
        "    Path('/tmp/scratch.txt').write_text('scratch')\n"
        "    Path('/dev/shm/shared.txt').write_text('shared')\n"
        "    assert str(tmp_path).startswith('/tmp/')\n"
        "    (tmp_path / 'fixture.txt').write_text('fixture')\n"
        "    with open('/dev/null', 'w') as sink:\n"
        "        sink.write('discarded')\n",
    )
    passed(project)
    assert (project / "data/declared.json").read_text() == '{"visible": true}\n'
    assert not (project / "new.txt").exists()


def test_scratch_and_processes_are_private_to_each_sandbox(project):
    marker = "telescope-" + uuid.uuid4().hex
    put(
        project,
        "tests/test_private.py",
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.telescope(inputs=[])\n"
        "@pytest.mark.parametrize('turn', ['first', 'second'])\n"
        "def test_private(turn):\n"
        f"    assert not Path('/tmp/{marker}').exists()\n"
        f"    Path('/tmp/{marker}').write_text(turn)\n"
        "    assert os.getpid() == 1 or os.getppid() == 1\n"
        f"    assert not os.path.exists('/proc/{os.getpid()}/root/{marker}')\n"
        "    assert sorted(int(name) for name in os.listdir('/proc') if name.isdecimal())[0] == 1\n",
    )
    report = passed(project)
    assert report["counts"]["executed"] == 2
    assert [group["route"] for group in report["groups"]].count("scoped") == 2
    assert not Path("/tmp", marker).exists()


def test_scoped_tests_share_the_host_network_namespace(project):
    host_network = os.readlink("/proc/self/ns/net")
    put(
        project,
        "tests/test_network.py",
        "import os\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.telescope(inputs=[])\n"
        "def test_network():\n"
        f"    assert os.readlink('/proc/self/ns/net') == {host_network!r}\n",
    )
    passed(project)


def test_mixed_file_scoped_cases_never_run_on_the_host(project, monkeypatch):
    put(
        project,
        "tests/test_mixed.py",
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "@pytest.fixture\n"
        "def where():\n"
        "    with open(os.path.join(os.environ.get('TELESCOPE_HOST_LOG', '/tmp'), 'setups.log'), 'a') as log:\n"
        "        log.write(str(Path.cwd()) + '\\n')\n"
        "    return Path.cwd()\n"
        "\n"
        "def test_ordinary(where):\n"
        "    assert Path('notes.txt').exists()\n"
        "\n"
        "@pytest.mark.telescope(inputs=[])\n"
        "def test_scoped(where):\n"
        "    assert where == Path('/project')\n"
        "    assert not Path('notes.txt').exists()\n",
    )
    # Ordinary sessions inherit this variable; sandboxes never receive TELESCOPE_* names.
    monkeypatch.setenv("TELESCOPE_HOST_LOG", str(project))
    first = passed(project)
    second = passed(project)
    for report in (first, second):
        routes = {row["route"]: row for row in report["units"]}
        assert set(routes) == {"ordinary", "scoped"}
        assert routes["ordinary"]["outcome"] == "executed"
        assert report["route_counts"]["ordinary"]["executed"] == 1
    assert first["route_counts"]["scoped"]["executed"] == 1
    assert second["route_counts"]["scoped"]["reused"] == 1
    # Only the two ordinary runs set up the fixture on the host.
    assert (project / "setups.log").read_text() == f"{project}\n{project}\n"


def test_case_mode_is_default_and_batch_is_opt_in(project):
    put(
        project,
        "tests/test_fixture_lifetime.py",
        "import pytest\n"
        "pytestmark = pytest.mark.telescope(inputs=[])\n"
        "STATE = []\n"
        "\n"
        "@pytest.fixture(scope='module')\n"
        "def shared():\n"
        "    return STATE\n"
        "\n"
        "@pytest.mark.parametrize('turn', range(3))\n"
        "def test_lifetime(shared, turn):\n"
        "    shared.append(turn)\n"
        "    # Only a shared session has seen the earlier turns.\n"
        "    assert turn < 2 or shared == [0, 1, 2]\n",
    )
    case = controller.run(project, no_cache=True)
    assert case["execution"]["mode"] == "case"
    assert [group["profile"] for group in case["groups"]] == ["case-v1"] * 3
    assert case["status"] == "failed"
    assert case["counts"]["executed"] == 2 and case["counts"]["failed"] == 1
    batch = passed(project, no_cache=True, execution_mode="batch")
    assert [group["profile"] for group in batch["groups"]] == ["batch-v1"]
    assert len(batch["groups"][0]["case_ids"]) == 3
    assert batch["counts"]["executed"] == 3


def test_independent_cases_overlap_up_to_the_worker_count(project):
    token = uuid.uuid4().hex
    put(
        project,
        "tests/test_overlap.py",
        "import socket\n"
        "import time\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.telescope(inputs=[])\n"
        "@pytest.mark.parametrize('name', ['left', 'right'])\n"
        "def test_overlap(name):\n"
        "    # Abstract sockets live in the shared network namespace, not the filesystem.\n"
        "    mine = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        f"    mine.bind('\\0{token}-' + name)\n"
        "    mine.listen()\n"
        "    other = 'right' if name == 'left' else 'left'\n"
        "    deadline = time.monotonic() + 20\n"
        "    while True:\n"
        "        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "        try:\n"
        f"            probe.connect('\\0{token}-' + other)\n"
        "            break\n"
        "        except OSError:\n"
        "            probe.close()\n"
        "            if time.monotonic() > deadline:\n"
        "                pytest.fail('the other sandbox never ran concurrently')\n"
        "            time.sleep(0.05)\n"
        "    # Stay alive until the other case has connected here too.\n"
        "    mine.settimeout(max(0.1, deadline - time.monotonic()))\n"
        "    peer, _ = mine.accept()\n"
        "    peer.close()\n"
        "    probe.close()\n",
    )
    report = passed(project, workers=2, no_cache=True, timeout=60)
    assert report["execution"]["workers"] == 2
    assert report["counts"]["executed"] == 2


def test_cache_identity_covers_the_installed_distributions(project, tmp_path):
    """A second environment that sees this one's packages, plus its own installs."""
    environment = tmp_path / "env"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    interpreter = environment / "bin" / "python"
    site_packages = Path(
        subprocess.run(
            [
                str(interpreter),
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    )
    shared = {sysconfig.get_path("purelib")}
    import pytest_telescope

    shared.add(str(Path(pytest_telescope.__file__).parent.parent))
    (site_packages / "shared.pth").write_text("".join(path + "\n" for path in sorted(shared)))
    put(
        project,
        "tests/test_cached.py",
        "import pytest\n@pytest.mark.telescope(inputs=[])\ndef test_cached():\n    pass\n",
    )

    def run():
        completed = subprocess.run(
            [str(interpreter), "-m", "pytest_telescope", "run"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return json.loads(completed.stdout)

    assert run()["counts"]["executed"] == 1
    assert run()["counts"]["reused"] == 1
    record = site_packages / "example_extra-1.0.dist-info"
    record.mkdir()
    (record / "METADATA").write_text("Metadata-Version: 2.1\nName: example_extra\nVersion: 1.0\n")
    assert run()["counts"]["executed"] == 1
    assert run()["counts"]["reused"] == 1
