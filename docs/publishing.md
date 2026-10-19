# Publishing

## Read the Docs

The repository includes `.readthedocs.yaml` and a strict MkDocs configuration.
An account owner can import `https://github.com/xu-hao/pytest-telescope` into
Read the Docs, select `main` as the default branch, and retain the default config
path. The service installs `docs/requirements.txt`; it does not need the package
or sandbox backend to build documentation. Enable the repository webhook and
verify a successful build before advertising a hosted documentation URL.

The configuration alone does not create a hosted project. Follow the
[Read the Docs import guide](https://docs.readthedocs.com/platform/stable/intro/add-project.html)
and [configuration reference](https://docs.readthedocs.com/platform/stable/config-file/v2.html).

## PyPI

The initial version is `0.1.0a1`, an explicitly incomplete pre-alpha static planner.
No package release is implied by a source merge. The `publish.yml` workflow runs
only when a GitHub Release is published. It requires a tag of `v` followed by the
exact package version, and it refuses publication until a license is recorded in
package metadata and a `LICENSE` file exists.

Before the first release, the owner must:

1. Verify the MIT license text and package metadata in the release artifacts.
2. Configure the GitHub `pypi` environment and its release-tag restrictions.
   Required reviewers are optional according to the maintainer's release policy. Review the source, wheel, sdist, README rendering, and tests.
3. Create a PyPI project or pending Trusted Publisher for `pytest-telescope`, owner
   `xu-hao`, repository `pytest-telescope`, workflow `publish.yml`, environment
   `pypi`. The PyPI project name must be available to that account.
4. Publish the approved GitHub Release, clearly describing the scaffold's limits,
   and complete any configured environment approval. Verify the uploaded version and files.

The build job runs checks without publishing credentials and transfers only wheel
and sdist artifacts. The separate publishing job has OIDC permission and uses
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/);
no persistent API token belongs in this repository. Actions are pinned to complete
commit IDs. The default CI workflow never publishes packages.

If upload fails, inspect the workflow result and PyPI's version/file listing
before retrying. Do not overwrite a released version or conceal partial uploads;
correct the cause and choose a new version if necessary. A failed documentation
build likewise needs a corrected commit and a successful new build before the
hosted version can be considered published.
