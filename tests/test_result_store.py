"""Remote result store behavior, with an in-memory object store at the client seam.

No test here reaches a network: `gs://` URLs resolve to the `Objects` stand-in.
"""

import json
import os
import re
import shutil
import signal
import sys
import threading

import pytest

from pytest_telescope import controller, result_store
from pytest_telescope.model import DeclarationError, UnsupportedCapability, fingerprint
from pytest_telescope.plan import make_plan

URL = "gs://shared-results/team/cache"
NAMESPACE = f"team/cache/execution-v{controller.SCHEMA}/"
NO_LOOKUPS = dict(hits=0, misses=0, invalid=0, failed=0, skipped=0)
NO_UPLOADS = dict(created=0, existing=0, failed=0, skipped=0)


class Objects:
    """An in-memory bucket with create-if-absent, scripted faults and a call log."""

    def __init__(self):
        self.objects, self.calls, self.buckets = {}, [], []
        self.faults = {"read": [], "create": []}
        self.before = {"read": None, "create": None}
        self.after_read = None
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def open(self, bucket):
        self.buckets.append(bucket)
        return self

    def _enter(self, operation, name):
        with self.lock:
            self.calls.append((operation, name))
            self.active += 1
            self.peak = max(self.peak, self.active)
            fault = self.faults[operation].pop(0) if self.faults[operation] else None
        try:
            if self.before[operation] is not None:
                self.before[operation]()
            if fault is not None:
                raise fault("scripted store fault")
        finally:
            with self.lock:
                self.active -= 1

    def read(self, name, limit):
        self._enter("read", name)
        with self.lock:
            data = self.objects.get(name)
        if self.after_read is not None:
            self.after_read(name)
        return None if data is None else data[: limit + 1]

    def create(self, name, data):
        self._enter("create", name)
        with self.lock:
            if name in self.objects:
                return False
            self.objects[name] = data
            return True

    def count(self, operation):
        return sum(call[0] == operation for call in self.calls)


@pytest.fixture
def store(monkeypatch):
    objects = Objects()
    monkeypatch.setitem(result_store.BACKENDS, "gs", objects.open)
    monkeypatch.setattr(result_store, "RETRY_DELAYS", (0, 0))
    return objects


def local_records(root):
    return sorted(path.name for path in (root / ".pytest-telescope/cache").glob("*.json"))


def test_remote_hit_populates_the_local_cache_and_is_reused(machines, store):
    first = controller.run(machines("first"), result_store=URL)
    assert first["status"] == "passed" and first["counts"]["executed"] == 3
    assert first["result_store"] == dict(
        backend="gs",
        state="available",
        reason=None,
        reused_groups=dict(local=0, remote=0),
        lookups=NO_LOOKUPS | dict(misses=3),
        uploads=NO_UPLOADS | dict(created=3),
        retries=0,
    )
    assert store.buckets == ["shared-results"]
    assert all(
        re.fullmatch(re.escape(NAMESPACE) + r"[0-9a-f]{64}\.json", name) for name in store.objects
    )
    assert len(store.objects) == 3 and len(machines.executed) == 3

    second = machines("second")
    assert not (second / ".pytest-telescope/cache").exists()
    uploaded = dict(store.objects)
    fetched = controller.run(second, result_store=URL)
    assert fetched["status"] == "passed"
    assert fetched["counts"]["reused"] == 3 and fetched["counts"]["executed"] == 0
    assert fetched["case_outcomes"]["cached_passed"] == 3
    assert all(
        group["outcome"] == "reused" and group["attempts"] == 0 and group["cache_eligible"]
        for group in fetched["groups"]
    )
    assert len(machines.executed) == 3, "a remote hit starts no sandbox"
    assert fetched["result_store"]["reused_groups"] == dict(local=0, remote=3)
    assert fetched["result_store"]["lookups"] == NO_LOOKUPS | dict(hits=3)
    assert fetched["result_store"]["uploads"] == NO_UPLOADS
    assert store.count("create") == 3 and store.objects == uploaded

    # The fetched records are now ordinary local records under the same names.
    assert local_records(second) == sorted(name.rsplit("/", 1)[1] for name in store.objects)
    calls = len(store.calls)
    offline = controller.run(second)
    assert offline["counts"]["reused"] == 3 and "result_store" not in offline
    local = controller.run(second, result_store=URL)
    assert local["result_store"]["reused_groups"] == dict(local=3, remote=0)
    assert local["result_store"]["lookups"] == NO_LOOKUPS
    assert len(store.calls) == calls and len(machines.executed) == 3


