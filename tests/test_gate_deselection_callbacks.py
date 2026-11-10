"""Actual exclusion callbacks conserve membership; candidate provenance is doubled."""

import json

import pytest
from test_gate_profiles import gates as gates
from test_gate_profiles import profile_session as profile_session


@pytest.mark.parametrize("action", ["observe", "notify-remove", "silent-remove", "notify-only"])
def test_core_callback_conserves_partition_and_authenticates_notifications(profile_session, action):
    value = profile_session
    with (value.root / "tests/test_profile.py").open("a") as stream:
        stream.write("def test_core_extra(): Path('extra-ran').touch()\n")
    callback = value.root / "callback.json"
    (value.root / "tests/conftest.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "notifications = []\n"
        "def pytest_deselected(items):\n"
        "    notifications.append([item.nodeid for item in items])\n"
        "    if not any(item.name == 'test_guard' for item in items):\n"
        "        return\n"
        "    session = items[0].session\n"
        "    before = [item.nodeid for item in session.items]\n"
        "    extra = [item for item in session.items if item.name == 'test_core_extra']\n"
        "    assert len(extra) == 1\n"
        f"    if {action!r} in ('notify-remove', 'silent-remove'):\n"
        "        session.items[:] = [item for item in session.items if item not in extra]\n"
        f"    if {action!r} in ('notify-remove', 'notify-only'):\n"
        "        session.config.hook.pytest_deselected(items=extra)\n"
        "    observed = {'before': before, 'after': [item.nodeid for item in session.items],\n"
        "                'notifications': notifications}\n"
        f"    Path({str(callback)!r}).write_text(json.dumps(observed))\n"
    )
    status = value.run("core")
    receipt = json.loads(value.membership.read_text()) if value.membership.exists() else None
    observed = {
        "action": action,
        "status": status,
        "callback": json.loads(callback.read_text()) if callback.exists() else None,
        "bodies": {
            name: (value.root / (name + "-ran")).exists() for name in ("core", "extra", "guard")
        },
        "receipt": receipt,
    }
    print("CORE_CALLBACK_OBSERVATION " + json.dumps(observed, sort_keys=True))
    core, extra, guard = [
        "tests/test_profile.py::test_" + name for name in ("core", "core_extra", "guard")
    ]
    assert observed["callback"] is not None, observed
    assert observed["callback"]["before"] == [core, extra], observed
    assert observed["callback"]["after"] == (
        [core] if action in {"notify-remove", "silent-remove"} else [core, extra]
    ), observed
    assert observed["callback"]["notifications"] == (
        [[guard], [extra]] if action in {"notify-remove", "notify-only"} else [[guard]]
    ), observed
    if action == "observe":
        assert status == 0, observed
        assert observed["bodies"] == {"core": True, "extra": True, "guard": False}, observed
        assert receipt == {
            "schema": 1,
            "profile": "core",
            "pytest_version": "9.2.0",
            "selected": [core, extra],
            "deselected": [guard],
        }, observed
    else:
        assert status == pytest.ExitCode.USAGE_ERROR, observed
        assert not any(observed["bodies"].values()), observed
        assert receipt is None, observed
