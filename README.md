# pytest-telescope

Declared inputs. Isolated tests. Focused reruns.

Unmarked tests run fresh through ordinary pytest in the invoking Python environment.
Literal input declarations opt cases into an independent restricted execution route,
where unchanged successful results can be reused. Two cases in one file can declare
different resources and rerun independently:

```python
import pytest


@pytest.mark.telescope(inputs=["data/red.json"], bundles=["pricing"])
def test_red(): ...


@pytest.mark.parametrize(
    "product",
    [pytest.param("blue", marks=pytest.mark.telescope(
        inputs=["data/blue.json"], bundles=["pricing"]
    ))],
)
def test_price(product): ...
```

```toml
[tool.pytest_telescope.bundles.pricing]
inputs = ["app/__init__.py", "app/pricing.py", "data/currency.json"]
```

**Replacement status: UNQUALIFIED.** These guides describe the accepted
[ordinary/Bubblewrap contract](docs/bubblewrap.md), not a published backend or a qualified
0.1.0a4 release. Only the bounded resource mechanism has passed its native proof.
Runtime preparation, routing, lifecycle and full installed-package qualification remain
required. Published 0.1.0a3 keeps its historical Docker behavior; its artifacts are immutable.

## Start with ordinary tests, then declare scopes

A literal inherited `inputs`, `bundles` or `distributions` marker opts into scoped
execution, including an empty list. Module, class, function and `pytest.param`
declarations add together. Project defaults alone, absence assertions alone and
`cache=False` alone leave a case ordinary. Absence assertions still check exactly their
named paths. Invalid declarations refuse rather than becoming ordinary tests.

The [complete quickstart](docs/index.md) contains copyable application, test, JSON and
TOML files, parameter-specific declarations and a denied-read example. It shows expected
replacement behavior; no replacement installation command is claimed qualified yet.
An all-ordinary invocation needs neither Bubblewrap nor a prepared scoped runtime.
“All-ordinary” means there are no potential scoped declarations anywhere in the effective
structural file boundary. Selecting only ordinary cases with `-k` or `-m` from a mixed
boundary still requires scoped discovery and preflight for its potential scoped cases.

## Fixture and import boundaries

Each suite has one fresh ordinary pytest session, with its invoking interpreter,
installed plugins, environment, cwd, rootdir, configuration and arguments. Ordinary
results are never reused. Session/module/class fixtures keep normal pytest lifetimes
among that suite's ordinary tests.

Scoped `case` mode remains the default: every actual scoped case gets a fresh restricted
session and private scratch. Explicit [batch mode](docs/batching.md) shares fixtures
only within one suite/module and exactly equal effective scopes. Any member miss reruns
the complete selected group; groups never union unequal inputs.

Mixed modules and conftest files may be imported in ordinary context. The collection
checkpoint removes scoped items before pytest fixture setup or test bodies. Independently
restricted discovery supplies scoped authority; ordinary collection never does. Inventory
disagreement refuses without widening or replay. A refusal does not imply no earlier
ordinary import or test ran. The [execution contract](docs/execution-contract.md) explains
these boundaries and the ordering of sessions.

## Runtime and results

The replacement proposes `bubblewrap-v1` with explicitly prepared `python-v2` or
`git-local-v2` frozen runtimes; old Docker artifacts are ineligible and never silently
migrated. [Git](docs/git-runtime.md) and [isolated child lookup](docs/isolated-source.md)
remain required qualification work. No Bash, browser or service profile is added.

[Schema 3](docs/result-schema.md) distinguishes ordinary/scoped routes, configured and
observed resource limits, and unknown CPU measurements. Ordinary execution is not a
sandbox or cache-authority claim. Filesystem isolation alone does not make arbitrary
Python deterministic; scoped `cache=False` runs fresh without reusable recording.

See the [marker reference](docs/specification.md), [scoped guide](docs/scoped-execution.md),
[development guide](docs/development.md), and [publishing guide](docs/publishing.md).
The [benchmark](docs/benchmark.md) retains historical measurements; they do not qualify
the replacement. Licensed under the [MIT License](LICENSE).
