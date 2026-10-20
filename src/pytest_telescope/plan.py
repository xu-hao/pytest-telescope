"""Static scope preview; actual case enumeration belongs to isolated collection."""

from . import python_source, runtime_profiles
from .execution import execution_mode
from .model import DeclarationError, InfrastructureError, load, render_json

PlanError = DeclarationError


def make_plan(
    root,
    *,
    suites=None,
    structural_files=None,
    runtime_profile=None,
    required_capabilities=None,
    python_profile=None,
    source_roots=None,
):
    manifest = load(
        root,
        suite_names=suites,
        structural_files=structural_files,
        python_profile=python_profile,
        source_roots=source_roots,
    )
    selection = runtime_profiles.normalize(manifest.runtime, runtime_profile, required_capabilities)
    runtime_profiles.validate_environment(selection, manifest.env)
    result = {
        "runtime": selection,
        "python": python_source.envelope(manifest.suites),
        "mode": "plan-only",
        "execution": {
            "mode": execution_mode(manifest.execution),
            "profile": execution_mode(manifest.execution) + "-v1",
        },
        "case_inventory": "requires isolated pytest collection",
        "inventory": manifest.inventory,
        "suites": [{**suite.identity(), "inventory": suite.inventory} for suite in manifest.suites],
        "distributions": sorted(manifest.distributions),
        "discovery_scope": "broad-potential" if manifest.broad_inputs else "declared-union",
        "inputs": manifest.units[0].inputs,
        "declarations": [
            {
                "file": row["file"],
                "line": row["position"][0],
                "scope": row["owner"],
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
