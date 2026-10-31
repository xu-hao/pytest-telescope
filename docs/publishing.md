# Publishing

## Read the Docs

Read the Docs builds the documentation using `.readthedocs.yaml`, Sphinx, Furo and MyST
Markdown. It installs `docs/requirements.txt`; building the docs does not require the
package or Bubblewrap. Builds and their status appear on the
[project dashboard](https://app.readthedocs.org/projects/pytest-telescope/).

The `docs.yml` workflow requests a `latest` build on pushes to `main` and manual
dispatches. It uses the encrypted `READTHEDOCS_TOKEN` repository secret and a fixed
[Read the Docs API build
endpoint](https://docs.readthedocs.com/platform/stable/api/v3.html#build-triggering).
The workflow performs no checkout, never runs on pull requests, and has bounded
connection/request deadlines and retries. No repository webhook is required. A
successful workflow means the build request was accepted. Check that the build for the
intended commit finishes and that its pages render correctly.

If the request fails, inspect the workflow status and project build list before
retrying. Fix the token or build configuration, or wait for the API to recover, according
to the error. If the build itself fails, fix the cause and verify a new build.

## PyPI

The `publish.yml` workflow uploads a package when a GitHub Release is published.
Merging source alone does not release it. The tag must be `v` followed by the exact
package version, and the package must have a license in its metadata and a `LICENSE`
file.

Each release must document its execution and cache rules and include results from real
sandbox tests and benchmarks. Version `0.1.0a6` uses ordinary pytest and Bubblewrap
filesystem views.

Before each release:

1. Verify the MIT license text, package metadata, README rendering, source, wheel,
   sdist, and qualification results.
2. Retain the GitHub `pypi` environment's release-tag restrictions. This project's
   release policy has no required-reviewer gate.
3. Use the configured encrypted GitHub secret `PYPI_API_TOKEN` authorized to publish
   `pytest-telescope`. Never put credentials in source, build artifacts, or logs.
4. Publish the intended GitHub Release with the exact version tag and describe
   the supported guarantees and limits. Verify the uploaded version and files.

The build job receives no publishing credential and transfers only wheel and sdist
artifacts. The separate publishing job uses [PyPA's token-based action
interface](https://github.com/pypa/gh-action-pypi-publish#specifying-a-different-username)
with user `__token__`, the encrypted token as its password, and the fixed official
upload endpoint `https://upload.pypi.org/legacy/`. OIDC token permission is absent;
attestations are disabled for this token-based publication mode. No Trusted Publisher
registration is required. Actions remain pinned to complete commit IDs, and the default
CI workflow never publishes packages.

If upload fails, inspect the workflow result and PyPI's version/file listing before
retrying. Do not overwrite a released version or conceal partial uploads; correct the
cause and choose a new version if necessary.
