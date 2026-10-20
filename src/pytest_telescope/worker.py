"""Collection and per-case execution inside the prepared restricted runtime."""

import hashlib
import json
import os
import re
import resource
import sys
import time
from pathlib import Path

import pytest

MAX_INVENTORY = 67_108_864


def reap_completed_children():
    # PID1 adopts orphaned children. Reap only those already exited after all teardown.
    # Both bounds prevent a concurrently forking live child from extending this phase.
    reap_deadline = time.monotonic() + 0.05
    for _ in range(4096):
        if time.monotonic() >= reap_deadline:
            break
        try:
            child, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if child == 0:
            break


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(value):
    return hashlib.sha256(value.encode()).hexdigest()


def project_file(filename):
    path = Path(os.path.abspath(Path("/project") / filename)).relative_to(Path("/project"))
    text = path.as_posix()
    if (
        not text
        or len(text.encode()) > 4096
        or len(path.parts) > 64
        or any(ord(char) < 32 for char in text)
        or "\\" in text
    ):
        raise ValueError("invalid project path")
    return text


def main():
    started = time.monotonic()
    request_path = os.environ.pop("TELESCOPE_REQUEST")
    if request_path != "/telescope-request.json":
        raise ValueError("invalid request location")
    with open(request_path, "rb") as stream:
        encoded = stream.read(MAX_INVENTORY + 1)
    if len(encoded) > MAX_INVENTORY:
        raise ValueError("request limit exceeded")
    request = json.loads(encoded)
    if request.get("metadata"):
        from importlib import metadata

        def normalize(name):
            return re.sub(r"[-_.]+", "-", name).lower()

        installed = {normalize(dist.metadata["Name"]) for dist in metadata.distributions()}
        local = {
            normalize(dist.metadata["Name"]) for dist in metadata.distributions(path=["/metadata"])
        }
        if installed & local:
            print(
                "TELESCOPE_UNSUPPORTED "
                + canonical({"capability": "project-metadata:runtime-conflict"}),
                flush=True,
            )
            return 4
        sys.path.insert(0, "/metadata")
    suite_root = request["suite_root"]

    def case_identity(nodeid):
        return identity(canonical([request["suite"], canonical_nodeid(nodeid)]))

    def canonical_nodeid(value):
        filename, separator, suffix = value.partition("::")
        path = (Path("/project") / suite_root / filename).resolve()
        return project_file(path) + (separator + suffix if separator else "")

    mode = request["mode"]
    if mode not in ("collection", "execution", "batch"):
        raise ValueError("invalid request mode")
    cases, finished, selected_ids, failures = {}, set(), [], []
    origins, positions_cache, source_calls, required_origins = {}, {}, {}, {}
    declaration_error = False
    collection_failed = False

    def invalid(message):
        nonlocal declaration_error
        declaration_error = True
        raise pytest.UsageError(message)

    registry = request["registry"]
    if type(registry) is not list or len(registry) > 100_000:
        raise ValueError("invalid declaration registry")
    registry_ids = set()
    for row in registry:
        if type(row) is not dict or set(row) != {
            "id",
            "file",
            "position",
            "kwargs",
            "kind",
            "owner",
        }:
            raise ValueError("invalid declaration registry row")
        if (
            type(row["id"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", row["id"])
            or row["id"] in registry_ids
            or row["kind"] not in ("scope", "decorator", "parameter")
            or type(row["owner"]) is not list
            or len(row["owner"]) > 128
            or any(type(name) is not str or not name.isidentifier() for name in row["owner"])
            or type(row["position"]) is not list
            or len(row["position"]) != 4
            or any(type(number) is not int or number < 0 for number in row["position"])
        ):
            raise ValueError("invalid declaration registry identity")
        filename = project_file(row["file"])
        if filename != row["file"]:
            raise ValueError("noncanonical declaration path")
        key = (filename, tuple(row["position"]))
        if key in source_calls:
            raise ValueError("duplicate declaration position")
        registry_ids.add(row["id"])
        source_calls[key] = (row["id"], canonical(row["kwargs"]))
        if row["kind"] != "parameter":
            required_origins.setdefault((filename, tuple(row["owner"])), set()).add(row["id"])

    original_marker = pytest.mark.telescope

    def guarded_marker(*args, **kwargs):
        frame = sys._getframe(1)
        code = frame.f_code
        if code not in positions_cache:
            positions_cache[code] = tuple(code.co_positions())
        try:
            position = positions_cache[code][frame.f_lasti // 2]
            origin = source_calls.get((project_file(code.co_filename), position))
            encoded_kwargs = canonical(kwargs)
        except (ValueError, TypeError, IndexError):
            invalid("unsupported dynamic telescope declaration")
        if args or origin is None or origin[1] != encoded_kwargs:
            invalid("unsupported dynamic telescope declaration")
        mark = original_marker(**kwargs)
        # Keep the Mark alive and compare with a separate immutable static encoding.
        origins[id(mark.mark)] = (origin[0], origin[1], mark.mark)
        return mark

    pytest.mark.telescope = guarded_marker

    def evidence(nodeid, phase, filename, line, kind):
        if len(failures) >= 64:
            return
        try:
            path = project_file(filename)
        except (ValueError, OSError):
            path = "<runtime>"
        failures.append(
            {
                "file": path,
                "line": line if type(line) is int and 0 <= line <= 10_000_000 else 0,
                "phase": phase,
                "exception_type": kind
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", kind)
                else "TestError",
                "case_id": case_identity(nodeid),
            }
        )

    class Observer:
        def __init__(self):
            self.captured = {}
            self.selection = None
            self.session = None
            self.checkpoint = False
            self.bytes = 1024

        @pytest.hookimpl(tryfirst=True)
        def pytest_configure(self, config):
            if any(
                getattr(plugin, "__name__", "").split(".")[0] == "xdist"
                for _, plugin in config.pluginmanager.list_name_plugin()
            ):
                invalid("nested distributed scheduling is unsupported")
            config.addinivalue_line(
                "markers", "telescope: literal per-case input and reuse declarations"
            )

        def describe(self, item):
            if not isinstance(item, pytest.Function):
                invalid("unsupported custom collected item")
            try:
                filename = project_file(item.path)
            except (ValueError, OSError):
                invalid("collected item escaped the project namespace")
            nodeid = item.nodeid
            if (
                type(nodeid) is not str
                or not nodeid
                or len(nodeid.encode()) > 4096
                or any(ord(char) < 32 for char in nodeid)
            ):
                invalid("invalid collected case identity")
            declaration_ids = set()
            for mark in item.iter_markers("telescope"):
                origin = origins.get(id(mark))
                try:
                    unchanged = origin is not None and canonical(mark.kwargs) == origin[1]
                except (TypeError, ValueError):
                    unchanged = False
                if mark.args or not unchanged:
                    invalid("unregistered or modified telescope declaration")
                declaration_ids.add(origin[0])
            chain = item.listchain()
            owner = [node.name for node in chain if isinstance(node, pytest.Class)]
            originalname = item.originalname
            if type(originalname) is not str or not originalname.isidentifier():
                invalid("unsupported generated case owner")
            owner.append(originalname)
            for depth in range(len(owner) + 1):
                required = required_origins.get((filename, tuple(owner[:depth])), set())
                if not required.issubset(declaration_ids):
                    invalid("collected case lost an inherited literal declaration")
            aliases = {
                canonical_nodeid(node.nodeid) for node in chain if node.nodeid and node is not item
            }
            aliases.update(
                (
                    filename,
                    canonical_nodeid(nodeid),
                    originalname,
                    canonical_nodeid(item.parent.nodeid) + "::" + originalname,
                )
            )
            if len(aliases) > 128 or any(
                type(value) is not str
                or not value
                or len(value.encode()) > 4096
                or any(ord(char) < 32 for char in value)
                for value in aliases
            ):
                invalid("invalid collected selector aliases")
            return {
                "case_id": case_identity(nodeid),
                "file": filename,
                "origins": sorted(declaration_ids),
                "aliases": sorted({identity(alias) for alias in aliases} | {case_identity(nodeid)}),
                "selected": False,
            }

        def pytest_itemcollected(self, item):
            row = self.describe(item)
            if item.nodeid in self.captured or len(self.captured) >= 100_000:
                invalid("duplicate case identity or inventory limit exceeded")
            self.bytes += len(canonical(row).encode())
            if self.bytes > MAX_INVENTORY - 4096:
                invalid("collection inventory exceeds 64 MiB")
            self.captured[item.nodeid] = (item, row)

        def unchanged(self, session):
            for item, row in self.captured.values():
                if self.describe(item) != row:
                    invalid("collected declaration inventory changed after validation")
            current = tuple(session.items)
            if self.selection is not None and current != self.selection:
                invalid("collected item inventory changed after validation")
            if any(
                item.nodeid not in self.captured or self.captured[item.nodeid][0] is not item
                for item in current
            ):
                invalid("unregistered item appeared after collection")

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_collection_modifyitems(self, session, config, items):
            self.session = session
            yield
            self.unchanged(session)
            if collection_failed:
                return
            if mode == "collection":
                selectors = request["selectors"]
                if type(selectors) is not list or any(
                    type(value) is not str for value in selectors
                ):
                    invalid("invalid case selectors")
                aliases = {alias for _, row in self.captured.values() for alias in row["aliases"]}
                if not request.get("allow_unknown_selectors", False) and any(
                    selector not in aliases for selector in selectors
                ):
                    invalid("selector does not identify a collected case")
                selector_set = set(selectors)
                chosen = [
                    item
                    for item in items
                    if not request.get("selection_disabled", False)
                    and (
                        not selectors
                        or not selector_set.isdisjoint(self.captured[item.nodeid][1]["aliases"])
                    )
                ]
            else:
                file_inventory = sorted(
                    (
                        {"case_id": row["case_id"], "origins": row["origins"]}
                        for _, row in self.captured.values()
                    ),
                    key=lambda row: row["case_id"],
                )
                if identity(canonical(file_inventory)) != request["expected_digest"]:
                    invalid("containing-file inventory differs from discovery")
                requested = request["case_ids"] if mode == "batch" else [request["case_id"]]
                wanted = set(requested)
                chosen = [item for item in items if case_identity(item.nodeid) in wanted]
                if [case_identity(item.nodeid) for item in chosen] != requested:
                    invalid("execution membership or order differs from discovery")
            chosen_set = set(chosen)
            deselected = [item for item in items if item not in chosen_set]
            items[:] = chosen
            if deselected:
                config.hook.pytest_deselected(items=deselected)
            self.selection = tuple(items)
            self.unchanged(session)

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_collection_finish(self, session):
            yield
            self.session = session
            self.unchanged(session)
            self.checkpoint = not collection_failed and self.selection is not None
            if mode != "collection" and self.checkpoint:
                selected_ids[:] = [case_identity(item.nodeid) for item in session.items]

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtestloop(self, session):
            self.unchanged(session)
            yield
            self.unchanged(session)

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            outcome = yield
            report = outcome.get_result()
            if report.failed:
                filename, line = str(item.path), item.location[1] + 1
                kind = "UnexpectedPass"
                if call.excinfo is not None:
                    kind = call.excinfo.type.__name__
                    traceback = call.excinfo.traceback
                    if traceback:
                        filename, line = str(traceback[-1].path), traceback[-1].lineno + 1
                evidence(report.nodeid, report.when, filename, line, kind)

        def pytest_collectreport(self, report):
            nonlocal collection_failed
            if report.failed:
                collection_failed = True
                evidence(
                    report.nodeid, "collect", report.nodeid.split("::")[0], 0, "CollectionError"
                )

        def pytest_runtest_logreport(self, report):
            case = case_identity(report.nodeid)
            previous = cases.get(case)
            if report.failed:
                cases[case] = "failed"
            elif previous != "failed":
                if report.skipped:
                    cases[case] = "skipped"
                elif report.when == "call":
                    cases[case] = "passed"
            if report.when == "teardown":
                finished.add(case)

    observer = Observer()
    arguments = list(request["args"])
    if mode == "collection":
        arguments.append("--collect-only")
    saved_out, saved_err = sys.stdout, sys.stderr
    with open(os.devnull, "w") as sink:
        sys.stdout = sys.stderr = sink
        try:
            exit_code = int(pytest.main(arguments, plugins=[observer]))
            if observer.session is not None:
                observer.unchanged(observer.session)
        except pytest.UsageError:
            declaration_error = True
            exit_code = 4
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err
            pytest.mark.telescope = original_marker
    if declaration_error:
        exit_code = 4
    reap_completed_children()
    leaked = [name for name in os.listdir("/proc") if name.isdecimal() and int(name) != os.getpid()]
    usage = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    common = {
        "nonce": request["nonce"],
        "exit_code": exit_code,
        "leaked_descendants": bool(leaked),
        "worker_seconds": time.monotonic() - started,
        "cpu_seconds": usage.ru_utime + usage.ru_stime + children.ru_utime + children.ru_stime,
        "failures": failures,
    }
    if mode == "collection":
        selected = {item.nodeid: order for order, item in enumerate(observer.selection or ())}
        items = [
            {**row, "selected": nodeid in selected, "order": selected.get(nodeid)}
            for nodeid, (_, row) in sorted(observer.captured.items())
        ]
        result = {
            **common,
            "protocol": 4,
            "kind": "collection",
            "items": items,
            "complete": observer.checkpoint
            and not declaration_error
            and not leaked
            and not collection_failed
            and exit_code in (0, 5),
        }
    else:
        result = {
            **common,
            "protocol": 1,
            "collected": len(selected_ids),
            "case_ids": selected_ids,
            "passed": sum(value == "passed" for value in cases.values()),
            "failed": sum(value == "failed" for value in cases.values()),
            "skipped": sum(value == "skipped" for value in cases.values()),
            "complete": observer.checkpoint
            and not declaration_error
            and not leaked
            and set(selected_ids) == finished
            and len(cases) == len(selected_ids),
        }
    if mode == "batch":
        requested = request["case_ids"]
        result.update(
            protocol=2,
            group_id=request["group_id"],
            case_ids=requested,
            collected=len(requested),
            case_results=[
                dict(case_id=case, outcome=cases.get(case, "not-run"), complete=case in finished)
                for case in requested
            ],
        )
        result["complete"] = result["complete"] and selected_ids == requested
    print("TELESCOPE_RESULT " + canonical(result), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
