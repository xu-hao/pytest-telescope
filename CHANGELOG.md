# Changelog

## 0.1.0a6

- Moving a checkout with pytest assertion-rewrite bytecode no longer refuses valid
  literal telescope declarations. Declaration validation uses the module's current
  project location while preserving source-position, literal-argument and provenance
  checks.

## 0.1.0a5

**Isolated child interpreters can import declared source through an editable install.**

- A project installed into the invoking environment in editable form, for example with
  `pip install -e .`, now works when its code under test starts an isolated child such as
  `subprocess.run([sys.executable, "-I", "-m", "toypkg.worker"])`. Previously the child
  failed with `ModuleNotFoundError` in a scoped test, although it passed under plain
  pytest.
- The sandbox shows the same frozen, read-only declared files at the project's own host
  path as at `/project`. The setuptools finder module and `.pth` path entries both point
  there, so they resolve to declared files only. Undeclared files stay absent at both
  paths, the live checkout is never mounted, and the working directory stays `/project`.
- An environment inside the project directory, such as `.venv`, is mounted at its host
  path on top of that view. The frozen copy on the host is never modified.
- The sandbox identity also covers how the environment reaches into the project,
  relative to the project. Re-pointing an editable install, or moving a checkout whose
  editable install still names the old path, reruns scoped cases. Identical checkouts at
  different paths still share results, and a project without an editable install keeps
  a path-independent identity.
- `sandbox.policy` in results is `bubblewrap-view-v2`. Results recorded by earlier
  versions are not reused.
- A project directory that the sandbox cannot also show at its host path is refused with
  `sandbox:environment`: `/`, `/tmp`, `/dev/shm`, a directory containing one the sandbox
  mounts, or one inside `/project`, `/metadata`, `/telescope`, `/proc` or `/dev` (other
  than `/dev/shm`). `/project` itself remains supported.

## Earlier releases

Release notes for 0.1.0a1 to 0.1.0a4 are on the
[GitHub releases page](https://github.com/xu-hao/pytest-telescope/releases).
