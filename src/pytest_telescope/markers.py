"""Bounded literal marker discovery; never imports or evaluates project code."""

import ast
import hashlib
import json

MAX_SOURCE = 4 * 1024 * 1024
MAX_TOTAL_SOURCE = 64 * 1024 * 1024
MAX_NODES = 200_000
MAX_TOTAL_NODES = 2_000_000
MAX_MARKERS = 100_000
MAX_DEPTH = 128


class MarkerError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted(node.value) + "." + node.attr
    return ""


def literal(node):
    if isinstance(node, ast.Constant) and type(node.value) in (str, bool):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [literal(item) for item in node.elts]
    raise MarkerError("telescope declarations require literal string lists and booleans")


def validate_kwargs(kwargs):
    if type(kwargs) is not dict or set(kwargs) - {
        "inputs",
        "bundles",
        "absent",
        "cache",
        "distributions",
    }:
        raise MarkerError("unsupported telescope marker fields")
    for name, value in kwargs.items():
        if name == "cache":
            if type(value) is not bool:
                raise MarkerError("telescope cache must be a literal boolean")
        elif (
            type(value) is not list
            or len(value) > 4096
            or any(type(item) is not str or not item or len(item) > 4096 for item in value)
        ):
            raise MarkerError("telescope paths and bundles require bounded literal string lists")
    return kwargs


def discover(content, filename):
    if len(content) > MAX_SOURCE:
        raise MarkerError("test source exceeds the 4 MiB static discovery limit")
    try:
        tree = ast.parse(content, filename=filename)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise MarkerError("cannot parse bounded test source for literal declarations") from exc
    parents = {}
    nodes = []
    pending = [(tree, 0)]
    while pending:
        node, depth = pending.pop()
        if len(nodes) >= MAX_NODES or depth > MAX_DEPTH:
            raise MarkerError("test syntax exceeds static discovery node/depth limits")
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            parents[child] = node
            pending.append((child, depth + 1))

    def owner(node):
        names = []
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(node.name)
        return list(reversed(names))

    def placement(call):
        parent = parents[call]
        if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if call in parent.decorator_list:
                return "decorator", [*owner(parent), parent.name]
        current = call
        while isinstance(parents.get(current), (ast.List, ast.Tuple)):
            current = parents[current]
        assignment = parents.get(current)
        if isinstance(assignment, (ast.Assign, ast.AnnAssign)) and assignment.value is current:
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            container = parents.get(assignment)
            if (
                len(targets) == 1
                and isinstance(targets[0], ast.Name)
                and targets[0].id == "pytestmark"
                and isinstance(container, (ast.Module, ast.ClassDef))
            ):
                return "scope", owner(assignment)
        keyword = parents.get(current)
        param = parents.get(keyword)
        if (
            isinstance(keyword, ast.keyword)
            and keyword.arg == "marks"
            and isinstance(param, ast.Call)
        ):
            if dotted(param.func) == "pytest.param":
                values = parents.get(param)
                parametrization = parents.get(values)
                if isinstance(parametrization, ast.keyword) and parametrization.arg == "argvalues":
                    parametrization = parents.get(parametrization)
                function = parents.get(parametrization)
                if (
                    isinstance(values, (ast.List, ast.Tuple))
                    and isinstance(parametrization, ast.Call)
                    and dotted(parametrization.func) == "pytest.mark.parametrize"
                    and isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and parametrization in function.decorator_list
                ):
                    return "parameter", [*owner(function), function.name]
        raise MarkerError(
            "literal telescope markers belong in decorators, module/class pytestmark, or inline pytest.param marks"
        )

    registry = []
    for node in nodes:
        if isinstance(node, ast.Attribute) and dotted(node) == "pytest.mark.telescope":
            parent = parents.get(node)
            if not isinstance(parent, ast.Call) or parent.func is not node:
                raise MarkerError(
                    "call pytest.mark.telescope directly with literal keyword arguments"
                )
        if not isinstance(node, ast.Call) or dotted(node.func) != "pytest.mark.telescope":
            continue
        if node.args:
            raise MarkerError("telescope declarations accept keyword arguments only")
        kwargs = {}
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg in kwargs:
                raise MarkerError(
                    "computed or duplicate telescope keyword arguments are unsupported"
                )
            kwargs[keyword.arg] = literal(keyword.value)
        validate_kwargs(kwargs)
        kind, names = placement(node)
        record = {
            "file": filename,
            "position": [node.lineno, node.end_lineno, node.col_offset, node.end_col_offset],
            "kwargs": kwargs,
            "kind": kind,
            "owner": names,
        }
        record["id"] = hashlib.sha256(canonical(record).encode()).hexdigest()
        registry.append(record)
        if len(registry) > MAX_MARKERS:
            raise MarkerError("too many literal telescope markers")
    registry.sort(key=lambda row: row["position"])
    return registry, len(nodes)
