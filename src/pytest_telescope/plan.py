"""The same validated declaration inventory used by execution."""

import json

from .model import DeclarationError, load

PlanError = DeclarationError


def make_plan(root):
    manifest = load(root)
    result = {
        "mode": "plan-only",
        "inventory": manifest.inventory,
        "targets": [
            {
                "name": unit.name,
                "tests": unit.tests,
                "inputs": unit.inputs,
                "absent": unit.absent,
                "cache": unit.cache,
            }
            for unit in manifest.units
        ],
    }
    if len(json.dumps(result, indent=2, sort_keys=True).encode()) > 1_048_576:
        raise DeclarationError("plan exceeds the 1 MiB output limit")
    return result
