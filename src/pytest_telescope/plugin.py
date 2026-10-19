"""Opt-in static planning with explicit refusal of unfinished isolation."""

import json

import pytest

from pytest_telescope.plan import PlanError, make_plan


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("telescope")
    group.addoption(
        "--telescope",
        action="store_true",
        default=False,
        help="Request isolated execution (not implemented).",
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
    if options.telescope:
        raise pytest.UsageError(
            "pytest-telescope: isolated execution is not implemented; "
            "no tests were collected or run by telescope. "
            "Omit --telescope to use ordinary, unrestricted pytest."
        )
    if options.telescope_plan:
        try:
            plan = make_plan(early_config.invocation_params.dir)
        except PlanError as exc:
            raise pytest.UsageError(f"pytest-telescope: {exc}") from exc
        print(json.dumps(plan, indent=2, sort_keys=True))
        raise SystemExit(0)
