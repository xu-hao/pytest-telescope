"""Collection and execution inside a Bubblewrap view, or one ordinary pytest session.

This module is loaded by path in its sessions and imports only the standard library and
pytest, so it never adds pytest-telescope's package directory to a session's import path.
"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

MAX_INVENTORY = 67_108_864
REQUEST_PATH = "/telescope/request.json"
SCOPE_FIELDS = frozenset({"inputs", "bundles", "distributions", "env"})
PROJECT_ROOT = Path("/project")


FRAMEWORK_SOURCE_IDENTITIES = {
    "9.1.1": (
        (
            "_pytest.doctest",
            "04261f5b3ddce0c36584f2be42e2938d5a0dbe914414502533a93e8c142dacf9",
            720,
        ),
        (
            "_pytest.fixtures",
            "f5d4d3f848d97411e456d50400d3aaecc19a9b0c90def9151e395ee8952042b7",
            1563,
        ),
        (
            "_pytest.junitxml",
            "557b8978c66b8010fafc50ada4bf56333f9927f31b2b5909fe4c1a9df1a3c0f0",
            335,
        ),
        ("_pytest.tmpdir", "5099f71d89413191c9e7515c9d4fe9fc7083bb73306f2cf1f73a9f89c862ed90", 275),
        (
            "_pytest.legacypath",
            "fe5eaff1a90d31f4dce53023bdb73a33efedd79ee9f5013afb5d7c58cd0346df",
            297,
        ),
    ),
    "9.2.0": (
        (
            "_pytest.doctest",
            "20d8d03d1efbf143b4094eab9cf4f2551c40a4ccce53e3e37c8eb626569d2e00",
            737,
        ),
        (
            "_pytest.fixtures",
            "3847b9bbb05ba2408c3e9e270276ee86a70c1f0d1d420ad14b37b06093a9a330",
            1644,
        ),
        (
            "_pytest.junitxml",
            "3f3b32bac64d387060d513844db6b70b1b43c5391bfe0cc819cf062d55bc4966",
            382,
        ),
        ("_pytest.tmpdir", "5834124a28502a4cbea5a98370c32eb023359163a42b491a7d7ef46c2dec459a", 313),
        (
            "_pytest.legacypath",
            "16ddbb331699b4f81ec7c0ef38ea5a53a691250dfcf5996a3398990a8a0c4966",
            297,
        ),
    ),
}


def reap_completed_children():
    # The sandboxed worker is PID 1 and adopts orphaned children. Reap only those
    # already exited after all teardown.
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


def skip_reason(report):
    """Digest the actual final report without rendering arbitrary objects."""
    if hasattr(report, "wasxfail"):
        reason, kind = report.wasxfail, "xfail"
    else:
        reason, kind = report.longrepr, "skip"
        if type(reason) is tuple:
            if len(reason) != 3 or type(reason[0]) is not str or type(reason[1]) is not int:
                raise ValueError("unsupported skip evidence")
            reason = reason[2]
    if type(reason) is not str or len(reason) > 4096:
        raise ValueError("unsupported skip evidence")
    encoded = reason.encode("utf-8")
    if len(encoded) > 4096:
        raise ValueError("unsupported skip evidence")
    return dict(
        profile="pytest-reason-sha256-v1", kind=kind, sha256=hashlib.sha256(encoded).hexdigest()
    )


def project_file(filename):
    path = Path(os.path.abspath(PROJECT_ROOT / filename)).relative_to(PROJECT_ROOT)
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


def read_request(path):
    with open(path, "rb") as stream:
        encoded = stream.read(MAX_INVENTORY + 1)
    if len(encoded) > MAX_INVENTORY:
        raise ValueError("request limit exceeded")
    return json.loads(encoded)


def ordinary_main(path):
    """Entry for one ordinary suite session on the host, without a sandbox."""
    return main(read_request(path), ordinary=True)


def available_fixture_definitions(session, items):
    """Read the admitted pytest layout without relying on a fixture's static closure.

    This is only the layout adapter. Its result remains local to the worker and
    is not evidence of a qualified provenance table or an admitted group.
    """
    from _pytest.fixtures import FixtureDef, FixtureManager
    from _pytest.nodes import Node
    from _pytest.python import DirectParamFixtureDef, get_direct_param_fixture_func

    manager = session._fixturemanager
    if type(manager) is not FixtureManager or pytest.__version__ not in ("9.1.1", "9.2.0"):
        raise ValueError("unsupported fixture layout")
    registered = {}
    registry_entries = []
    count = 0

    def register(name, definitions):
        nonlocal count
        if (
            type(name) is not str
            or not name
            or len(name.encode("utf-8")) > 4096
            or type(definitions) is not list
            or len(definitions) > 4096
        ):
            raise ValueError("invalid fixture registry")
        for definition in definitions:
            if (
                type(definition) is not FixtureDef
                or definition.argname != name
                or definition.scope not in ("function", "class", "module", "package", "session")
                or not callable(definition.func)
            ):
                raise ValueError("invalid fixture definition")
            count += 1
            if count > 4096:
                raise ValueError("fixture registry limit exceeded")
            registered.setdefault(name, set()).add(id(definition))
            registry_entries.append(definition)

    if pytest.__version__ == "9.1.1":
        registry = manager._arg2fixturedefs
        if type(registry) is not dict or len(registry) > 4096:
            raise ValueError("invalid fixture registry")
        for name, definitions in registry.items():
            register(name, definitions)
    else:
        node_registry = manager._arg2node2fixturedefs
        legacy_registry = manager._arg2nodeid2fixturedefs
        if type(node_registry) is not dict or type(legacy_registry) is not dict:
            raise ValueError("invalid fixture registry")
        if len(set(node_registry) | set(legacy_registry)) > 4096:
            raise ValueError("fixture registry limit exceeded")
        buckets = 0
        for is_node, registry in ((True, node_registry), (False, legacy_registry)):
            for name, mapping in registry.items():
                if type(mapping) is not dict:
                    raise ValueError("invalid fixture registry")
                buckets += len(mapping)
                if buckets > 100_000:
                    raise ValueError("fixture registry limit exceeded")
                for owner, definitions in mapping.items():
                    if is_node:
                        if not isinstance(owner, Node):
                            raise ValueError("invalid fixture owner")
                    elif type(owner) is not str or len(owner.encode("utf-8")) > 4096:
                        raise ValueError("invalid fixture owner")
                    if type(definitions) is not list:
                        raise ValueError("invalid fixture registry")
                    for definition in definitions:
                        if type(definition) is not FixtureDef:
                            raise ValueError("invalid fixture definition")
                        if is_node and not (
                            definition.node is owner
                            or (
                                owner is session
                                and definition.node is None
                                and definition.baseid == ""
                            )
                        ):
                            raise ValueError("invalid fixture owner")
                        if not is_node and definition.baseid != owner:
                            raise ValueError("invalid fixture owner")
                    register(name, definitions)
    associations = 0
    result = []
    for item in items:
        if not isinstance(item, pytest.Function):
            raise ValueError("unsupported collected item")
        definitions = []
        for name in sorted(registered):
            visible = manager.getfixturedefs(name, item)
            if visible is not None and type(visible) not in (list, tuple):
                raise ValueError("invalid fixture lookup")
            for definition in visible or ():
                if type(definition) is not FixtureDef or id(definition) not in registered[name]:
                    raise ValueError("fixture lookup disagrees with registry")
                definitions.append(definition)
                associations += 1
                if associations > 100_000:
                    raise ValueError("fixture association limit exceeded")
        fixture_info = item._fixtureinfo
        direct = fixture_info.name2fixturedefs
        if type(direct) is not dict:
            raise ValueError("invalid item fixture metadata")
        for name, item_definitions in direct.items():
            if type(name) is not str or type(item_definitions) not in (list, tuple):
                raise ValueError("invalid item fixture metadata")
            for definition in item_definitions:
                if id(definition) in registered.get(name, set()):
                    continue
                if (
                    type(definition) is not DirectParamFixtureDef
                    or definition.argname != name
                    or definition.func is not get_direct_param_fixture_func
                    or name not in item.callspec.indices
                    or type(item.callspec.indices[name]) is not int
                    or item.callspec.indices[name] < 0
                    or name not in item.callspec._arg2scope
                ):
                    raise ValueError("unsupported item fixture metadata")
                definitions.append(definition)
                associations += 1
                if associations > 100_000:
                    raise ValueError("fixture association limit exceeded")
        result.append((item, definitions))
    return result, registry_entries


def qualified_framework_identity(session, definition, identities):
    """Verify a candidate against an independently supplied fixed source table.

    A matching name or file alone never grants framework status. This helper
    accepts only the real registered fixture function exposed by the matching
    installed builtin plugin, and returns its fixed source identity.
    """
    import base64
    import inspect
    import types
    from importlib import metadata

    from _pytest.fixtures import FixtureFunctionDefinition

    function = definition.func
    if isinstance(function, types.MethodType):
        function = function.__func__
    if type(function) is not types.FunctionType:
        return None
    if (definition.node is not None and definition.node is not session) or definition.baseid != "":
        return None
    manager = session._fixturemanager
    if pytest.__version__ == "9.1.1":
        registered = manager._arg2fixturedefs.get(definition.argname, ())
    elif pytest.__version__ == "9.2.0":
        registered = [
            entry
            for buckets in (
                manager._arg2node2fixturedefs.get(definition.argname, {}),
                manager._arg2nodeid2fixturedefs.get(definition.argname, {}),
            )
            for values in buckets.values()
            for entry in values
        ]
    else:
        return None
    matching = 0
    for entry in registered:
        other = entry.func
        if isinstance(other, types.MethodType):
            other = other.__func__
        if other is function:
            matching += 1
    # A re-registration is not the original builtin fixture registration, even
    # when it reuses the builtin function object. Ambiguity gets no exemption.
    if matching != 1:
        return None
    code = function.__code__
    module = sys.modules.get(function.__module__)
    if type(module) is not types.ModuleType:
        return None
    entry = (function.__module__, code.co_firstlineno)
    sources = [source for name, source, line in identities if (name, line) == entry]
    if len(sources) != 1:
        return None
    distribution = metadata.distribution("pytest")
    if distribution.version != pytest.__version__:
        raise ValueError("pytest distribution mismatch")
    module_file = Path(module.__file__).resolve(strict=True)
    entries = [
        file
        for file in distribution.files or ()
        if Path(distribution.locate_file(file)).resolve() == module_file
    ]
    if len(entries) != 1 or entries[0].hash is None or entries[0].hash.mode != "sha256":
        raise ValueError("pytest source provenance unavailable")
    with module_file.open("rb") as stream:
        contents = stream.read(MAX_INVENTORY + 1)
    if len(contents) > MAX_INVENTORY:
        raise ValueError("pytest source provenance limit")
    digest = hashlib.sha256(contents).hexdigest()
    record = base64.urlsafe_b64encode(bytes.fromhex(digest)).rstrip(b"=").decode("ascii")
    if digest != sources[0] or record != entries[0].hash.value:
        raise ValueError("pytest source provenance mismatch")
    registered_object = False
    for plugin in session.config.pluginmanager.get_plugins():
        if type(plugin) is types.ModuleType:
            if plugin is not module:
                continue
            namespace = vars(plugin)
        elif isinstance(plugin, type):
            if plugin.__module__ != function.__module__:
                continue
            namespace = vars(plugin)
        else:
            if type(plugin).__module__ != function.__module__:
                continue
            namespace = vars(type(plugin))
        for value in namespace.values():
            if type(value) is staticmethod:
                value = value.__func__
            if type(value) is not FixtureFunctionDefinition:
                continue
            try:
                candidate = inspect.unwrap(value)
            except (ValueError, TypeError):
                continue
            if isinstance(candidate, staticmethod):
                candidate = candidate.__func__
            if isinstance(candidate, types.MethodType):
                candidate = candidate.__func__
            if candidate is function:
                registered_object = True
                break
        if registered_object:
            break
    if not registered_object or definition.scope != "session":
        raise ValueError("builtin fixture provenance changed")
    return identity(
        canonical(
            {
                "pytest": pytest.__version__,
                "module": function.__module__,
                "source": digest,
                "line": code.co_firstlineno,
            }
        )
    )


def fixture_inspection_rows(session, items, case_ids, suite, identities):
    """Construct bounded opaque per-item records from actual available fixtures.

    Callers must still bind and validate the registry, graph and worker channel.
    Unknown source, scope or parameter metadata refuses rather than being guessed.
    """
    import types

    from _pytest.python import Package, _get_first_non_fixture_func, importtestmodule
    from _pytest.scope import Scope

    if len(items) != len(case_ids) or len(items) > 4096:
        raise ValueError("invalid fixture inspection membership")
    if len(set(case_ids)) != len(case_ids) or any(
        type(case) is not str or not re.fullmatch(r"[0-9a-f]{64}", case) for case in case_ids
    ):
        raise ValueError("invalid fixture inspection membership")
    result = []
    source_hashes = {}
    source_bytes = 0
    available, _ = available_fixture_definitions(session, items)
    for (item, definitions), case in zip(available, case_ids):
        fixtures = []
        packages = []
        for parent in item.iter_parents():
            if not isinstance(parent, Package):
                continue
            if type(parent) is not Package:
                raise ValueError("unsupported package collector")
            package_path = project_file(parent.path)
            init_path = parent.path / "__init__.py"
            module = importtestmodule(init_path, session.config)
            lifecycle = []
            for names in (
                ("setUpModule", "setup_module"),
                ("tearDownModule", "teardown_module"),
            ):
                function = _get_first_non_fixture_func(module, names)
                if function is None:
                    lifecycle.append(None)
                    continue
                if type(function) is not types.FunctionType:
                    raise ValueError("unsupported package lifecycle")
                code = function.__code__
                if (
                    type(function.__qualname__) is not str
                    or len(function.__qualname__.encode("utf-8")) > 4096
                ):
                    raise ValueError("invalid package lifecycle identity")
                lifecycle.append(
                    identity(
                        canonical(
                            {
                                "source": project_file(code.co_filename),
                                "line": code.co_firstlineno,
                                "name": function.__qualname__,
                            }
                        )
                    )
                )
            packages.append(
                {
                    "owner_id": identity(
                        canonical({"suite": suite, "scope": "package", "path": package_path})
                    ),
                    "lifecycle": lifecycle,
                }
            )
        packages.sort(key=lambda row: row["owner_id"])
        callspec = item.__dict__.get("callspec")
        if callspec is not None and (
            type(callspec.indices) is not dict or type(callspec._arg2scope) is not dict
        ):
            raise ValueError("invalid fixture parameter metadata")
        for definition in definitions:
            function = definition.func
            if isinstance(function, types.MethodType):
                function = function.__func__
            if type(function) is not types.FunctionType:
                raise ValueError("unsupported fixture function")
            module = sys.modules.get(function.__module__)
            if type(module) is not types.ModuleType or type(module.__file__) is not str:
                raise ValueError("unknown fixture source")
            path = Path(module.__file__).resolve(strict=True)
            if path.suffix != ".py":
                raise ValueError("unsupported fixture source")
            if path not in source_hashes:
                with path.open("rb") as stream:
                    data = stream.read(MAX_INVENTORY + 1)
                if len(data) > MAX_INVENTORY:
                    raise ValueError("fixture source limit exceeded")
                source_bytes += len(data)
                if source_bytes > MAX_INVENTORY:
                    raise ValueError("fixture source limit exceeded")
                source_hashes[path] = hashlib.sha256(data).hexdigest()
            code = function.__code__
            if (
                type(function.__qualname__) is not str
                or len(function.__qualname__.encode("utf-8")) > 4096
                or type(definition.argname) is not str
                or len(definition.argname.encode("utf-8")) > 4096
            ):
                raise ValueError("invalid fixture identity")
            framework = qualified_framework_identity(session, definition, identities)
            definition_id = framework or identity(
                canonical(
                    {
                        "module": function.__module__,
                        "path": str(path),
                        "source": source_hashes[path],
                        "line": code.co_firstlineno,
                        "qualname": function.__qualname__,
                        "argname": definition.argname,
                    }
                )
            )
            index = None
            scope = definition.scope
            if callspec is not None and definition.argname in callspec.indices:
                index = callspec.indices[definition.argname]
                if type(index) is not int or index < 0:
                    raise ValueError("invalid fixture parameter binding")
                effective = callspec._arg2scope.get(definition.argname)
                if not isinstance(effective, Scope):
                    raise ValueError("invalid fixture parameter scope")
                scope = effective.value
            if scope not in ("function", "class", "module", "package", "session"):
                raise ValueError("unsupported fixture scope")
            if scope == "package":
                # The package owner can differ from the apparent filesystem
                # parent. Do not invent an owner until that shape is qualified.
                raise ValueError("unsupported fixture package owner")
            owner = {"suite": suite, "scope": scope}
            if scope == "function":
                owner["case"] = case
            elif scope in ("module", "class"):
                owner["file"] = project_file(item.path)
                if scope == "class":
                    if not isinstance(item.cls, type) or type(item.cls.__qualname__) is not str:
                        raise ValueError("unknown fixture class owner")
                    owner["class"] = item.cls.__qualname__
            fixtures.append(
                {
                    "definition_id": definition_id,
                    "owner_id": identity(canonical(owner)),
                    "scope": definition.scope,
                    "parameter_index": index,
                    "effective_scope": scope,
                    "framework": framework is not None,
                }
            )
        fixtures.sort(
            key=lambda row: (
                row["definition_id"],
                row["owner_id"],
                row["effective_scope"],
                -1 if row["parameter_index"] is None else row["parameter_index"],
                row["scope"],
                row["framework"],
            )
        )
        if len({canonical(row) for row in fixtures}) != len(fixtures):
            raise ValueError("duplicate fixture identity")
        result.append({"case_id": case, "fixtures": fixtures, "packages": packages})
    return result


def fixture_registry_commitment(session, items, identities, observer):
    """Commit the entire fixed registry, including definitions not in closures."""
    import types

    _, definitions = available_fixture_definitions(session, items)
    entries, source_hashes = [], {}
    source_bytes = 0
    for definition in definitions:
        function = definition.func
        if isinstance(function, types.MethodType):
            function = function.__func__
        if type(function) is not types.FunctionType:
            raise ValueError("unsupported fixture function")
        module = sys.modules.get(function.__module__)
        if type(module) is not types.ModuleType or type(module.__file__) is not str:
            raise ValueError("unknown fixture source")
        path = Path(module.__file__).resolve(strict=True)
        if path.suffix != ".py":
            raise ValueError("unsupported fixture source")
        if path not in source_hashes:
            with path.open("rb") as stream:
                data = stream.read(MAX_INVENTORY + 1)
            if len(data) > MAX_INVENTORY:
                raise ValueError("fixture source limit exceeded")
            source_bytes += len(data)
            if source_bytes > MAX_INVENTORY:
                raise ValueError("fixture source limit exceeded")
            source_hashes[path] = hashlib.sha256(data).hexdigest()
        owner = definition.node.nodeid if definition.node is not None else definition.baseid
        if (
            type(owner) is not str
            or len(owner.encode("utf-8")) > 4096
            or "[" in owner
            or "]" in owner
            or type(definition.argname) is not str
            or type(function.__qualname__) is not str
            or len(function.__qualname__.encode("utf-8")) > 4096
        ):
            raise ValueError("unsupported fixture registry identity")
        framework = qualified_framework_identity(session, definition, identities)
        entries.append(
            {
                "name": definition.argname,
                "module": function.__module__,
                "path": str(path),
                "source": source_hashes[path],
                "line": function.__code__.co_firstlineno,
                "qualname": function.__qualname__,
                "owner": owner,
                "scope": definition.scope,
                "framework": framework,
            }
        )
    plugins = []
    for name, plugin in session.config.pluginmanager.list_name_plugin():
        if plugin is observer:
            continue
        if plugin is session.config.pluginmanager:
            # Pluggy registers the manager itself with a process-specific id.
            # Bind its actual object/source, but use a stable reserved identity.
            name = "<pytest-plugin-manager>"
        if type(name) is not str or not name or len(name.encode("utf-8")) > 4096:
            raise ValueError("unsupported plugin registry identity")
        if plugin is None:
            # Pluggy retains explicitly blocked plugin names in its registry.
            plugins.append({"name": name, "kind": "blocked"})
            if len(plugins) > 4096:
                raise ValueError("plugin registry limit exceeded")
            continue
        if isinstance(plugin, types.ModuleType):
            module = plugin
            kind = "module"
        elif isinstance(plugin, type):
            module = sys.modules.get(plugin.__module__)
            kind = "class:" + plugin.__qualname__
        else:
            module = sys.modules.get(type(plugin).__module__)
            kind = "instance:" + type(plugin).__qualname__
        if type(module) is not types.ModuleType or type(module.__file__) is not str:
            raise ValueError("unsupported plugin source")
        path = Path(module.__file__).resolve(strict=True)
        if path.suffix != ".py":
            raise ValueError("unsupported plugin source")
        if path not in source_hashes:
            with path.open("rb") as stream:
                data = stream.read(MAX_INVENTORY + 1)
            if len(data) > MAX_INVENTORY:
                raise ValueError("plugin source limit exceeded")
            source_bytes += len(data)
            if source_bytes > MAX_INVENTORY:
                raise ValueError("plugin source limit exceeded")
            source_hashes[path] = hashlib.sha256(data).hexdigest()
        if type(kind) is not str or len(kind.encode("utf-8")) > 4096:
            raise ValueError("unsupported plugin identity")
        plugins.append(
            {
                "name": name,
                "module": module.__name__,
                "kind": kind,
                "path": str(path),
                "source": source_hashes[path],
            }
        )
        if len(plugins) > 4096:
            raise ValueError("plugin registry limit exceeded")
    plugins.sort(key=lambda entry: entry["name"])
    if len({entry["name"] for entry in plugins}) != len(plugins):
        raise ValueError("duplicate plugin identity")
    # Raw paths, owner strings and names remain inside the worker. The parent
    # receives only the commitment, and validates the separately bound rows.
    return identity(
        canonical({"pytest": pytest.__version__, "plugins": plugins, "entries": entries})
    )


def main(request=None, *, ordinary=False):
    global PROJECT_ROOT
    started = time.monotonic()
    if ordinary:
        PROJECT_ROOT = Path(request["project_root"])
        if not PROJECT_ROOT.is_absolute():
            raise ValueError("ordinary sessions require an absolute project root")
    else:
        if os.environ.pop("TELESCOPE_REQUEST", None) != REQUEST_PATH:
            raise ValueError("invalid request location")
        request = read_request(REQUEST_PATH)
    if request.get("metadata"):
        from importlib import metadata

        def normalize(name):
            return re.sub(r"[-_.]+", "-", name).lower()

        runtime_paths = [path for path in sys.path if path != "/metadata"]
        installed = {
            normalize(dist.metadata["Name"]) for dist in metadata.distributions(path=runtime_paths)
        }
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
        if "/metadata" not in sys.path:
            sys.path.insert(0, "/metadata")
    suite_root = request["suite_root"]

    def case_identity(nodeid):
        return identity(canonical([request["suite"], canonical_nodeid(nodeid)]))

    def canonical_nodeid(value):
        filename, separator, suffix = value.partition("::")
        path = (PROJECT_ROOT / suite_root / filename).resolve()
        return project_file(path) + (separator + suffix if separator else "")

    mode = request["mode"]
    guard_mode = mode in ("guard-capture", "guard-inspection", "guard-batch")
    nonce_length = 64 if guard_mode else 32
    if type(request.get("nonce")) is not str or not re.fullmatch(
        rf"[0-9a-f]{{{nonce_length}}}", request["nonce"]
    ):
        raise ValueError("invalid request nonce")
    if mode == "guard-capture":
        expected_envelope = {
            "protocol": "fixture-capture-v1",
            "nonce": request["nonce"],
            "base_context_id": request["context_id"],
            "case_ids": request["case_ids"],
        }
        if (
            type(request.get("guard_envelope")) is not dict
            or request["guard_envelope"] != expected_envelope
        ):
            raise ValueError("invalid fixture capture envelope")
    elif mode == "guard-inspection":
        expected_envelope = {
            "protocol": "fixture-inspection-v1",
            "nonce": request["nonce"],
            "context_id": request["context_id"],
            "group_id": request["group_id"],
            "case_ids": request["case_ids"],
            "registry_id": request["registry_id"],
        }
        if (
            type(request.get("guard_envelope")) is not dict
            or request["guard_envelope"] != expected_envelope
        ):
            raise ValueError("invalid fixture inspection envelope")
    if guard_mode:
        cases = request.get("case_ids")
        if (
            type(cases) is not list
            or not 0 < len(cases) <= 4096
            or any(
                type(case) is not str or not re.fullmatch(r"[0-9a-f]{64}", case) for case in cases
            )
            or len(set(cases)) != len(cases)
            or request.get("group_id") != request.get("context_id")
            or type(request.get("expected_digest")) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", request["expected_digest"])
        ):
            raise ValueError("invalid guarded membership")
        if mode in ("guard-inspection", "guard-batch") and (
            type(request.get("registry_id")) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", request["registry_id"])
        ):
            raise ValueError("invalid guarded registry")
        if mode == "guard-batch" and (
            request.get("guard_protocol") != "batch-guard-v1"
            or type(request.get("graph_id")) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", request["graph_id"])
        ):
            raise ValueError("invalid guarded execution")
    if type(request.get("context_id")) is not str or not re.fullmatch(
        r"[0-9a-f]{64}", request["context_id"]
    ):
        raise ValueError("invalid request context")
    if (
        mode
        not in (
            "collection",
            "execution",
            "batch",
            "ordinary",
            "guard-capture",
            "guard-inspection",
            "guard-batch",
        )
        or (mode == "ordinary") != ordinary
    ):
        raise ValueError("invalid request mode")
    cases, finished, selected_ids, failures = {}, set(), [], []
    origins, positions_cache, source_calls, required_origins = {}, {}, {}, {}
    declaration_error = False
    collection_failed = False
    phase_reports, collection_skips = {}, []
    failure_reports = {}
    invalid_evidence = False

    def invalid(message):
        nonlocal declaration_error
        declaration_error = True
        raise pytest.UsageError(message)

    registry = request["registry"]
    if type(registry) is not list or len(registry) > 100_000:
        raise ValueError("invalid declaration registry")
    registry_ids, scoped_origins = set(), set()
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
        if SCOPE_FIELDS.intersection(row["kwargs"]):
            scoped_origins.add(row["id"])
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
            # Pytest's rewrite cache can retain co_filename from a moved checkout.
            filename = project_file(frame.f_globals.get("__file__"))
            origin = source_calls.get((filename, position))
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
            self.actual_selection = None
            self.session = None
            self.checkpoint = False
            self.dispatch_started = False
            self.selected_case_ids = set()
            self.bytes = 1024
            self.guard_items = None
            self.guard_registry = None

        @pytest.hookimpl(tryfirst=True)
        def pytest_configure(self, config):
            # Ordinary sessions autoload installed plugins; only active scheduling refuses.
            if (
                ordinary
                and (
                    config.getoption("numprocesses", default=None) not in (None, 0)
                    or config.getoption("dist", default="no") != "no"
                )
            ) or (
                not ordinary
                and any(
                    getattr(plugin, "__name__", "").split(".")[0] == "xdist"
                    for _, plugin in config.pluginmanager.list_name_plugin()
                )
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
            if mode in ("collection", "ordinary"):
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
                requested = (
                    request["case_ids"]
                    if mode in ("batch", "guard-capture", "guard-inspection", "guard-batch")
                    else [request["case_id"]]
                )
                wanted = set(requested)
                chosen = [item for item in items if case_identity(item.nodeid) in wanted]
                if [case_identity(item.nodeid) for item in chosen] != requested:
                    invalid("execution membership or order differs from discovery")
            self.actual_selection = tuple(chosen)
            if ordinary:
                # Scoped cases of mixed files never reach fixture setup on the host.
                chosen = [
                    item
                    for item in chosen
                    if scoped_origins.isdisjoint(self.captured[item.nodeid][1]["origins"])
                ]
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
                self.selected_case_ids = set(selected_ids)
            if mode in ("guard-capture", "guard-inspection", "guard-batch") and self.checkpoint:
                identities = FRAMEWORK_SOURCE_IDENTITIES.get(pytest.__version__)
                if identities is None:
                    invalid("unsupported fixture adapter version")
                try:
                    self.guard_registry = fixture_registry_commitment(
                        session, session.items, identities, self
                    )
                    self.guard_items = fixture_inspection_rows(
                        session, session.items, selected_ids, request["suite"], identities
                    )
                except (AttributeError, KeyError, OSError, TypeError, ValueError, UnicodeError):
                    invalid("fixture metadata cannot be validated")

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtestloop(self, session):
            self.unchanged(session)
            if mode == "guard-batch":
                identities = FRAMEWORK_SOURCE_IDENTITIES.get(pytest.__version__)
                try:
                    current_registry = fixture_registry_commitment(
                        session, session.items, identities, self
                    )
                    current_items = fixture_inspection_rows(
                        session, session.items, selected_ids, request["suite"], identities
                    )
                    current_graph = identity(
                        canonical(
                            {
                                "protocol": "fixture-inspection-v1",
                                "context_id": request["context_id"],
                                "group_id": request["group_id"],
                                "case_ids": request["case_ids"],
                                "registry_id": current_registry,
                                "items": current_items,
                            }
                        )
                    )
                    if (
                        current_registry != request["registry_id"]
                        or current_graph != request["graph_id"]
                    ):
                        invalid("fixture plan changed before dispatch")
                except (AttributeError, KeyError, OSError, TypeError, ValueError, UnicodeError):
                    invalid("fixture plan cannot be revalidated")
            self.dispatch_started = True
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
                failure_reports[case_identity(report.nodeid), report.when] = (filename, line, kind)

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_collectreport(self, report):
            nonlocal collection_failed, invalid_evidence
            yield
            if report.failed:
                collection_failed = True
                evidence(
                    report.nodeid, "collect", report.nodeid.split("::")[0], 0, "CollectionError"
                )
            if report.skipped:
                try:
                    if len(collection_skips) >= 100_000:
                        raise ValueError("collection evidence bound")
                    collection_skips.append(
                        dict(
                            file=canonical_nodeid(report.nodeid).split("::")[0],
                            reason=skip_reason(report),
                        )
                    )
                except (ValueError, TypeError, UnicodeError, OSError):
                    invalid_evidence = True

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_logreport(self, report):
            nonlocal invalid_evidence
            yield
            try:
                case = case_identity(report.nodeid)
                if case not in self.selected_case_ids or report.when not in (
                    "setup",
                    "call",
                    "teardown",
                ):
                    raise ValueError("unrequested phase report")
                reports = phase_reports.setdefault(case, [])
                phases = [row["phase"] for row in reports]
                if (
                    report.when in phases
                    or len(phases) >= 3
                    or (not phases and report.when != "setup")
                    or (phases and report.when == "setup")
                    or ("teardown" in phases)
                    or (report.when == "call" and reports[0]["outcome"] != "passed")
                    or report.outcome not in ("passed", "failed", "skipped")
                ):
                    raise ValueError("inconsistent phase report")
                reports.append(
                    dict(
                        phase=report.when,
                        outcome=report.outcome,
                        skip_reason=skip_reason(report) if report.skipped else None,
                    )
                )
            except (ValueError, TypeError, UnicodeError, AttributeError, OSError):
                invalid_evidence = True
                return
            previous = cases.get(case)
            if report.failed:
                failure = failure_reports.get((case, report.when))
                if failure is None:
                    failure = (report.location[0], report.location[1] + 1, "UnexpectedPass")
                evidence(report.nodeid, report.when, *failure)
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
    if mode in ("collection", "guard-capture", "guard-inspection"):
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
    leaked = []
    if not ordinary:
        # PID 1 of the sandbox: any other process left after teardown is a descendant.
        reap_completed_children()
        leaked = [
            name for name in os.listdir("/proc") if name.isdecimal() and int(name) != os.getpid()
        ]
    if invalid_evidence:
        # No raw report, reason or traceback may cross the worker boundary.
        print("TELESCOPE_EVIDENCE_ERROR invalid observed outcome evidence", flush=True)
        return 3
    common = {
        "nonce": request["nonce"],
        "exit_code": exit_code,
        "leaked_descendants": bool(leaked),
        "worker_seconds": time.monotonic() - started,
        "failures": failures,
        "context_id": request["context_id"],
        "collection_skips": collection_skips,
    }

    if mode in ("guard-capture", "guard-inspection"):
        if (
            exit_code != 0
            or leaked
            or not observer.checkpoint
            or observer.guard_registry is None
            or observer.guard_items is None
        ):
            print(
                "TELESCOPE_UNSUPPORTED " + canonical({"capability": "execution:fixture-metadata"}),
                flush=True,
            )
            return 4 if declaration_error else 3
        if mode == "guard-capture":
            result = {
                "protocol": "fixture-capture-v1",
                "nonce": request["nonce"],
                "base_context_id": request["context_id"],
                "case_ids": request["case_ids"],
                "registry_id": observer.guard_registry,
                "items": observer.guard_items,
            }
            result["content_id"] = identity(
                canonical(
                    {
                        "protocol": "fixture-content-v1",
                        "registry_id": observer.guard_registry,
                        "case_ids": request["case_ids"],
                        "items": observer.guard_items,
                    }
                )
            )
        else:
            if observer.guard_registry != request["registry_id"]:
                print(
                    "TELESCOPE_UNSUPPORTED "
                    + canonical({"capability": "execution:fixture-metadata"}),
                    flush=True,
                )
                return 4
            result = {
                "protocol": "fixture-inspection-v1",
                "nonce": request["nonce"],
                "context_id": request["context_id"],
                "group_id": request["group_id"],
                "case_ids": request["case_ids"],
                "registry_id": observer.guard_registry,
                "items": observer.guard_items,
            }
            result["graph_id"] = identity(
                canonical({key: value for key, value in result.items() if key != "nonce"})
            )
        print("TELESCOPE_RESULT " + canonical(result), flush=True)
        return 0

    def member(case):
        return dict(
            case_id=case,
            outcome=cases.get(case, "not-run"),
            complete=case in finished,
            reports=phase_reports.get(case, []),
        )

    if mode == "collection":
        selected = {item.nodeid: order for order, item in enumerate(observer.selection or ())}
        items = [
            {**row, "selected": nodeid in selected, "order": selected.get(nodeid)}
            for nodeid, (_, row) in sorted(observer.captured.items())
        ]
        result = {
            **common,
            "protocol": 7,
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
            "protocol": 5,
            "collected": len(selected_ids),
            "case_ids": selected_ids,
            "case_results": [member(case) for case in selected_ids],
            "passed": sum(value == "passed" for value in cases.values()),
            "failed": sum(value == "failed" for value in cases.values()),
            "skipped": sum(value == "skipped" for value in cases.values()),
            "complete": observer.checkpoint
            and not declaration_error
            and not leaked
            and set(selected_ids) == finished
            and len(cases) == len(selected_ids),
        }
    if mode in ("batch", "guard-batch"):
        requested = request["case_ids"]
        result.update(
            protocol=6,
            group_id=request["group_id"],
            case_ids=requested,
            collected=len(requested),
            case_results=[member(case) for case in requested],
        )
        result["complete"] = result["complete"] and selected_ids == requested
        if mode == "guard-batch":
            result.update(
                guard_protocol="batch-guard-v1",
                registry_id=request["registry_id"],
                graph_id=request["graph_id"],
            )
    if ordinary:
        selected = {
            item.nodeid: order for order, item in enumerate(observer.actual_selection or ())
        }
        result.update(
            protocol=2,
            kind="ordinary",
            dispatch_started=observer.dispatch_started,
            declaration_error=declaration_error,
            inventory_complete=observer.checkpoint
            and not declaration_error
            and not collection_failed,
            items=[
                {**row, "selected": nodeid in selected, "order": selected.get(nodeid)}
                for nodeid, (_, row) in sorted(observer.captured.items())
            ]
            if observer.actual_selection is not None
            else [],
            case_results=[member(case) for case in selected_ids],
        )
    print("TELESCOPE_RESULT " + canonical(result), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
