"""The `gs://` backend through the real Google Cloud Storage client, without a network.

The client sends real HTTP requests through `requests`; a transport adapter mounted on
its session answers them from memory as the storage JSON API would.
"""

import io
import json
import re
import threading
from urllib.parse import parse_qs, unquote, urlsplit

import google.auth
import pytest
import requests
from google.auth.credentials import AnonymousCredentials
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
from urllib3.response import HTTPResponse

from pytest_telescope import controller, result_store

URL = "gs://shared-results/team/cache"
NO_LOOKUPS = dict(hits=0, misses=0, invalid=0, failed=0, skipped=0)
NO_UPLOADS = dict(created=0, existing=0, failed=0, skipped=0)


class Service(requests.adapters.BaseAdapter):
    """Object download and multipart upload of the storage JSON API, held in memory."""

    def __init__(self):
        super().__init__()
        self.objects, self.requests, self.faults = {}, [], []
        self.lock = threading.Lock()

    def close(self):
        pass

    def respond(self, request, status, body=b"", headers=None):
        if status >= 400:
            body = json.dumps({"error": {"code": status}}).encode()
            headers = {"Content-Type": "application/json"}
        raw = HTTPResponse(
            body=io.BytesIO(body),
            headers={"Content-Length": str(len(body)), **(headers or {})},
            status=status,
            preload_content=False,
        )
        return requests.adapters.HTTPAdapter().build_response(request, raw)

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        parts = urlsplit(request.url)
        query = parse_qs(parts.query)
        if re.fullmatch(r"/storage/v1/b/[^/]+", parts.path):
            # Some client versions look up bucket metadata in the background.
            return self.respond(request, 403)
        with self.lock:
            self.requests.append(
                dict(
                    method=request.method,
                    path=parts.path,
                    query=query,
                    range=request.headers.get("Range"),
                    timeout=timeout,
                )
            )
            fault = self.faults.pop(0) if self.faults else None
        if isinstance(fault, int):
            return self.respond(request, fault)
        if fault is not None:
            raise fault
        download = re.fullmatch(r"/download/storage/v1/b/([^/]+)/o/([^/]+)", parts.path)
        if request.method == "GET" and download and query == {"alt": ["media"]}:
            body = self.objects.get((download[1], unquote(download[2])))
            if body is None:
                return self.respond(request, 404)
            first, last = map(
                int, re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["Range"]).groups()
            )
            if first >= len(body):
                return self.respond(request, 416)
            part = body[first : last + 1]
            return self.respond(
                request,
                206,
                part,
                {"Content-Range": f"bytes {first}-{first + len(part) - 1}/{len(body)}"},
            )
        upload = re.fullmatch(r"/upload/storage/v1/b/([^/]+)/o", parts.path)
        if request.method == "POST" and upload and query.get("uploadType") == ["multipart"]:
            content_type = request.headers["Content-Type"]
            if isinstance(content_type, bytes):
                content_type = content_type.decode()
            boundary = re.search(r'boundary="([^"]+)"', content_type)[1].encode()
            _, metadata, content, _ = request.body.split(b"--" + boundary)
            name = (upload[1], json.loads(metadata.split(b"\r\n\r\n", 1)[1])["name"])
            with self.lock:
                if query.get("ifGenerationMatch") == ["0"] and name in self.objects:
                    return self.respond(request, 412)
                self.objects[name] = content.split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n")
            return self.respond(
                request,
                200,
                json.dumps({"bucket": name[0], "name": name[1], "generation": "1"}).encode(),
                {"Content-Type": "application/json"},
            )
        raise AssertionError(f"unexpected storage request: {request.method} {request.url}")


