"""Static scope preview; actual case enumeration belongs to isolated collection."""

from .model import DeclarationError, InfrastructureError, load, render_json

PlanError = DeclarationError


def make_plan(root):
    manifest = load(root)
    result = {
        "mode": "plan-only",
        "case_inventory": "requires isolated pytest collection",
        "inventory": manifest.inventory,
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
