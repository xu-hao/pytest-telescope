"""Bounded literal marker discovery; never imports or evaluates project code."""

import ast
import hashlib
import json
from dataclasses import dataclass

MAX_SOURCE = 4 * 1024 * 1024
# The totals scale with the frozen project: the source total equals its 2 GiB byte bound,
# and the node total allows one AST node per eight bytes of that source budget.
MAX_TOTAL_SOURCE = 2 * 1024 * 1024 * 1024
MAX_NODES = 200_000
MAX_TOTAL_NODES = 2**28
MAX_MARKERS = 100_000
MAX_DEPTH = 128


class MarkerError(ValueError):
    pass


@dataclass(frozen=True)
class Scan:
    """One bounded parse of a distinct regular file, shared by all of its import origins."""

    file: str
    sha256: str
    rows: tuple


def size_text(limit):
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2)):
        if limit % scale == 0:
            return f"{limit // scale} {unit}"
    return f"{limit:,}-byte"


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
        "tools",
        "env",
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


def oversize_message():
    return f"Python source exceeds the {size_text(MAX_SOURCE)} per-file static discovery limit"


def discover(content, filename):
    rows, nodes = scan(content)
    return label(rows, filename), nodes


def label(rows, filename):
    """Give one import origin its own records; equal to parsing the bytes under that name."""
    registry = []
    for row in rows:
        record = {"file": filename, **row}
        record["id"] = hashlib.sha256(canonical(record).encode()).hexdigest()
        registry.append(record)
    return registry


def scan(content):
    """Parse bounded source once into origin-independent literal marker rows."""
    if len(content) > MAX_SOURCE:
        raise MarkerError(oversize_message())
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise MarkerError("cannot parse bounded Python source for literal declarations") from exc
    parents = {}
    nodes = []
    pending = [(tree, 0)]
    while pending:
        node, depth = pending.pop()
        if len(nodes) >= MAX_NODES:
            raise MarkerError(
                f"Python syntax exceeds the static discovery limit of {MAX_NODES:,} AST nodes"
                " per file"
            )
        if depth > MAX_DEPTH:
            raise MarkerError(
                f"Python syntax exceeds the static discovery depth limit of {MAX_DEPTH}"
            )
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
                return "decorator", [*owner(parent), parent.name], None
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
                return "scope", owner(assignment), None
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
                    return (
                        "parameter",
                        [*owner(function), function.name],
                        {
                            "position": [
                                parametrization.lineno,
                                parametrization.end_lineno,
                                parametrization.col_offset,
                                parametrization.end_col_offset,
                            ],
                            "index": values.elts.index(param),
                            "count": len(values.elts),
                        },
                    )
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
        kind, names, parameter = placement(node)
        registry.append(
            {
                "position": [node.lineno, node.end_lineno, node.col_offset, node.end_col_offset],
                "kwargs": kwargs,
                "kind": kind,
                "owner": names,
                "parameter": parameter,
            }
        )
        if len(registry) > MAX_MARKERS:
            raise MarkerError(
                f"Python source exceeds the static discovery limit of {MAX_MARKERS:,}"
                " literal telescope marker calls"
            )
    registry.sort(key=lambda row: row["position"])
    return tuple(registry), len(nodes)