def reseal(record):
    record["checksum"] = fingerprint(record["result"])
    return record


def corrupt(data):
    return data[: len(data) // 2]


def stale_checksum(data):
    record = json.loads(data)
    record["result"]["worker_seconds"] += 1
    return json.dumps(record).encode()


def other_schema(data):
    record = json.loads(data)
    record["schema"] += 1
    return json.dumps(record).encode()


def failed_session(data):
    record = json.loads(data)
    record["result"]["exit_code"] = 1
    return json.dumps(reseal(record)).encode()


def oversized(data):
    return data + b" " * 1_048_576


def unknown_field(data):
    record = json.loads(data)
    record["result"]["cpu_seconds"] = 0.1
    return json.dumps(reseal(record)).encode()


@pytest.mark.parametrize(
    "damage", [corrupt, stale_checksum, other_schema, failed_session, oversized, unknown_field]
)
def test_invalid_remote_record_is_a_miss_and_never_a_pass(machines, store, damage):
    controller.run(machines("first", count=1), result_store=URL)
    (name,) = store.objects
    store.objects[name] = damaged = damage(store.objects[name])

    report = controller.run(machines("second", count=1), result_store=URL)
    assert report["status"] == "passed"
    assert report["counts"]["executed"] == 1 and report["counts"]["reused"] == 0
    assert len(machines.executed) == 2, "the case ran again instead of trusting the record"
    assert report["result_store"]["state"] == "available"
    assert report["result_store"]["lookups"] == NO_LOOKUPS | dict(invalid=1)
    assert report["result_store"]["reused_groups"] == dict(local=0, remote=0)
    # Create-if-absent never replaces what the store already holds.
    assert report["result_store"]["uploads"] == NO_UPLOADS | dict(existing=1)
    assert store.objects[name] == damaged


def compact(data):
    return json.dumps(json.loads(data), separators=(",", ":")).encode()


def compact_unicode_near_the_limit(data):
    record = json.loads(data)
    skip = {
        "file": "tests/données" * 4,
        "reason": {"profile": "pytest-reason-sha256-v1", "kind": "skip", "sha256": "c" * 64},
    }
    record["result"]["collection_skips"] = [skip] * 4800
    reshaped = json.dumps(reseal(record), separators=(",", ":"), ensure_ascii=False).encode()
    # Within the record size limit as stored, beyond it as this cache would write it.
    assert len(reshaped) <= 1_048_576 < len(json.dumps(record, sort_keys=True).encode())
    return reshaped


@pytest.mark.parametrize("reshape", [compact, compact_unicode_near_the_limit])
def test_valid_record_in_another_encoding_is_a_miss(machines, store, reshape):
    # The same valid result, serialized differently. A fetched record enters the local
    # cache byte for byte as this cache writes it, so any other bytes are a miss.
    controller.run(machines("first", count=1), result_store=URL)
    (name,) = store.objects
    store.objects[name] = reshaped = reshape(store.objects[name])
    assert controller.parse_record(reshaped, name.rsplit("/", 1)[1][:-5], kind="execution")

    report = controller.run(machines("second", count=1), result_store=URL)
    assert report["status"] == "passed", report.get("collection", {}).get("reason")
    assert report["counts"]["executed"] == 1 and report["counts"]["reused"] == 0
    assert report["result_store"]["lookups"] == NO_LOOKUPS | dict(invalid=1)
    assert report["result_store"]["uploads"] == NO_UPLOADS | dict(existing=1)
    assert store.objects[name] == reshaped


def test_record_of_another_key_is_a_miss_even_when_relabelled(machines, store):
    controller.run(machines("first", count=2), result_store=URL)
    one, two = sorted(store.objects)
    swapped = json.loads(store.objects[two])
    # A valid record stored under another key's name, and the same record with its key
    # field rewritten to match that name: its result still belongs to the other key.
    relabelled = dict(swapped, key=one.rsplit("/", 1)[1].removesuffix(".json"))
    store.objects[one], store.objects[two] = (
        json.dumps(relabelled).encode(),
        store.objects[one],
    )

    report = controller.run(machines("second", count=2), result_store=URL)
    assert report["status"] == "passed" and report["counts"]["executed"] == 2
    assert report["result_store"]["lookups"] == NO_LOOKUPS | dict(invalid=2)
    assert len(machines.executed) == 4


def test_only_locally_published_records_are_uploaded(machines, store):
    # The invocation's cleanup fails after every case passed: nothing is published
    # locally, so nothing may be uploaded either.
    root = machines("first", fail_outer=True)
    report = controller.run(root, result_store=URL)
    assert report["status"] == "infrastructure_error"
    assert report["counts"]["executed"] == 3 and len(machines.executed) == 3
    assert not local_records(root)
    assert store.count("create") == 0 and not store.objects
    assert report["result_store"]["uploads"] == NO_UPLOADS
    assert report["result_store"]["lookups"] == NO_LOOKUPS | dict(misses=3)


def test_local_records_changed_before_upload_are_not_uploaded(machines, store):
    root = machines("first")
    cache = root / ".pytest-telescope/cache"

    def change_the_other_records():
        # Another process changes the local cache while the first record uploads.
        store.before["create"] = None
        uploading = store.calls[-1][1].rsplit("/", 1)[1]
        truncated, relabelled = sorted(p for p in cache.glob("*.json") if p.name != uploading)
        truncated.write_bytes(truncated.read_bytes()[:-1])
        relabelled.write_bytes((cache / uploading).read_bytes())

    store.before["create"] = change_the_other_records
    report = controller.run(root, result_store=URL)
    assert report["status"] == "passed" and report["counts"]["executed"] == 3
    assert report["result_store"]["state"] == "available"
    assert report["result_store"]["uploads"] == NO_UPLOADS | dict(created=1, skipped=2)
    assert store.count("create") == 1 and len(store.objects) == 1


def test_interrupted_uploads_keep_the_finished_run(machines, store, monkeypatch, capsys):
    root = machines("first")

    def interrupt():
        store.before["create"] = None
        os.kill(os.getpid(), signal.SIGINT)

    store.before["create"] = interrupt
    monkeypatch.chdir(root)
    assert controller.main(["run", "--result-store", URL]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "passed" and report["counts"]["executed"] == 3
    assert report["result_store"]["state"] == "degraded"
    assert report["result_store"]["reason"] == "cancelled"
    # The upload in flight finished; the two not yet started were skipped.
    assert report["result_store"]["uploads"] == NO_UPLOADS | dict(created=1, skipped=2)
    assert len(store.objects) == 1
    assert controller.run(root)["counts"]["reused"] == 3


def test_create_if_absent_race_keeps_the_first_record(machines, store):
    controller.run(machines("first", count=1), result_store=URL)
    (name,) = store.objects
    first_record = store.objects.pop(name)

    def another_writer_wins(looked_up):
        # The other machine uploads between this machine's lookup and its own upload.
        store.objects[looked_up] = first_record
        store.after_read = None

    store.after_read = another_writer_wins
    report = controller.run(machines("second", count=1), result_store=URL)
    assert report["status"] == "passed" and report["counts"]["executed"] == 1
    assert report["result_store"]["lookups"] == NO_LOOKUPS | dict(misses=1)
    assert report["result_store"]["uploads"] == NO_UPLOADS | dict(existing=1)
    assert report["result_store"]["state"] == "available"
    assert store.objects == {name: first_record}


def test_transient_failures_are_retried_within_the_budget(machines, store):
    transient = result_store.TransientStoreError
    store.faults["read"] = [transient, transient]
    store.faults["create"] = [transient]
    first = controller.run(machines("first", count=1), result_store=URL)
    assert first["status"] == "passed"
    assert first["result_store"]["state"] == "available"
    assert first["result_store"]["reason"] is None
    assert first["result_store"]["retries"] == 3
    assert first["result_store"]["lookups"] == NO_LOOKUPS | dict(misses=1)
    assert first["result_store"]["uploads"] == NO_UPLOADS | dict(created=1)
    assert store.count("read") == 3 and store.count("create") == 2

    store.faults["read"] = [transient, transient]
    second = controller.run(machines("second", count=1), result_store=URL)
    assert second["counts"]["reused"] == 1
    assert second["result_store"]["lookups"] == NO_LOOKUPS | dict(hits=1)
    assert second["result_store"]["retries"] == 2


def test_exhausted_lookup_retries_degrade_to_local_only(machines, store):
    store.faults["read"] = [result_store.TransientStoreError] * 100
    root = machines("first")
    report = controller.run(root, result_store=URL)
    # The run itself is unaffected: every case executed and passed.
    assert report["status"] == "passed" and report["counts"]["executed"] == 3
    assert report["counts"]["infrastructure_error"] == 0
    assert report["result_store"] == dict(
        backend="gs",
        state="degraded",
        reason="transient",
        reused_groups=dict(local=0, remote=0),
        lookups=NO_LOOKUPS | dict(failed=1, skipped=2),
        uploads=NO_UPLOADS | dict(skipped=3),
        retries=2,
    )
    # Three attempts for the first lookup, then no remote request at all.
    assert store.calls == [("read", store.calls[0][1])] * 3 and not store.objects
    # Local caching continued.
    assert len(local_records(root)) == 3
    assert controller.run(root)["counts"]["reused"] == 3


def test_exhausted_upload_retries_degrade_and_are_counted(machines, store):
    store.faults["create"] = [result_store.TransientStoreError] * 100
    root = machines("first")
    report = controller.run(root, result_store=URL)
    assert report["status"] == "passed" and report["counts"]["executed"] == 3
    assert report["result_store"]["state"] == "degraded"
    assert report["result_store"]["reason"] == "transient"
    assert report["result_store"]["lookups"] == NO_LOOKUPS | dict(misses=3)
    assert report["result_store"]["uploads"] == NO_UPLOADS | dict(failed=1, skipped=2)
    assert report["result_store"]["retries"] == 2
    assert store.count("create") == 3 and not store.objects
    assert controller.run(root)["counts"]["reused"] == 3


def test_rejected_request_degrades_without_retrying(machines, store):
    store.faults["read"] = [result_store.StoreError]
    report = controller.run(machines("first"), result_store=URL)
    assert report["status"] == "passed" and report["counts"]["executed"] == 3
    assert report["result_store"]["state"] == "degraded"
    assert report["result_store"]["reason"] == "rejected"
    assert report["result_store"]["lookups"] == NO_LOOKUPS | dict(failed=1, skipped=2)
    assert report["result_store"]["uploads"] == NO_UPLOADS | dict(skipped=3)
    assert report["result_store"]["retries"] == 0 and len(store.calls) == 1


def test_no_remote_io_without_a_configured_store(machines, store):
    root = machines("first")
    cold = controller.run(root)
    warm = controller.run(root)
    assert cold["counts"]["executed"] == 3 and warm["counts"]["reused"] == 3
    assert "result_store" not in cold and "result_store" not in warm
    assert not store.buckets and not store.calls and not store.objects


def test_no_cache_and_force_limit_remote_io(machines, store):
    root = machines("first")
    bypassed = controller.run(root, result_store=URL, no_cache=True)
    assert bypassed["counts"]["executed"] == 3 and not store.calls
    assert bypassed["result_store"] == dict(
        backend="gs",
        state="available",
        reason=None,
        reused_groups=dict(local=0, remote=0),
        lookups=NO_LOOKUPS,
        uploads=NO_UPLOADS,
        retries=0,
    )
    controller.run(root, result_store=URL)
    assert len(store.objects) == 3
    calls = len(store.calls)
    forced = controller.run(root, result_store=URL, force=True)
    # Forced execution asks neither cache; its uploads find the earlier records.
    assert forced["counts"]["executed"] == 3
    assert forced["result_store"]["lookups"] == NO_LOOKUPS
    assert forced["result_store"]["uploads"] == NO_UPLOADS | dict(existing=3)
    assert [operation for operation, _ in store.calls[calls:]] == ["create"] * 3


def test_reuse_counts_separate_local_from_remote_groups(machines, store):
    controller.run(machines("first"), result_store=URL)
    second = machines("second")
    missing = sorted(store.objects)[0]
    del store.objects[missing]
    mixed = controller.run(second, result_store=URL)
    assert mixed["counts"]["reused"] == 2 and mixed["counts"]["executed"] == 1
    assert mixed["result_store"]["reused_groups"] == dict(local=0, remote=2)
    assert mixed["result_store"]["lookups"] == NO_LOOKUPS | dict(hits=2, misses=1)
    assert mixed["result_store"]["uploads"] == NO_UPLOADS | dict(created=1)

    # One local record is lost; the store supplies it while the others stay local.
    (second / ".pytest-telescope/cache" / missing.rsplit("/", 1)[1]).unlink()
    again = controller.run(second, result_store=URL)
    assert again["counts"]["reused"] == 3
    assert again["result_store"]["reused_groups"] == dict(local=2, remote=1)
    assert again["result_store"]["lookups"] == NO_LOOKUPS | dict(hits=1)
    assert sum(group["outcome"] == "reused" for group in again["groups"]) == 3


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_lookups_and_uploads_use_the_bounded_worker_pool(machines, store, workers):
    barrier = threading.Barrier(workers, timeout=10)
    store.before["read"] = store.before["create"] = barrier.wait
    first = controller.run(machines("first", count=4), result_store=URL, workers=workers)
    # Each barrier releases only when `workers` requests are in flight together.
    assert first["result_store"]["uploads"] == NO_UPLOADS | dict(created=4)
    assert store.peak == workers and store.count("read") == store.count("create") == 4

    store.peak = 0
    second = controller.run(machines("second", count=4), result_store=URL, workers=workers)
    assert second["counts"]["reused"] == 4 and store.peak == workers
    assert second["result_store"]["reused_groups"] == dict(local=0, remote=4)


def test_missing_gcs_client_is_a_typed_declaration_error(machines, monkeypatch, capsys):
    root = machines("first")
    monkeypatch.setitem(sys.modules, "google.cloud.storage", None)
    with pytest.raises(UnsupportedCapability) as refusal:
        controller.run(root, result_store=URL)
    assert isinstance(refusal.value, DeclarationError)
    assert refusal.value.capability == "result-store:gcs"
    assert "pytest-telescope[gcs]" in str(refusal.value)
    assert refusal.value.accounting is None and not machines.executed
    assert not (root / ".pytest-telescope").exists()

    monkeypatch.chdir(root)
    assert controller.main(["run", "--result-store", URL]) == 4
    printed = json.loads(capsys.readouterr().out)
    assert printed["kind"] == "refusal" and printed["status"] == "unsupported"
    assert printed["capability"] == "result-store:gcs"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "shared-results/team",
        "gs://",
        "gs:///team",
        "gs://Shared/team",
        "gs://shared-results/team//cache",
        "gs://shared-results/../other",
        "gs://shared-results/team?generation=1",
        "gs://shared-results/team cache",
        "gs://shared-results/" + "a" * 513,
        5,
        True,
        b"gs://shared-results",
        ["gs://shared-results"],
    ],
)
def test_malformed_store_url_is_a_declaration_error(machines, store, url):
    root = machines("first")
    with pytest.raises(DeclarationError) as refusal:
        controller.run(root, result_store=url)
    assert not isinstance(refusal.value, UnsupportedCapability)
    assert not machines.executed and not store.buckets


