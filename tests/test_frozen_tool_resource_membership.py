"""Actual native process resource membership with small, unexecuted ELF inputs."""

import pytest
from frozen_tool_ordering_fixture import Ordering


@pytest.mark.parametrize("association", ["none", "file", "directory"])
def test_resource_membership_is_explicit_and_stays_with_its_entrypoint(
    tmp_path, monkeypatch, association
):
    fixture = Ordering(tmp_path, monkeypatch)
    fixture.object("resources/left.so")
    fixture.object("resources/right.so")
    resources = {"none": [], "file": ["resources/left.so"], "directory": ["resources"]}[association]
    fixture.entry("bin/python3.12", resources=resources)
    fixture.entry("bin/other", resources=["resources/right.so"])
    fixture.finish()
    capture = fixture.capture()
    with capture:
        runtime = capture.runtimes["native"]
        processes = dict(runtime.processes(capture._budget()))
        worker, other = ("prefix", "bin/python3.12"), ("prefix", "bin/other")
        left, right = ("prefix", "resources/left.so"), ("prefix", "resources/right.so")
        selected = {"none": set(), "file": {left}, "directory": {left, right}}[association]
        assert processes[worker] == {worker} | selected
        assert processes[other] == {other, right}
        assert left in runtime.elfs and right in runtime.elfs
    assert not capture.directory.exists()
