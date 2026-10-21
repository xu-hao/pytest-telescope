"""Static route and scope preview; actual case enumeration belongs to real collection."""

from .execution import execution_mode, worker_count
from .model import DeclarationError, InfrastructureError, load, render_json
from .sandbox import BACKEND, POLICY

PlanError = DeclarationError


def make_plan(root, *, suites=None, structural_files=None):
    manifest = load(root, suite_names=suites, structural_files=structural_files)
    mode = execution_mode(manifest.execution)
    result = {
        "mode": "plan-only",
        "execution": {
            "mode": mode,
            "profile": mode + "-v1",
            "workers": worker_count(manifest.execution),
        },
        "sandbox": {
            "backend": BACKEND,
            "policy": POLICY,
            "required": manifest.scope_possible,
            "identity": None,
        },
        "case_inventory": "requires pytest collection",
        "inventory": manifest.inventory,
        "suites": [
            {
                **suite.identity(),
                "inventory": suite.inventory,
                "ordinary_files": manifest.ordinary_files[suite.name],
                "scoped_files": manifest.scoped_files[suite.name],
            }
            for suite in manifest.suites
        ],
        "distributions": sorted(manifest.distributions),
        "inputs": [] if manifest.pool is None else manifest.pool.inputs,
        "declarations": [
            {
                "file": row["file"],
                "line": row["position"][0],
                "scope": row["owner"],
                "route": "scoped"
                if {"inputs", "bundles", "distributions"}.intersection(row["kwargs"])
                else "ordinary",
                "inputs": manifest.declarations[row["id"]].inputs,
                "absent": manifest.declarations[row["id"]].absent,
                "cache": manifest.declarations[row["id"]].cache,
            }
            for row in manifest.registry
        ],
    }
    try:
        render_json(result)
    except InfrastructureError as exc:
        raise DeclarationError(str(exc)) from exc
    return result
