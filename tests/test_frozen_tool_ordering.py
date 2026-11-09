"""Observable native ordering proof controls; no synthetic ELF is executed."""

import pytest
from frozen_tool_ordering_fixture import Ordering

from pytest_telescope.model import UnsupportedCapability


@pytest.fixture
def ordered(tmp_path, monkeypatch):
    return Ordering(tmp_path, monkeypatch)


def refusal(ordered, names=("native",), *, union=False, capability="tool:layout"):
    captured = ordered.capture(names)
    with pytest.raises(UnsupportedCapability) as caught:
        with captured as actual:
            if union:
                actual.view(names)
    assert caught.value.capability == capability
    assert not captured.directory.exists()


@pytest.mark.parametrize("tag", ["rpath", "runpath"])
def test_own_ordered_slot_shadows_different_fallback(ordered, tag):
    ordered.object("a/libchoice.so", soname="libchoice.so")
    ordered.object("fallback/libchoice.so", soname="libchoice.so")
    ordered.fallback("libchoice.so", "fallback/libchoice.so")
    ordered.entry("bin/python3.12", needed=["libchoice.so"], libraries=["a/libchoice.so"],
                  **{tag: "$ORIGIN/../a"})
    ordered.finish()
    captured = ordered.capture()
    with captured as actual:
        assert ordered.targets(actual) == {str(ordered.prefix / "a/libchoice.so")}
    assert not captured.directory.exists()


def test_empty_earlier_slot_selects_later_before_fallback(ordered):
    ordered.object("b/libchoice.so", soname="libchoice.so")
    ordered.object("fallback/libchoice.so", soname="libchoice.so")
    ordered.fallback("libchoice.so", "fallback/libchoice.so")
    ordered.entry("bin/python3.12", needed=["libchoice.so"], libraries=["b/libchoice.so"],
                  rpath="$ORIGIN/../empty:$ORIGIN/../b")
    ordered.finish()
    with ordered.capture() as actual:
        assert ordered.targets(actual) == {str(ordered.prefix / "b/libchoice.so")}


@pytest.mark.parametrize("conflict", [False, True])
def test_all_inherited_blocks_must_miss_or_agree(ordered, conflict):
    ordered.object("resources/extension.so", needed=["libchoice.so"])
    ordered.object("fallback/libchoice.so", soname="libchoice.so")
    ordered.fallback("libchoice.so", "fallback/libchoice.so")
    if conflict:
        ordered.object("a/libchoice.so", soname="libchoice.so")
    ordered.entry("bin/python3.12", resources=["resources"], rpath="$ORIGIN/../a")
    ordered.finish()
    if conflict:
        refusal(ordered)
    else:
        with ordered.capture() as actual:
            assert ordered.targets(actual, "resources/extension.so") == {str(ordered.prefix / "fallback/libchoice.so")}


def test_ancestry_only_dependency_is_not_admitted(ordered):
    ordered.object("resources/extension.so", needed=["libchoice.so"])
    ordered.object("a/libchoice.so", soname="libchoice.so")
    ordered.entry("bin/python3.12", resources=["resources"], rpath="$ORIGIN/../a")
    ordered.finish()
    refusal(ordered)


def test_ambiguous_fallback_is_not_sorted_into_a_winner(ordered):
    for directory in ("a", "b"):
        ordered.object(directory + "/libchoice.so", soname="libchoice.so")
        ordered.fallback("libchoice.so", directory + "/libchoice.so")
    ordered.entry("bin/python3.12", needed=["libchoice.so"], libraries=["a/libchoice.so"])
    ordered.finish()
    refusal(ordered)


def test_diamond_and_cycle_keep_exact_transitive_membership(ordered):
    ordered.object("a/libleft.so", needed=["libshared.so"], soname="libleft.so", rpath="$ORIGIN")
    ordered.object("a/libright.so", needed=["libshared.so"], soname="libright.so", rpath="$ORIGIN")
    ordered.object("a/libshared.so", needed=["libleft.so"], soname="libshared.so", rpath="$ORIGIN")
    ordered.entry("bin/python3.12", needed=["libleft.so", "libright.so"],
                  libraries=["a/libleft.so", "a/libright.so", "a/libshared.so"], rpath="$ORIGIN/../a")
    ordered.finish()
    with ordered.capture() as actual:
        assert ordered.targets(actual) == {str(ordered.prefix / "a" / name) for name in ("libleft.so", "libright.so")}
        assert ordered.targets(actual, "a/libshared.so") == {str(ordered.prefix / "a/libleft.so")}


