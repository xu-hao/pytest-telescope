"""Real native projections and worker request construction, intercepted before launch."""

import copy
import shutil
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from frozen_tool_configuration_fixtures import StaticConfiguration

from pytest_telescope import sandbox, tool_capture, tool_context
from pytest_telescope.model import InfrastructureError, UnsupportedCapability


class RequestObserved(Exception):
    pass


@pytest.mark.parametrize("changed", ["unchanged", "executable", "binds", "description"])
def test_native_worker_request_keeps_isolation_flags_and_exact_projected_context(
    tmp_path, monkeypatch, changed
):
    fixture = StaticConfiguration(tmp_path, monkeypatch)
    tools = tool_context.load_selected(
        fixture.project.root, fixture.project.definitions, ["native"]
    )
    calls, started = [], []

    def observe(argv, timeout, **kwargs):
        calls.append((argv, kwargs))
        raise RequestObserved

    monkeypatch.setattr(sandbox, "capture", observe)
    with sandbox.invocation(fixture.project.root) as directory:
        project_view = directory / "project"
        project_view.mkdir()
        with tool_capture.Capture(
            SimpleNamespace(root=fixture.project.root, tools=tools),
            directory,
            10,
            None,
            sandbox=fixture.box,
        ) as capture:
            view = capture.view(["native"])
            assert view.native and capture.runtimes["native"].elfs
            box = fixture.box
            if changed == "executable":
                box = replace(box, executable=str(fixture.prefix / "bin/python"))
            elif changed == "binds":
                box = replace(box, binds=())
            elif changed == "description":
                description = copy.deepcopy(box.description)
                description["path"].append(str(fixture.prefix / "extra-search"))
                box = replace(box, description=description)
            expected = RequestObserved if changed == "unchanged" else InfrastructureError
            with pytest.raises(expected):
                sandbox.execute(
                    box,
                    project_view,
                    [],
                    {},
                    10,
                    root=fixture.project.root,
                    invocation_directory=directory,
                    tool_view=view,
                    request={
                        "mode": "collection",
                        "registry": [],
                        "selectors": [],
                        "context_id": "0" * 64,
                    },
                    on_session_start=lambda: started.append(True),
                )
            if changed == "unchanged":
                assert len(calls) == 1
                argv, kwargs = calls[0]
                assert argv[-4:] == [box.executable, "-I", "-B", sandbox.WORKER_PATH]
                mounts = [
                    (argv[index + 1], argv[index + 2])
                    for index, word in enumerate(argv)
                    if word == "--ro-bind"
                ]
                for destination in view.closed:
                    owned = dict((target, source) for source, target in view.bindings)[destination]
                    assert (str(owned), destination) in mounts
                    assert (destination, destination) not in mounts
                assert kwargs["on_start"] is not None
            else:
                assert not calls
            assert not started
    assert not directory.exists()


@pytest.mark.parametrize(
    "destination",
    ["/project", "/project/node_modules", "/project/src/vendor/tool", "/", "/tools/frontend"],
)
def test_a_tool_binding_that_overlaps_the_project_view_refuses_before_launch(
    tmp_path, monkeypatch, destination
):
    # The frozen project is shown at /project, wherever the checkout lives. A frozen-tool
    # binding at, below or above that path would replace or hide part of the project
    # view, so it refuses; a binding in the tool namespace reaches the launch.
    project = tmp_path / "checkout"
    project.mkdir()
    box = replace(sandbox.describe(project, sys.executable), bwrap=shutil.which("bwrap"))
    calls = []

    def observe(argv, timeout, **kwargs):
        calls.append(argv)
        raise RequestObserved

    monkeypatch.setattr(sandbox, "capture", observe)
    with sandbox.invocation(project) as directory:
        project_view = directory / "project"
        project_view.mkdir()
        # Owned frozen material of this invocation, as a captured tool leaves it.
        source = directory / "tools" / "frontend" / "install"
        source.mkdir(parents=True)
        (source / "value.txt").write_text("frontend\n")
        view = tool_capture.ToolView(
            directory / "tools", ("frontend",), ((source, destination),), "0" * 64
        )

        def execute():
            sandbox.execute(
                box,
                project_view,
                [],
                {},
                10,
                root=project,
                invocation_directory=directory,
                tool_view=view,
                request={
                    "mode": "collection",
                    "registry": [],
                    "selectors": [],
                    "context_id": "0" * 64,
                },
            )

        if destination == "/tools/frontend":
            with pytest.raises(RequestObserved):
                execute()
            [argv] = calls
            assert ["--ro-bind", str(source), destination] in [
                argv[index : index + 3] for index in range(len(argv))
            ]
        else:
            with pytest.raises(UnsupportedCapability) as refused:
                execute()
            assert refused.value.capability == "tool:layout"
            assert str(project) not in str(refused.value)
            assert not calls
    assert not directory.exists()
