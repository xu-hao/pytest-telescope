# Remote result store

A *remote result store* shares successful scoped results between machines. It is an
object store that pytest-telescope reads when the local result cache has no record for
a group, and writes after it has recorded a new one. Use it when runs start with an
empty cache directory, such as cloud batch workers or CI jobs on fresh machines, so that
a later run of the same inputs elsewhere reuses the result instead of executing it again.

The store is optional and layered over the local cache: the cache directory keeps
working exactly as described in [declarations, inventory and reuse](execution-contract.md),
and a run without a configured store sends no remote request. The first supported
backend is Google Cloud Storage, addressed as `gs://bucket/prefix`.

## Configure a store

Install the Google Cloud Storage client with the `gcs` extra:

```sh
python -m pip install 'pytest-telescope[gcs]'
```

Then name the store for one invocation, or for every invocation of the project:

```sh
python -m pytest_telescope run --result-store gs://example-results/telescope -- -q
```

```python
from pytest_telescope.controller import run

run(root, result_store="gs://example-results/telescope")
```

```toml
[tool.pytest_telescope.execution]
result_store = "gs://example-results/telescope"
```

The flag or argument overrides the configuration key. The URL is `gs://`, a bucket
name, and an optional prefix of path segments made of letters, digits, `.`, `_` and `-`;
the prefix may be empty and may end with one `/`. The pytest adapter (`pytest
--telescope`) takes no store flag and uses the configuration key.

Credentials are never part of the configuration. The client finds [application default
credentials](https://cloud.google.com/docs/authentication/application-default-credentials)
itself: the attached service account from the metadata server on Google Cloud, or the
credentials that the environment provides elsewhere. A URL cannot carry a key, and no
setting names one. The identity needs permission to read objects
(`storage.objects.get`) and, to share its own results, to create them
(`storage.objects.create`). It never lists, overwrites or deletes objects.

A malformed URL is a declaration error. A URL with another scheme refuses with capability
`result-store:backend`, and a `gs://` URL without the installed client refuses with
`result-store:gcs`. All three are reported before any test session starts; none sends
a request. `plan` validates the configured URL without requiring the client.

## What is read and written

Each scoped group, which is one case in `case` mode or one batch in `batch` mode, has a
cache key and one record named `<key>.json`. In the store that record is the object

```text
<prefix>/execution-v<N>/<key>.json
```

where `N` is the version of the cache record format, so a release that changes the
format writes under a new name and leaves older records untouched.

**Reading.** When the local cache has no valid record for a group, the run asks the
store for that group's object before it starts a sandbox. The bytes are checked with the
same validation as a local record: the record format, its checksum, the key it names,
and that its result is a complete success of exactly this group. A valid record is
written into the local cache through the same staging and publication as a freshly
recorded result, and the group is reported as `reused`. Later runs on that machine find
it locally and do not ask the store again. A missing object, or one that fails any
check, is a miss: the group executes as if no store were configured. The store never
turns a miss into a pass.

**Writing.** After an invocation has published its new records in the local cache, it
uploads those records, byte for byte, under the same names. An upload only creates: it
is conditional on no object existing under that name (generation precondition 0). When
two machines record the same key at the same time, the first upload is kept and the
other is told the object exists, which counts as success. Nothing in the store is ever
replaced. A record that was itself fetched from the store is not uploaded again.

Only what the local cache would record is uploaded. A failed or incomplete group, a
group marked `cache=False`, a run with `--no-cache`, and an invocation whose cleanup
failed publish no local record and therefore upload nothing. With `--force` the run
executes without asking either cache and then uploads; the store keeps any record it
already had. Collection inventories are not shared: every machine collects for itself,
which is also how new tests are discovered before any stored result is accepted.

A record is shared only between runs that compute the same key. Besides the declared
inputs, a key covers the interpreter build, every installed distribution and its
version, the pytest-telescope version, the invoking user and group IDs, and the pytest
options and selectors. Machines therefore share results when they run the same image as
the same user; the checkout's location does not matter.

pytest-telescope never removes objects. Deleting objects, for example with a bucket
lifecycle rule that expires old ones, is always safe: the affected groups execute again.
The local cache keeps its own [bounds](execution-contract.md#bounds-and-performance).

## Trust model

Trust the store exactly as far as you trust everyone who can write to it. Validation
shows that a record is well formed and belongs to the requested key; the checksum
detects damage and is not a signature. Anyone who can create objects under the prefix
can therefore store a passing record for a key whose tests never passed, and every run
that reads the store will reuse it.

- Grant object creation only to the jobs whose results you would accept, such as CI
  jobs of protected branches. Give every other reader read-only access.
- Do not point a run that tests untrusted changes at a store it can write to.
- Use a separate bucket or prefix for each trust boundary.

A read-only job still reuses stored records. Its first upload is refused, which the
report shows as a degraded store with `reason: "rejected"`, one failed upload and no
failed lookup; its remaining uploads are skipped.

Uploaded records are the local cache's records. They contain opaque case and group
identities, outcomes, timings and digests, and no node IDs, parameter values,
environment values, source or test output.

## Failures and the degraded state

The store is a dependency that can fail, and its failure never fails a run or changes a
test outcome. Every remote request is bounded:

- Each request waits at most 10 seconds for the store to connect or to send more data.
- A request that fails transiently (a timeout, a connection error, or status 408, 429,
  500, 502, 503 or 504) is repeated after 0.5 seconds and again after 1 second: at most
  three attempts for one lookup or upload. The client library's own retries are
  disabled, so these are the only repetitions.
- A request that is rejected (denied access, missing credentials, or any other
  response) is not repeated.

When a lookup or upload has used its attempts, or was rejected, the store is *degraded*
for the rest of the invocation. No further request starts: the remaining lookups are
treated as misses and their groups execute, and the remaining uploads are skipped.
A request already in flight on another worker finishes its current attempt and is not
repeated. The run continues with the local cache only, publishes its local records as usual, and keeps the
status and exit code its tests earned.

The report states the degraded store and counts what failed, so that a caller can raise
an infrastructure incident for the store while accepting the test results. A degraded
store is reported, never hidden as an ordinary cache miss.

## Accounting

A run with a configured store adds `result_store` to its result; a run without one has
no such field. The [result reference](result-schema.md#remote-result-store) defines it:

```json
{
  "backend": "gs",
  "state": "available",
  "reason": null,
  "reused_groups": {"local": 12, "remote": 30},
  "lookups": {"hits": 30, "misses": 4, "invalid": 0, "failed": 0, "skipped": 0},
  "uploads": {"created": 4, "existing": 0, "failed": 0, "skipped": 0},
  "retries": 0
}
```

`reused_groups` separates the reused groups whose record was already in the local cache
from those fetched from the store in this run. The store's hit rate for the groups that
the local cache could not serve is `hits / (hits + misses + invalid)`. A nonzero
`invalid` count means the store holds records that do not validate, for example damaged
objects; because uploads never replace an object, remove such objects to share those
keys again.

## Concurrency

Lookups and uploads use the [worker pool](scoped-execution.md#parallel-scoped-workers).
A group's lookup runs in the worker slot that would otherwise start its sandbox, so at
most `workers` lookups or sandboxes are active together. Uploads start after local
publication, when no sandbox is running, on at most `workers` threads. The local cache
lock is released before uploads begin, so a slow store does not hold up another
invocation that uses the same cache directory.