def two_contexts(ordered, same=False):
    for directory in ("a", "b"):
        ordered.object(directory + "/libchoice.so", soname="libchoice.so")
    ordered.entry("bin/python3.12", needed=["libchoice.so"], libraries=["a/libchoice.so"],
                  resources=["resources"] if same else [], rpath="$ORIGIN/../a")
    if same:
        ordered.object("resources/extension.so", needed=["libchoice.so"], rpath="$ORIGIN/../b")
    else:
        ordered.entry("bin/other", needed=["libchoice.so"], libraries=["b/libchoice.so"], rpath="$ORIGIN/../b")
    ordered.finish()


def test_distinct_exec_contexts_keep_distinct_same_soname(ordered):
    two_contexts(ordered)
    with ordered.capture() as actual:
        assert ordered.targets(actual) == {str(ordered.prefix / "a/libchoice.so")}
        assert ordered.targets(actual, "bin/other") == {str(ordered.prefix / "b/libchoice.so")}


def test_one_resource_context_refuses_conflicting_needed_name(ordered):
    two_contexts(ordered, same=True)
    refusal(ordered)


def test_one_context_refuses_distinct_files_with_same_soname(ordered):
    ordered.object("a/libleft.so", soname="shared-name.so")
    ordered.object("a/libright.so", soname="shared-name.so")
    ordered.entry("bin/python3.12", needed=["libleft.so", "libright.so"],
                  libraries=["a/libleft.so", "a/libright.so"], rpath="$ORIGIN/../a")
    ordered.finish()
    refusal(ordered)


def test_same_executable_across_tools_combines_resource_context(ordered):
    ordered.object("resources/left.so", soname="shared-name.so")
    ordered.object("resources/right.so", soname="shared-name.so")
    ordered.finish()
    ordered.partition("left", {"resources/left.so"}, resources=["resources/left.so"])
    ordered.partition("right", {"resources/right.so"}, resources=["resources/right.so"])
    with ordered.capture(("left",)):
        pass
    with ordered.capture(("right",)):
        pass
    refusal(ordered, ("left", "right"), union=True)


def test_original_logical_search_drift_refuses(ordered):
    ordered.object("a/libchoice.so", soname="libchoice.so")
    ordered.entry("bin/python3.12", needed=["libchoice.so"], libraries=["a/libchoice.so"],
                  rpath="$ORIGIN/../a")
    ordered.finish()
    from frozen_tool_fixtures import entries
    root = ordered.document["roots"][0]
    root["entries"] = [row for row in root["entries"] if not (row["path"] == "a" or row["path"].startswith("a/"))]
    ordered.document["roots"].append({"id": "separate", "source": str(ordered.prefix / "a"),
                                       "coverage": "tree", "preserve_source": True, "entries": entries(ordered.prefix / "a")})
    ordered.document["executables"][0]["libraries"] = [{"root": "separate", "path": "libchoice.so"}]
    ordered.project.save("native")
    refusal(ordered)


def test_differing_hardware_descendant_refuses(ordered):
    ordered.object("a/libchoice.so", soname="libchoice.so")
    ordered.object("a/glibc-hwcaps/x86-64-v3/libchoice.so", soname="libchoice.so")
    ordered.entry("bin/python3.12", needed=["libchoice.so"], libraries=["a/libchoice.so"], rpath="$ORIGIN/../a")
    ordered.finish()
    refusal(ordered)