def test_unsupported_store_backend_is_refused(machines, store):
    with pytest.raises(UnsupportedCapability) as refusal:
        controller.run(machines("first"), result_store="s3://shared-results/team")
    assert refusal.value.capability == "result-store:backend"
    assert not machines.executed and not store.buckets


def test_store_is_configured_by_key_and_overridden_by_argument_and_flag(
    machines, store, monkeypatch, capsys
):
    root = machines("first", count=1)
    (root / "pyproject.toml").write_text(
        '[tool.pytest_telescope.execution]\nresult_store = "gs://configured-results"\n'
    )
    configured = controller.run(root)
    assert configured["result_store"]["uploads"] == NO_UPLOADS | dict(created=1)
    assert store.buckets == ["configured-results"]
    # Without a prefix, records sit directly below the namespace.
    assert all(name.startswith(f"execution-v{controller.SCHEMA}/") for name in store.objects)

    controller.run(root, result_store=URL)
    assert store.buckets[-1] == "shared-results"
    monkeypatch.chdir(root)
    assert controller.main(["run", "--result-store", "gs://flag-results/nightly/"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert store.buckets[-1] == "flag-results" and printed["result_store"]["backend"] == "gs"

    (root / "pyproject.toml").write_text(
        '[tool.pytest_telescope.execution]\nresult_store = "results"\n'
    )
    with pytest.raises(DeclarationError):
        make_plan(root)
    with pytest.raises(DeclarationError):
        controller.run(root)


# Real Bubblewrap from here on: required controls that fail rather than skip without it.


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        '[tool.pytest_telescope.bundles.unit]\ninputs = ["data.json"]\n'
    )
    (root / "data.json").write_text('{"value": 7}')
    (root / "test_unit.py").write_text(
        "import json\nfrom pathlib import Path\nimport pytest\n"
        'pytestmark = pytest.mark.telescope(bundles=["unit"])\n'
        'def test_value():\n    assert json.loads(Path("data.json").read_text())["value"] == 7\n'
        "def test_other():\n    assert True\n"
    )
    return root


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_identical_checkout_elsewhere_reuses_real_results_from_the_store(
    checkout, tmp_path, store, mode
):
    elsewhere = tmp_path / "elsewhere" / "copy"
    shutil.copytree(checkout, elsewhere)
    cold = controller.run(checkout, result_store=URL, execution_mode=mode)
    assert cold["status"] == "passed" and cold["counts"]["executed"] == 2, cold
    groups = 2 if mode == "case" else 1
    assert cold["result_store"]["uploads"] == NO_UPLOADS | dict(created=groups)

    warm = controller.run(elsewhere, result_store=URL, execution_mode=mode)
    assert warm["status"] == "passed", warm
    assert warm["counts"]["reused"] == 2 and warm["counts"]["executed"] == 0
    assert warm["result_store"]["reused_groups"] == dict(local=0, remote=groups)
    assert all(group["attempts"] == 0 for group in warm["groups"])
    assert [unit["reports"] for unit in warm["units"]] == [
        unit["reports"] for unit in cold["units"]
    ]
    assert controller.run(elsewhere, execution_mode=mode)["counts"]["reused"] == 2

    # A changed input is a different key: the store has nothing for it.
    (elsewhere / "data.json").write_text('{"value": 7, "more": true}')
    changed = controller.run(elsewhere, result_store=URL, execution_mode=mode)
    assert changed["counts"]["executed"] == 2 and changed["counts"]["reused"] == 0
    assert changed["result_store"]["lookups"] == NO_LOOKUPS | dict(misses=groups)


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_failing_and_uncached_groups_are_never_uploaded(checkout, store, mode):
    (checkout / "test_unit.py").write_text(
        (checkout / "test_unit.py").read_text().replace("assert True", "assert False")
    )
    (checkout / "test_fresh.py").write_text(
        "import pytest\npytestmark = pytest.mark.telescope(inputs=[], cache=False)\n"
        "def test_fresh():\n    assert True\n"
    )
    report = controller.run(checkout, result_store=URL, execution_mode=mode)
    assert report["status"] == "failed" and report["cases"]["failed"] == 1
    passed = [unit for unit in report["units"] if unit["result_status"] == "passed"]
    assert len(passed) == 2
    if mode == "case":
        # Only the passing cacheable case of the failing module is stored.
        assert report["result_store"]["uploads"] == NO_UPLOADS | dict(created=1)
        assert report["result_store"]["lookups"] == NO_LOOKUPS | dict(misses=2)
        (name,) = store.objects
        assert json.loads(store.objects[name])["result"]["exit_code"] == 0
    else:
        # The batch failed as a whole; its passing member is not extracted.
        assert report["result_store"]["uploads"] == NO_UPLOADS and not store.objects
        assert report["result_store"]["lookups"] == NO_LOOKUPS | dict(misses=1)
    # The uncached case was never looked up or uploaded.
    assert store.count("read") == (2 if mode == "case" else 1)

    again = controller.run(checkout, result_store=URL, execution_mode=mode)
    assert again["status"] == "failed" and again["cases"]["failed"] == 1
    assert again["result_store"]["reused_groups"] == dict(local=int(mode == "case"), remote=0)


@pytest.mark.requires_fixture_guard
def test_guarded_batch_records_are_shared_and_validated_as_guarded(checkout, tmp_path, store):
    (checkout / "pyproject.toml").write_text(
        (checkout / "pyproject.toml").read_text()
        + '[tool.pytest_telescope.execution]\nmode = "batch"\n'
    )
    elsewhere = tmp_path / "elsewhere" / "copy"
    shutil.copytree(checkout, elsewhere)
    cold = controller.run(checkout, result_store=URL, fixture_guard="available-v1")
    assert cold["schema"] == 5 and cold["status"] == "passed", cold
    assert cold["fixture_guard"]["state"] == "admitted"
    assert cold["result_store"]["uploads"] == NO_UPLOADS | dict(created=1)

    warm = controller.run(elsewhere, result_store=URL, fixture_guard="available-v1")
    assert warm["schema"] == 5 and warm["status"] == "passed", warm
    assert warm["fixture_guard"]["state"] == "admitted"
    assert warm["counts"]["reused"] == 2 and warm["counts"]["executed"] == 0
    assert warm["result_store"]["reused_groups"] == dict(local=0, remote=1)

    # An unguarded run has another key and never accepts the guarded record.
    plain = tmp_path / "plain" / "copy"
    shutil.copytree(checkout, plain, ignore=shutil.ignore_patterns(".pytest-telescope"))
    unguarded = controller.run(plain, result_store=URL)
    assert unguarded["schema"] == 4 and unguarded["counts"]["executed"] == 2
    assert unguarded["result_store"]["reused_groups"] == dict(local=0, remote=0)
    assert unguarded["result_store"]["lookups"] == NO_LOOKUPS | dict(misses=1)
