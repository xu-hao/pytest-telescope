"""Match actual CPython path-file and metadata lookup semantics before imports."""

import json
import uuid

import pytest

from pytest_telescope import python_source, runtime
from pytest_telescope.model import UnsupportedCapability


@pytest.mark.parametrize("name", ["src ", "src\u00a0", "src\u2028nested"])
def test_unrepresentable_path_file_roots_refuse(tmp_path, name):
    (tmp_path / name).mkdir()
    with pytest.raises(UnsupportedCapability, match="path"):
        python_source.normalize(tmp_path, {"profile": "isolated-source-v1", "source_roots": [name]})


@pytest.mark.parametrize(
    "root,entry",
    [
        ("src", "Example-1.0.DIST-INFO/METADATA"),
        ("src", "Example-1.0.EGG-INFO/PKG-INFO"),
        ("Example-1.0.EGG", "EGG-INFO/PKG-INFO"),
    ],
)
def test_metadata_namespace_aliases_refuse(tmp_path, root, entry):
    (tmp_path / root).mkdir()
    settings = python_source.normalize(
        tmp_path, {"profile": "isolated-source-v1", "source_roots": [root]}
    )
    with pytest.raises(UnsupportedCapability) as error:
        python_source.validate_view(settings, [root + "/" + entry])
    assert error.value.capability == "python:source-layout"


def test_pinned_python_path_file_and_metadata_interpretation(tmp_path):
    view = tmp_path / "view"
    for name in (
        "site",
        "src",
        "src ",
        "source/Example-1.0.DIST-INFO",
        "ExampleEgg-1.0.EGG/EGG-INFO",
    ):
        (view / name).mkdir(parents=True)
    (view / "site/example.pth").write_text("/proof/src \n")
    (view / "source/Example-1.0.DIST-INFO/METADATA").write_text("Name: Example\nVersion: 1.0\n")
    (view / "ExampleEgg-1.0.EGG/EGG-INFO/PKG-INFO").write_text("Name: ExampleEgg\nVersion: 1.0\n")
    for path in view.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o444)
    view.chmod(0o755)
    prepared = runtime.build_runtime(tmp_path)
    token = uuid.uuid4().hex
    name = "telescope-path-semantics-" + token
    label = "org.pytest-telescope.path-proof"
    try:
        code = (
            "import site,sys,json,importlib.metadata as m; site.addsitedir('/proof/site'); "
            "print(json.dumps({'trimmed':'/proof/src' in sys.path,'exact':'/proof/src ' in sys.path,"
            "'metadata':sorted(d.metadata['Name'] for d in m.distributions(path=['/proof/source','/proof/ExampleEgg-1.0.EGG']))}))"
        )
        result = runtime.command(
            [
                "docker",
                "run",
                "--name",
                name,
                "--label",
                label + "=" + token,
                "--read-only",
                "--network=none",
                "--ipc=none",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user=65534:65534",
                "--cpus=1",
                "--memory=512m",
                "--memory-swap=512m",
                "--pids-limit=128",
                "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m",
                "--workdir=/tmp",
                "--mount",
                f"type=bind,src={view},dst=/proof,readonly",
                "--entrypoint=python",
                prepared["image"],
                "-I",
                "-c",
                code,
            ],
            timeout=30,
        )
        assert json.loads(result) == {
            "trimmed": True,
            "exact": False,
            "metadata": ["Example", "ExampleEgg"],
        }
    finally:
        info = json.loads(runtime.command(["docker", "inspect", name]))[0]
        assert info["Config"]["Labels"][label] == token
        runtime.command(["docker", "rm", "-f", name])
        runtime._image_info(prepared["image"], runtime._project(tmp_path))
        runtime.command(["docker", "image", "rm", prepared["image"]])
