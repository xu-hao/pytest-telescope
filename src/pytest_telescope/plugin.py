"""Optional pytest adapter; the dedicated CLI avoids host plugin startup."""

import json
import os

import pytest

from pytest_telescope.plan import PlanError, make_plan


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("telescope")
    group.addoption(
        "--telescope",
        action="store_true",
        default=False,
        help="Run declared targets through the isolated cache-first controller.",
    )
    group.addoption(
        "--telescope-plan",
        action="store_true",
        default=False,
        help="Validate declarations and print a static plan without importing tests.",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    options = early_config.known_args_namespace
    if options.telescope and options.telescope_plan:
        raise pytest.UsageError("choose execution or plan-only mode, not both")
    if options.telescope or options.telescope_plan:
        requested = "--telescope" if options.telescope else "--telescope-plan"
        if requested not in early_config.invocation_params.args or os.environ.get("PYTEST_ADDOPTS"):
            raise pytest.UsageError(
                "use an explicit command-line telescope flag without PYTEST_ADDOPTS; dedicated CLI supports declared pytest addopts"
            )
    if options.telescope:
        from pytest_telescope.controller import main

        forwarded = [arg for arg in early_config.invocation_params.args if arg != "--telescope"]
        raise SystemExit(main(["run", "--", *forwarded]))
    if options.telescope_plan:
        try:
            plan = make_plan(early_config.invocation_params.dir)
        except PlanError as exc:
            raise pytest.UsageError(f"pytest-telescope: {exc}") from exc
        print(json.dumps(plan, indent=2, sort_keys=True))
        raise SystemExit(0)