@pytest.fixture
def service(monkeypatch):
    """Route the client that `gs://` stores construct to the in-memory service.

    The store's own client arguments are kept; only the HTTP session is replaced. The
    application default credentials name no project, and the environment names none.
    """
    served, real_client = Service(), storage.Client
    served.clients = []

    def client(**arguments):
        served.clients.append(arguments)
        session = AuthorizedSession(AnonymousCredentials())
        session.mount("https://", served)
        return real_client(_http=session, **arguments)

    def application_default(*args, **kwargs):
        return AnonymousCredentials(), None

    for name in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    for name in ("STORAGE_EMULATOR_HOST", "API_ENDPOINT_OVERRIDE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(google.auth, "default", application_default)
    monkeypatch.setattr(storage, "Client", client)
    monkeypatch.setattr(result_store, "RETRY_DELAYS", (0, 0))
    return served


@pytest.fixture
def objects(service):
    return result_store.BACKENDS["gs"]("shared-results")


def test_read_is_bounded_and_missing_or_empty_objects_hold_no_record(service, objects):
    assert objects.read("team/absent.json", 10) is None
    service.objects["shared-results", "team/a record.json"] = b"0123456789abcdef"
    service.objects["shared-results", "team/empty.json"] = b""
    assert objects.read("team/a record.json", 10) == b"0123456789a"
    assert objects.read("team/a record.json", 100) == b"0123456789abcdef"
    assert objects.read("team/empty.json", 10) == b""
    sent = service.requests[1]
    assert sent["path"] == "/download/storage/v1/b/shared-results/o/team%2Fa%20record.json"
    assert sent["range"] == "bytes=0-10"
    assert all(request["timeout"] == result_store.REQUEST_TIMEOUT for request in service.requests)
    assert len(service.requests) == 4


def test_create_is_conditional_on_absence_and_keeps_the_first_object(service, objects):
    assert objects.create("team/record.json", b"first") is True
    assert objects.create("team/record.json", b"second") is False
    assert service.objects == {("shared-results", "team/record.json"): b"first"}
    assert [request["query"]["ifGenerationMatch"] for request in service.requests] == [["0"]] * 2
    assert all(request["timeout"] == result_store.REQUEST_TIMEOUT for request in service.requests)


@pytest.mark.parametrize(
    "fault",
    [
        408,
        429,
        500,
        502,
        503,
        504,
        requests.exceptions.ConnectionError("synthetic"),
        requests.exceptions.ReadTimeout("synthetic"),
    ],
)
@pytest.mark.parametrize("operation", ["read", "create"])
def test_transient_failures_are_typed_and_not_retried_by_the_client(
    service, objects, fault, operation
):
    service.faults = [fault]
    with pytest.raises(result_store.TransientStoreError):
        objects.read("a.json", 5) if operation == "read" else objects.create("a.json", b"x")
    # The client library repeats nothing itself; the controller owns the retry budget.
    assert len(service.requests) == 1 and not service.objects


@pytest.mark.parametrize("fault", [400, 401, 403])
@pytest.mark.parametrize("operation", ["read", "create"])
def test_rejections_are_typed_and_not_transient(service, objects, fault, operation):
    service.faults = [fault]
    with pytest.raises(result_store.StoreError) as failure:
        objects.read("a.json", 5) if operation == "read" else objects.create("a.json", b"x")
    assert not isinstance(failure.value, result_store.TransientStoreError)
    assert len(service.requests) == 1


def test_a_missing_bucket_rejects_uploads(service, objects):
    service.faults = [404]
    with pytest.raises(result_store.StoreError) as failure:
        objects.create("a.json", b"x")
    assert not isinstance(failure.value, result_store.TransientStoreError)


def test_records_are_shared_through_the_client(machines, service):
    first = controller.run(machines("first"), result_store=URL)
    assert first["status"] == "passed"
    assert first["result_store"]["uploads"] == NO_UPLOADS | dict(created=3)
    assert first["result_store"]["lookups"] == NO_LOOKUPS | dict(misses=3)
    namespace = f"team/cache/execution-v{controller.SCHEMA}/"
    assert len(service.objects) == 3
    assert all(
        bucket == "shared-results" and re.fullmatch(namespace + r"[0-9a-f]{64}\.json", name)
        for bucket, name in service.objects
    )

    second = controller.run(machines("second"), result_store=URL)
    assert second["counts"]["reused"] == 3 and len(machines.executed) == 3
    assert second["result_store"]["reused_groups"] == dict(local=0, remote=3)
    assert second["result_store"]["state"] == "available"


def test_credentials_that_name_no_project_reach_the_store(machines, service):
    # Reading and creating objects bills no project, so credentials need not name one.
    report = controller.run(machines("first"), result_store=URL)
    assert report["result_store"]["state"] == "available", report["result_store"]
    assert report["result_store"]["uploads"] == NO_UPLOADS | dict(created=3)
    assert len(service.objects) == 3
    # One client per invocation, and the store never supplies credentials itself.
    assert len(service.clients) == 1 and "credentials" not in service.clients[0]


def test_unavailable_service_degrades_after_three_requests(machines, service):
    service.faults = [503] * 100
    report = controller.run(machines("first"), result_store=URL)
    assert report["status"] == "passed" and report["counts"]["executed"] == 3
    assert report["result_store"]["state"] == "degraded"
    assert report["result_store"]["reason"] == "transient"
    assert report["result_store"]["lookups"] == NO_LOOKUPS | dict(failed=1, skipped=2)
    assert report["result_store"]["uploads"] == NO_UPLOADS | dict(skipped=3)
    assert len(service.requests) == 3


def test_denied_access_degrades_after_one_request(machines, service):
    service.faults = [403] * 100
    report = controller.run(machines("first"), result_store=URL)
    assert report["status"] == "passed" and report["counts"]["executed"] == 3
    assert report["result_store"]["state"] == "degraded"
    assert report["result_store"]["reason"] == "rejected"
    assert len(service.requests) == 1


def test_missing_credentials_degrade_without_failing_the_run(machines, service, monkeypatch):
    def client(**arguments):
        raise DefaultCredentialsError("no application default credentials")

    monkeypatch.setattr(storage, "Client", client)
    report = controller.run(machines("first"), result_store=URL)
    assert report["status"] == "passed" and report["counts"]["executed"] == 3
    assert report["result_store"]["state"] == "degraded"
    assert report["result_store"]["reason"] == "rejected"
    assert report["result_store"]["lookups"] == NO_LOOKUPS | dict(failed=1, skipped=2)
    assert not service.requests
