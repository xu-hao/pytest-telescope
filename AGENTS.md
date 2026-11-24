# pytest-telescope contributor instructions

Keep this file limited to repository-wide guardrails. Follow the canonical contracts and
procedures in [docs/index.md](docs/index.md) and [CONTRIBUTING.md](CONTRIBUTING.md)
(including [docs/development.md](docs/development.md) for local verification,
[docs/execution-contract.md](docs/execution-contract.md) for declarations, inventory, and
reuse, [docs/bubblewrap.md](docs/bubblewrap.md) for sandbox routing and the one-path rule,
and [docs/publishing.md](docs/publishing.md) for documentation and package releases).

## Repository verification and sandbox rules

- Full qualification runs `python scripts/check.py --functional-workers 4 --artifacts <new-dir>`
  (default `--functional-profile all`, with `--functional-workers N` matching the
  available CPU count) from a clean checkout (`git status --porcelain --untracked-files=all`
  must be empty) inside an active dedicated Python 3.11+ virtual environment with
  `pip install -e '.[dev]'` and a supported pytest 9 adapter (`pytest==9.2.0`), an
  existing absolute caller-owned `TMPDIR` outside `/tmp` and `/dev/shm` (or the
  coordinator-provided `TMPDIR`), and a fresh non-existent `--artifacts` directory outside
  the checkout.
- The verification gate runs `ruff check .`, `ruff format --check .`, sdist and wheel
  build (`python -m build`) + `twine check --strict`, strict Sphinx `dirhtml`
  documentation (`sphinx -n -W --keep-going -b dirhtml docs <site>`), isolated wheel
  installation and candidate verification, the behavioral and real Bubblewrap (`bwrap`)
  functional suites, and benchmark-harness unit checks. Actual host-dependent and browser
  compatibility benchmarks run separately when explicitly selected
  ([docs/benchmark.md](docs/benchmark.md)).
- Sandbox tests must run real Bubblewrap and verify which files a test can see; skipped or
  expected-failure tests do not count as passing, and `--functional-profile core` is only
  compatibility evidence, never a full qualification or landing verdict. Sandbox changes
  must preserve the one-path rule (`docs/bubblewrap.md#one-path-for-every-file`).

<!-- gardenia:begin -->
- Use the installed `agrein` skills: `ticket-delivery` before changing code, docs,
  config, infrastructure, or live state; `epic-queue` when decomposing multi-ticket
  work or coordinating subagent workers; `bug-investigation` before fixing defects or
  failing checks; and `owner-escalation` for unsettled product/policy rulings, live
  operation approvals, and decision handoffs.
- Coordinate ticket work by delegating each authorized ticket to a worker selected with
  the installed `agroute` and `howhard` skills; an assigned leaf worker implements its
  ticket directly without recursively delegating it (report a capability gap first if
  `agroute` or worker delegation is unavailable on the host):
  1. Inspect or refresh live agent quota state with `agr usage`.
  2. Emit eligible provider-qualified candidates for this host with
     `agr candidates --host <codex|claude|agy> --output /tmp/TASK-candidates.json --json`.
     If `price_request` is non-null, look up official first-party provider pricing (or
     classify internal harness models as `"alias"` or `"unlisted"`), run
     `agr prices resolve --file /tmp/TASK-prices.json --json`, and re-emit candidates once.
  3. Select a route with `howhard` (`context --auto-decide`, or `context`/`retrieve`/`decide`
     evaluating `baseline_request` in an independent stateless context only when non-null, or
     `pick`), then resolve the execution plan with
     `agr plan --host <codex|claude|agy> --decision /tmp/TASK-decision.json --json`.
  4. Honor `needs_review`. Execute native plans (`target_agent == host_agent`) with the
     host's native subagent tool and cross-agent plans (`target_agent != host_agent`) with
     the returned non-interactive CLI command.
  5. Record the actual outcome and measured usage in `howhard` following the plan's
     `feedback` block (`matched_receipt` or `raw_native`), then update `howhard` memory.
- Track every code, test, documentation, or packaging change with a descriptive issue
  (`#N — one sentence describing the work`). Do not file an issue solely for
  machine-local installation, deployment, configuration, service lifecycle, or other
  operator state unless it also requires a repository change or the user requests one.
- Develop and validate only in an issue-specific Git worktree branched from
  `origin/main`. Preserve unrelated work and keep the primary checkout read-only during
  development.
- Verify behavior at public boundaries. Start bug fixes with a failing behavioral test;
  accompany runtime changes with focused tests, affected canonical documentation, and a
  `CHANGELOG.md` entry when the change is user-facing.
- A documentation-only change needs no tests or AGCoord gate. It qualifies only when
  every changed path is `README.md`, `CHANGELOG.md`, `AGENTS.md`, Markdown below
  `docs/`, or a `.png` or `.svg` image below `docs/assets/` whose provenance is
  recorded in `docs/assets/README.md`. Verify the exact pull-request file list and
  diff, then merge it through the forge; the issue, worktree, branch, pull-request,
  and no-direct-target-push rules still apply.
- On Linux hosts with `agcoord` (`agc`), route checks and pull-request landing through
  the local coordinator with integer byte memory claims:
  - Check: `agc run --label "<short plain-English summary>" --no-overflow --resource cpu=4 --resource memory=8589934592 --resource tmpfs=4294967296 --resource tmpfs_inodes=262144 -- python scripts/check.py --functional-workers 4 --artifacts "$(mktemp -u "${TMPDIR:-/var/tmp}/telescope-gate.XXXXXX")"`
  - Land: `agc land <pr> --label "land #<pr> <short summary>" --no-overflow --resource cpu=4 --resource memory=8589934592 --resource tmpfs=4294967296 --resource tmpfs_inodes=262144 -- python scripts/check.py --functional-workers 4 --artifacts "$(mktemp -u "${TMPDIR:-/var/tmp}/telescope-gate.XXXXXX")"`
  Give every job a short plain-English `--label` for humans in `agc list` and `agc tui`
  (lead with what is checked or landed; omit commit SHAs, branch names, `"pre-land"` tags,
  and file paths).
  Never submit coordinated work recursively inside an admitted job, and never bypass or
  separate the gate verdict from publication. On hosts without `agc`, run
  `python scripts/check.py --functional-workers 4 --artifacts "$(mktemp -u "${TMPDIR:-/var/tmp}/telescope-gate.XXXXXX")"` directly and merge through the forge after checks pass.
- Tests must own and stop every process, repository, and temporary directory they
  create, and must never inspect or mutate real host state (`~/.codex`, `~/.claude`,
  `~/.gemini`, `~/.config`, or `~/.local`).
- Never upload a distribution to PyPI or modify live host/production state unless the
  owner explicitly authorizes that exact operation. Permission to implement, test,
  commit, push, merge, tag, or create a GitHub release does not authorize a PyPI upload.
<!-- gardenia:end -->