@pytest.mark.parametrize("changed", [False, True])
def test_union_reproves_winner_and_allows_shadowed_entry(ordered, changed):
    ordered.object("a/libchoice.so", soname="libchoice.so")
    ordered.object("b/libchoice.so", soname="libchoice.so")
    ordered.entry("bin/python3.12", needed=["libchoice.so"], libraries=["b/libchoice.so" if changed else "a/libchoice.so"],
                  rpath="$ORIGIN/../a:$ORIGIN/../b")
    ordered.finish()
    selected = "b/libchoice.so" if changed else "a/libchoice.so"
    added = "a/libchoice.so" if changed else "b/libchoice.so"
    ordered.partition("subject", {selected})
    ordered.partition("addition", {added})
    # Construct a second real frozen Graph for the union seam. This is a mapping
    # control, not standalone native-runtime admission or an executed union view.
    ordered.project.documents["addition"]["executables"] = []
    ordered.project.documents["addition"]["loader"] = None
    addition = ordered.project.documents["addition"]
    addition["roots"] = [addition["roots"][0]]
    addition["roots"][0]["entries"] = [row for row in addition["roots"][0]["entries"]
                                        if row["path"] in {".", added.split('/')[0], added}]
    ordered.project.save("addition")
    with ordered.capture(("subject",)) as actual:
        import time

        from pytest_telescope.tool_capture import Budget, _measure, _remove
        from pytest_telescope.tool_context import load_selected
        from pytest_telescope.tool_graph import Graph

        tool = load_selected(ordered.project.root, ordered.project.definitions, ["addition"])["addition"]
        destination = ordered.project.external / "union-copy"
        destination.mkdir()
        budget = Budget(time.monotonic() + 10, None)
        for row in addition["roots"][0]["entries"]:
            _measure(ordered.prefix / row["path"], row, budget, destination / "prefix" / row["path"])
        for row in reversed(addition["roots"][0]["entries"]):
            if row["kind"] == "directory":
                (destination / "prefix" / row["path"]).chmod(row["mode"] & 0o555)
        graph = Graph(tool, destination, Budget(time.monotonic() + 10, None))
        originals = {g.original(key): (g, key) for g in (actual.graphs["subject"], graph) for key in g.nodes}
        runtime = actual.runtimes["subject"]
        try:
            if changed:
                with pytest.raises(UnsupportedCapability):
                    runtime.validate_union(originals, actual._budget())
            else:
                runtime.validate_union(originals, actual._budget())
                assert ordered.targets(actual, name="subject") == {str(ordered.prefix / selected)}
        finally:
            _remove(destination)


def test_runpath_does_not_inherit_other_resource_rpath(ordered):
    ordered.object("resources/ancestor.so", rpath="$ORIGIN/../a")
    ordered.object("resources/requester.so", needed=["libchoice.so"], runpath="$ORIGIN/../empty")
    ordered.object("a/libchoice.so", soname="libchoice.so")
    ordered.object("fallback/libchoice.so", soname="libchoice.so")
    ordered.fallback("libchoice.so", "fallback/libchoice.so")
    ordered.entry("bin/python3.12", resources=["resources"])
    ordered.finish()
    with ordered.capture() as actual:
        assert ordered.targets(actual, "resources/requester.so") == {str(ordered.prefix / "fallback/libchoice.so")}


@pytest.mark.parametrize("tags", ["duplicate_rpath", "duplicate_runpath", "duplicate_soname", "dual"])
def test_duplicate_or_dual_tags_refuse(ordered, tags):
    values = {"rpath": "$ORIGIN/../a"}
    if tags == "duplicate_rpath":
        values["extra"] = [(15, "$ORIGIN/../b")]
    elif tags == "duplicate_runpath":
        values = {"runpath": "$ORIGIN/../a", "extra": [(29, "$ORIGIN/../b")]}
    elif tags == "duplicate_soname":
        values = {"soname": "one", "extra": [(14, "two")]}
    else:
        values["runpath"] = "$ORIGIN/../b"
    ordered.entry("bin/python3.12", **values)
    ordered.finish()
    refusal(ordered)


def test_context_state_bound_refuses_and_cleans_capture(ordered):
    # 257 native resources in257 executable contexts require more than65536
    # context/object tuples, while total declaration relations remain below4096
    # by referring to one resource subtree per executable.
    for index in range(257):
        ordered.object(f"resources/item{index}.so", soname=f"owned{index}.so")
    for index in range(256):
        ordered.entry(f"bin/process{index}", resources=["resources"])
    ordered.entry("bin/python3.12", resources=["resources"])
    ordered.finish()
    refusal(ordered, capability="tool:bounds")
