# Publishing

## Read the Docs

The repository includes `.readthedocs.yaml` and a strict MkDocs configuration. The
public [Read the Docs project
dashboard](https://app.readthedocs.org/projects/pytest-telescope/) is configured. Its
existence does not establish a successful build or live docs. The service installs
`docs/requirements.txt`; it does not need the package or sandbox backend to build
documentation.

The `docs.yml` workflow requests a `latest` build on pushes to `main` and manual
dispatches. It uses the encrypted `READTHEDOCS_TOKEN` repository secret and a fixed
[Read the Docs API build
endpoint](https://docs.readthedocs.com/platform/stable/api/v3.html#build-triggering).
The workflow performs no checkout, never runs on pull requests, and has bounded
connection/request deadlines and retries. No repository webhook is required. A
successful workflow means the request was accepted; verify the matching commit's
completed build and rendered pages before advertising a hosted documentation URL.

If the request fails, inspect the workflow status and project build list before
retrying. Correct the token, API availability, or build configuration responsible. A
failed documentation build needs a corrected source and a successful new build; do not
label the version published merely because the trigger was accepted.

## PyPI

The initial version is `0.1.0a1`. Its release must provide the complete isolated
execution and result-reuse contract, including real backend qualification and measured
benchmark results; a planner-only build is not a release candidate. No package release
is implied by a source merge. The `publish.yml` workflow runs only when a GitHub Release
is published. It requires a tag of `v` followed by the exact package version, and it
refuses publication until a license is recorded in package metadata and a `LICENSE` file
exists.

Before the first release:

1. Verify the MIT license text, package metadata, README rendering, source, wheel,
   sdist, and qualification results.
2. Retain the GitHub `pypi` environment's release-tag restrictions. This project's
   release policy has no required-reviewer gate.
3. Configure the encrypted GitHub secret `PYPI_API_TOKEN` with a token authorized
   to publish `pytest-telescope`. The project name must be available to that PyPI
   account. Never put the credential in source, build artifacts, or logs.
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
cause and choose a new version if necessary. A failed documentation build likewise needs
a corrected commit and a successful new build before the hosted version can be
considered published.
