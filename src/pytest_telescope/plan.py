"""Static scope preview; actual case enumeration belongs to isolated collection."""

from . import python_source, runtime_profiles
from .execution import backend_profile, controller_envelope, envelope
from .execution import execution_mode as choose_mode
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
    backend=None,
    execution_mode=None,
    budget=None,
    controller_resources=None,
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
    required = any(manifest.scoped_files.values())
    resources = envelope(manifest.execution, budget, require_fit=required)
    controller = controller_envelope(manifest.execution, controller_resources)
    mode = choose_mode(manifest.execution, execution_mode)
    result = {
        "backend": backend_profile(manifest.execution, backend),
        "resources": {
            "scoped": {
                "required": required,
                "validated": False,
                "configured": {"worker": resources.worker, "budget": resources.budget},
                "concurrency": None,
                "enforced": None,
            },
            "controller": {"configured": controller, "enforced": None},
            "ordinary": {"enforcement": "inherited-host", "slot_limit": 1, "peak_sessions": 0},
        },
        "runtime": selection,
        "python": python_source.envelope(manifest.suites),
        "mode": "plan-only",
        "execution": {
            "mode": mode,
            "profile": mode + "-v1",
        },
        "case_inventory": "requires real ordinary/scoped pytest collection",
        "inventory": manifest.inventory,
        "suites": [{**suite.identity(), "inventory": suite.inventory} for suite in manifest.suites],
        "distributions": sorted(manifest.distributions),
        "discovery_scope": "declared-union" if required else "ordinary",
        "inputs": sorted({name for unit in manifest.units for name in unit.inputs}),
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
