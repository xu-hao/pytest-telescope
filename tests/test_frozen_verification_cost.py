"""Each distinct frozen Python source is read and hashed once per invocation.

The end-to-end control runs real Bubblewrap. The direct controls build an owned frozen
pool and ask the registry for the same view repeatedly, as the controller does for
collection, key computation and each fresh worker.
"""

import collections
import hashlib
import os
import types

import pytest
from test_helper_registry import consumer, project_at
from test_helper_registry_contract import passed

from pytest_telescope import frozen_registry
from pytest_telescope.model import InfrastructureError, StaticDiscovery

HELPER = b'import pytest\npytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])\n'
MODULE = "helpers/support.py"


def frozen_pool(tmp_path, aliases):
    """One frozen module and its leaf-link aliases, with the registry that labels them."""
    pool = tmp_path / "pool"
    (pool / "helpers").mkdir(parents=True)
    module = pool / MODULE
    module.write_bytes(HELPER)
    module.chmod(0o444)
    # A frozen file carries the fixed timestamp that freezing gives every pool member.
    os.utime(module, ns=(0, 0))
    records = [
        {
            "path": MODULE,
            "kind": "file",
            "sha256": hashlib.sha256(HELPER).hexdigest(),
            "mode": 0o444,
        }
    ]
    for number in range(aliases):
        name = f"helpers/alias_{number}.py"
        (pool / name).symlink_to("support.py")
        records.append({"path": name, "kind": "symlink", "target": "support.py"})
    unit = types.SimpleNamespace(
        inputs=[record["path"] for record in records], absent=[], tool_links={}
    )
    registry = frozen_registry.FrozenRegistry(pool, records, {}, StaticDiscovery(pool).pool(unit))
    return registry, unit, module


@pytest.fixture
def frozen_reads(monkeypatch):
    """Every frozen file the registry reads, in order, with its byte count."""
    regular_bytes = frozen_registry.regular_bytes
    reads = []

    def counted(directory, name, limit):
        content, measured = regular_bytes(directory, name, limit)
        reads.append((name, len(content)))
        return content, measured

    monkeypatch.setattr(frozen_registry, "regular_bytes", counted)
    return reads


def rewrite(module, content):
    module.chmod(0o644)
    module.write_bytes(content)
    module.chmod(0o444)


def test_repeated_views_and_aliases_read_each_distinct_frozen_file_once(tmp_path, frozen_reads):
    registry, unit, _ = frozen_pool(tmp_path, aliases=200)

    views = [registry.for_view(unit) for _ in range(6)]

    assert frozen_reads == [(MODULE, len(HELPER))]
    assert all(view == views[0] for view in views)
    # Every import origin still authenticates its own record of the one parsed call.
    assert sorted(row["file"] for row in views[0]) == sorted(unit.inputs)
    assert len({row["id"] for row in views[0]}) == 201


def test_a_narrower_later_view_reuses_the_verification_of_a_shared_file(tmp_path, frozen_reads):
    registry, unit, _ = frozen_pool(tmp_path, aliases=3)
    narrow = types.SimpleNamespace(inputs=[MODULE, "helpers/alias_1.py"], absent=[], tool_links={})

    assert len(registry.for_view(unit)) == 4
    assert [row["file"] for row in registry.for_view(narrow)] == ["helpers/alias_1.py", MODULE]
    assert frozen_reads == [(MODULE, len(HELPER))]


def test_one_scoped_run_reads_each_frozen_python_source_once(tmp_path, frozen_reads):
    root = project_at(tmp_path)
    for number in range(4):
        (root / f"helpers/alias_{number}.py").symlink_to("support.py")
    inputs = ["helpers/__init__.py", "helpers/support.py", "data/answer.txt"]
    inputs += [f"helpers/alias_{number}.py" for number in range(4)]
    extra = "".join(f'def test_more_{number}(): assert answer() == "42"\n' for number in range(2))
    consumer(root, inputs=inputs, extra=extra)

    passed(root, executed=3, selected=3)

    counts = collections.Counter(name for name, _ in frozen_reads)
    assert counts[MODULE] == 1
    assert set(counts.values()) == {1}


@pytest.mark.parametrize("fault", ["bytes", "same_size_bytes", "mode", "missing"])
def test_tampering_found_by_the_first_verification_refuses_every_view(tmp_path, fault):
    registry, unit, module = frozen_pool(tmp_path, aliases=2)
    if fault == "bytes":
        rewrite(module, HELPER + b"# changed frozen bytes\n")
    elif fault == "same_size_bytes":
        rewrite(module, HELPER.replace(b"private", b"PRIVATE"))
        os.utime(module, ns=(0, 0))
    elif fault == "mode":
        module.chmod(0o544)
    else:
        module.unlink()

    for _ in range(2):
        # A refused file is never remembered as verified.
        with pytest.raises(InfrastructureError, match="frozen declaration source"):
            registry.for_view(unit)


@pytest.mark.parametrize("fault", ["bytes", "same_size_bytes", "mode", "missing", "replaced"])
def test_a_frozen_file_changed_after_its_verification_refuses_later_views(tmp_path, fault):
    registry, unit, module = frozen_pool(tmp_path, aliases=2)
    verified = registry.for_view(unit)
    assert registry.for_view(unit) == verified
    if fault == "bytes":
        rewrite(module, HELPER + b"# changed frozen bytes\n")
    elif fault == "same_size_bytes":
        rewrite(module, HELPER.replace(b"private", b"PRIVATE"))
    elif fault == "mode":
        module.chmod(0o544)
    elif fault == "missing":
        module.unlink()
    else:
        module.unlink()
        module.write_bytes(HELPER.replace(b"private", b"PRIVATE"))
        module.chmod(0o444)
        os.utime(module, ns=(0, 0))

    for _ in range(2):
        with pytest.raises(InfrastructureError, match="frozen declaration source"):
            registry.for_view(unit)


def test_link_text_is_still_verified_for_every_view(tmp_path, frozen_reads):
    registry, unit, _ = frozen_pool(tmp_path, aliases=2)
    registry.for_view(unit)
    alias = registry.root / "helpers/alias_0.py"
    alias.unlink()
    alias.symlink_to("alias_1.py")

    with pytest.raises(InfrastructureError, match="link changed"):
        registry.for_view(unit)
    assert frozen_reads == [(MODULE, len(HELPER))]
