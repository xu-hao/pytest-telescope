"""Absolute and cache lookup must select actual declared graph members."""

import pytest
from frozen_tool_ordering_fixture import Ordering

from pytest_telescope.model import UnsupportedCapability


@pytest.mark.parametrize("selection", ["declared", "missing", "outside-closed-search"])
def test_absolute_needed_requires_pinned_closed_target(tmp_path, monkeypatch, selection):
    fixture = Ordering(tmp_path, monkeypatch)
    relative = "unclosed/libchoice.so" if selection == "outside-closed-search" else "a/libchoice.so"
    target = fixture.prefix / relative
    if selection != "missing":
        fixture.object(relative)
    fixture.entry("bin/python3.12", needed=[str(target)],
                  libraries=[] if selection == "missing" else [relative])
    fixture.finish()
    capture = fixture.capture()
    if selection == "declared":
        with capture:
            assert fixture.targets(capture) == {str(target)}
    else:
        with pytest.raises(UnsupportedCapability) as caught:
            with capture:
                pytest.fail("unsupported absolute lookup was admitted")
        assert caught.value.capability == "tool:layout"
    assert not capture.directory.exists()


@pytest.mark.parametrize("selected", [True, False], ids=["selected", "excluded"])
def test_cache_target_cannot_borrow_unselected_owned_source(tmp_path, monkeypatch, selected):
    fixture = Ordering(tmp_path, monkeypatch)
    target = fixture.object("a/libchoice.so")
    fixture.entry("bin/python3.12", needed=["libchoice.so"],
                  libraries=["a/libchoice.so"] if selected else [])
    fixture.fallback("libchoice.so", "a/libchoice.so")
    fixture.finish()
    if not selected:
        rows = fixture.document["roots"][0]["entries"]
        rows[:] = [row for row in rows if row["path"] != "a/libchoice.so"]
        fixture.project.save("native")
    capture = fixture.capture()
    if selected:
        with capture:
            assert fixture.targets(capture) == {str(target)}
    else:
        with pytest.raises(UnsupportedCapability) as caught:
            with capture:
                pytest.fail("an unselected cache target was admitted")
        assert caught.value.capability == "tool:layout"
    assert target.is_file()
    assert not capture.directory.exists()
