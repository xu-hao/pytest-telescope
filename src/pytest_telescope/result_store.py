"""Optional remote result store: read-through and write-behind over the local cache."""

import importlib
import re
import threading
import time

from .model import DeclarationError, UnsupportedCapability

# One request, then one more after each delay: three attempts for a transient failure.
RETRY_DELAYS = (0.5, 1.0)
# Seconds one request may wait for the store to connect or to send more data.
REQUEST_TIMEOUT = 10
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})

_UNAVAILABLE = object()


class StoreError(Exception):
    """A store request failed; repeating it is not expected to help."""


class TransientStoreError(StoreError):
    """A store request failed in a way that may succeed when repeated."""


def parse(url):
    """Split a store URL into its backend, bucket and object-name prefix."""
    if type(url) is not str:
        raise DeclarationError("result_store must be a URL such as gs://bucket/prefix")
    scheme, separator, location = url.partition("://")
    if not separator or not re.fullmatch(r"[a-z][a-z0-9+.-]*", scheme):
        raise DeclarationError("result_store must be a URL such as gs://bucket/prefix")
    if scheme not in BACKENDS:
        raise UnsupportedCapability("result-store:backend", "result_store supports gs:// URLs only")
    bucket, _, prefix = location.partition("/")
    prefix = prefix.removesuffix("/")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket):
        raise DeclarationError("result_store names an invalid bucket")
    parts = prefix.split("/") if prefix else []
    if len(prefix) > 512 or any(
        not re.fullmatch(r"[A-Za-z0-9._-]+", part) or part in {".", ".."} for part in parts
    ):
        raise DeclarationError(
            "result_store prefix must be path segments of letters, digits, '.', '_' and '-'"
        )
    return scheme, bucket, prefix


class GcsObjects:
    """Objects of one bucket, read and created with application default credentials."""

    def __init__(self, bucket, client):
        self._name, self._client = bucket, client
        self._bucket = None
        self._lock = threading.Lock()

    def _handle(self):
        # Credentials are resolved on first use, never while a declaration is validated.
        # Reading and creating objects bills no project, so the client is built without
        # one: credentials that name no project still reach the bucket.
        with self._lock:
            if self._bucket is None:
                self._bucket = self._client(project=None).bucket(self._name)
            return self._bucket

    @staticmethod
    def _failure(error):
        import requests
        from google.api_core.exceptions import GoogleAPICallError
        from google.auth.exceptions import GoogleAuthError, TransportError

        if isinstance(error, GoogleAPICallError):
            transient = error.code in TRANSIENT_STATUS
        elif isinstance(error, GoogleAuthError):
            transient = isinstance(error, TransportError) or bool(
                getattr(error, "retryable", False)
            )
        else:
            transient = isinstance(
                error,
                (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                    ConnectionError,
                    TimeoutError,
                ),
            )
        return (TransientStoreError if transient else StoreError)(type(error).__name__)

    def read(self, name, limit):
        """At most `limit + 1` stored bytes of the object, or None when it does not exist.

        The bytes are read as stored, never decompressed, so the range bounds the read.
        """
        from google.api_core.exceptions import NotFound, RequestRangeNotSatisfiable

        try:
            return (
                self._handle()
                .blob(name)
                .download_as_bytes(
                    end=limit,
                    raw_download=True,
                    checksum=None,
                    retry=None,
                    timeout=REQUEST_TIMEOUT,
                )
            )
        except NotFound:
            return None
        except RequestRangeNotSatisfiable:
            # An empty object has no first byte to send; it holds no record.
            return b""
        except Exception as error:  # the client library is an external boundary
            raise self._failure(error) from error

    def create(self, name, data):
        """Create the object unless one exists; False when one already did."""
        from google.api_core.exceptions import PreconditionFailed

        try:
            self._handle().blob(name).upload_from_string(
                data,
                content_type="application/json",
                if_generation_match=0,
                retry=None,
                timeout=REQUEST_TIMEOUT,
            )
            return True
        except PreconditionFailed:
            return False
        except Exception as error:  # the client library is an external boundary
            raise self._failure(error) from error


def gcs_objects(bucket):
    try:
        storage = importlib.import_module("google.cloud.storage")
    except ImportError as error:
        raise UnsupportedCapability(
            "result-store:gcs",
            "a gs:// result store requires the Google Cloud Storage client; "
            "install pytest-telescope[gcs]",
        ) from error
    return GcsObjects(bucket, storage.Client)


# Object client of one bucket, per URL scheme.
BACKENDS = {"gs": gcs_objects}


class RemoteStore:
    """One invocation's bounded use of a store; safe to call from the worker threads."""

    def __init__(self, backend, objects, prefix, namespace):
        self.backend = backend
        self._objects = objects
        self._base = "/".join(part for part in (prefix, namespace) if part)
        self._lock = threading.Lock()
        self._reason = None
        self._retries = 0
        self._lookups = dict(hits=0, misses=0, invalid=0, failed=0, skipped=0)
        self._uploads = dict(created=0, existing=0, failed=0, skipped=0)

    def _request(self, counts, operation):
        with self._lock:
            if self._reason is not None:
                counts["skipped"] += 1
                return _UNAVAILABLE
        reason = "transient"
        for delay in (*RETRY_DELAYS, None):
            try:
                return operation()
            except TransientStoreError:
                pass
            except StoreError:
                reason = "rejected"
                break
            with self._lock:
                if delay is None or self._reason is not None:
                    break
                self._retries += 1
            time.sleep(delay)
        with self._lock:
            counts["failed"] += 1
            if self._reason is None:
                self._reason = reason
        return _UNAVAILABLE

    def name(self, key):
        return f"{self._base}/{key}.json"

    def lookup(self, key, limit, parse_record):
        """The validated result stored for `key`, or None; never raises for the store."""
        data = self._request(self._lookups, lambda: self._objects.read(self.name(key), limit))
        if data is _UNAVAILABLE:
            return None
        result = None if data is None else parse_record(data)
        with self._lock:
            self._lookups[
                "misses" if data is None else "invalid" if result is None else "hits"
            ] += 1
        return result

    def upload(self, key, data):
        """Create the record for `key` unless the store already has one."""
        created = self._request(self._uploads, lambda: self._objects.create(self.name(key), data))
        if created is not _UNAVAILABLE:
            with self._lock:
                self._uploads["created" if created else "existing"] += 1

    def skip_upload(self):
        with self._lock:
            self._uploads["skipped"] += 1

    def cancel(self, remaining):
        """The invocation was interrupted: `remaining` uploads will not start."""
        with self._lock:
            self._uploads["skipped"] += remaining
            if self._reason is None:
                self._reason = "cancelled"

    def report(self, *, local, remote):
        with self._lock:
            return {
                "backend": self.backend,
                "state": "available" if self._reason is None else "degraded",
                "reason": self._reason,
                "reused_groups": {"local": local, "remote": remote},
                "lookups": dict(self._lookups),
                "uploads": dict(self._uploads),
                "retries": self._retries,
            }


def connect(url, namespace):
    """Validate a store URL and its client; no request is sent until first use."""
    scheme, bucket, prefix = parse(url)
    return RemoteStore(scheme, BACKENDS[scheme](bucket), prefix, namespace)
