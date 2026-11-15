"""Isolated wire-level checks for conditional direct LocalAPI access."""

import io
import json
import socket
import subprocess
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rss_aggregator.runtime import RuntimeFailure
from rss_aggregator.serve_config import ServeConfigClient

TCP_CREDS = "curl -u:fixture-token http://localhost:41112/localapi/v0/status\n"
UNIX_PATH = "/private/fixture-tailscaled.sock"
UNIX_CREDS = (
    f"curl --unix-socket {UNIX_PATH} http://local-tailscaled.sock/localapi/v0/status\n"
)
TAG = "a" * 64
SECRET = "fixture-token private-admin.invalid /private/fixture-tailscaled.sock"


def reply(body=b"{}", status=200, headers=None):
    if headers is None:
        headers = [("Etag", TAG)]
    lines = [f"HTTP/1.1 {status} Fixture"]
    lines.extend(f"{key}: {value}" for key, value in headers)
    lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


class ControlledStream(io.BytesIO):
    def __init__(self, response, error):
        super().__init__(response)
        self.error = error
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.error:
            raise self.error
        return super().read(size)

    def readline(self, size=-1):
        if self.error:
            raise self.error
        return super().readline(size)


class ControlledSocket:
    def __init__(self, response):
        self.response = response
        self.wire = bytearray()
        self.closed = False
        self.timeout = None
        self.address = None
        self.send_error = None
        self.receive_error = None
        self.connect_error = None
        self.stream = None

    def setsockopt(self, *args):
        pass

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, address):
        self.address = address
        if self.connect_error:
            raise self.connect_error

    def sendall(self, data):
        self.wire.extend(data)
        if self.send_error:
            raise self.send_error

    def makefile(self, mode):
        assert mode == "rb"
        self.stream = ControlledStream(self.response, self.receive_error)
        return self.stream

    def close(self):
        self.closed = True

    def request(self):
        head, body = bytes(self.wire).split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        headers = [line.split(": ", 1) for line in lines[1:]]
        assert len(headers) == len(dict(headers)), "Unexpected duplicate wire headers"
        return lines[0], dict(headers), body


@pytest.fixture
def runtime():
    return SimpleNamespace(
        _run=Mock(return_value=subprocess.CompletedProcess([], 0, TCP_CREDS, SECRET))
    )


@pytest.fixture
def transport(monkeypatch):
    sockets = []
    calls = []
    pending = []

    def connect(address, timeout, source_address):
        calls.append((address, timeout, source_address))
        assert pending, "Unexpected connection/retry"
        sock = pending.pop(0)
        sockets.append(sock)
        return sock

    factory = Mock(side_effect=connect)
    monkeypatch.setattr(socket, "create_connection", factory)
    # stdlib HTTPConnection must never honor proxy environment settings.
    monkeypatch.setenv("HTTP_PROXY", "http://private-admin.invalid:9876")
    monkeypatch.setenv("ALL_PROXY", "http://private-admin.invalid:9876")
    return SimpleNamespace(
        pending=pending, sockets=sockets, calls=calls, factory=factory
    )


def queue(transport, response):
    sock = ControlledSocket(response)
    transport.pending.append(sock)
    return sock


def assert_sanitized(error):
    rendered = "".join(
        traceback.format_exception(type(error.value), error.value, error.tb)
    )
    for secret in SECRET.split():
        assert secret not in str(error.value)
        assert secret not in rendered
    assert error.value.__suppress_context__ or error.value.__context__ is None


def test_tcp_exact_wire_requests_share_discovery_and_upstream_tag(runtime, transport):
    first = queue(transport, reply(b'{"TCP": {}}'))
    second = queue(transport, reply(b""))
    client = ServeConfigClient(runtime)
    config, etag = client.read()
    assert config == {"TCP": {}}
    assert etag == TAG
    client.write({"TCP": {"443": {"HTTPS": True}}}, etag)
    runtime._run.assert_called_once_with(
        "tailscale", "debug", "local-creds", check=False
    )
    assert transport.calls == [(("127.0.0.1", 41112), 10, None)] * 2
    assert first.request() == (
        "GET /localapi/v0/serve-config HTTP/1.1",
        {
            "Host": "local-tailscaled.sock",
            "Accept-Encoding": "identity",
            "Authorization": "Basic OmZpeHR1cmUtdG9rZW4=",
        },
        b"",
    )
    body = b'{"TCP": {"443": {"HTTPS": true}}}'
    assert second.request() == (
        "POST /localapi/v0/serve-config HTTP/1.1",
        {
            "Host": "local-tailscaled.sock",
            "Accept-Encoding": "identity",
            "Authorization": "Basic OmZpeHR1cmUtdG9rZW4=",
            "If-Match": TAG,
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
        body,
    )
    assert first.closed and second.closed
    assert first.stream.closed and second.stream.closed


@pytest.mark.parametrize("etag", [TAG, '"opaque-tag"'])
def test_null_config_keeps_exact_original_tag(runtime, transport, etag):
    queue(transport, reply(b"null\n", headers=[("ETag", etag)]))
    post = queue(transport, reply(b""))
    client = ServeConfigClient(runtime)
    config, actual_tag = client.read()
    assert config == {}
    assert actual_tag == etag
    client.write(config, actual_tag)
    assert post.request()[1]["If-Match"] == etag
    assert post.request()[2] == b"{}"


def test_stderr_never_used_as_discovery(runtime, transport):
    runtime._run.return_value = subprocess.CompletedProcess([], 0, "", TCP_CREDS)
    with pytest.raises(RuntimeFailure) as error:
        ServeConfigClient(runtime)
    assert_sanitized(error)
    transport.factory.assert_not_called()


@pytest.mark.parametrize(
    "output",
    [
        "",
        TCP_CREDS.rstrip("\n"),
        TCP_CREDS + "\n",
        TCP_CREDS.replace("\n", "\r\n"),
        " " + TCP_CREDS,
        TCP_CREDS + "echo ignored\n",
        "sudo " + TCP_CREDS,
        TCP_CREDS.replace("-u:fixture-token", "-uuser:fixture-token"),
        TCP_CREDS.replace("-u:fixture-token", "-u:"),
        TCP_CREDS.replace("fixture-token", "'fixture-token'"),
        TCP_CREDS.replace("fixture-token", "fixture token"),
        TCP_CREDS.replace("fixture-token", "fixture-token;bad"),
        TCP_CREDS.replace("41112", "0"),
        TCP_CREDS.replace("41112", "-1"),
        TCP_CREDS.replace("41112", "65536"),
        TCP_CREDS.replace("41112", "100000"),
        TCP_CREDS.replace("41112", "04111"),
        TCP_CREDS.replace("41112", "abc"),
        TCP_CREDS.replace("localhost", "127.0.0.1"),
        TCP_CREDS.replace("localhost", "private-admin.invalid"),
        TCP_CREDS.replace("http:", "https:"),
        TCP_CREDS.replace("/status", "/status?query=1"),
        UNIX_CREDS.replace(UNIX_PATH, "relative.sock"),
        UNIX_CREDS.replace(UNIX_PATH, "/private/with space.sock"),
        UNIX_CREDS.replace(UNIX_PATH, "/private/with\x00null.sock"),
        UNIX_CREDS.replace("--unix-socket", "--unix-socket="),
        UNIX_CREDS.replace("local-tailscaled.sock", "localhost"),
        None,
    ],
)
def test_rejects_unsupported_discovery_without_connecting(runtime, transport, output):
    runtime._run.return_value = subprocess.CompletedProcess([], 0, output, SECRET)
    with pytest.raises(RuntimeFailure) as error:
        ServeConfigClient(runtime)
    assert_sanitized(error)
    transport.factory.assert_not_called()


@pytest.mark.parametrize("returncode", [1, 127])
def test_failed_discovery_is_sanitized(runtime, transport, returncode, capsys, caplog):
    runtime._run.return_value = subprocess.CompletedProcess(
        [], returncode, TCP_CREDS, SECRET
    )
    with pytest.raises(RuntimeFailure) as error:
        ServeConfigClient(runtime)
    assert_sanitized(error)
    assert capsys.readouterr() == ("", "")
    assert not caplog.records
    transport.factory.assert_not_called()


def test_discovery_exception_is_sanitized(runtime, transport):
    runtime._run.side_effect = RuntimeFailure(SECRET)
    with pytest.raises(RuntimeFailure) as error:
        ServeConfigClient(runtime)
    assert_sanitized(error)
    assert "compatibility" in str(error.value)
    assert "local permissions" in str(error.value)
    transport.factory.assert_not_called()


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        (b"{}", []),
        (b"{}", [("ETag", "")]),
        (b"{}", [("ETag", TAG), ("etag", TAG)]),
        (b"{}", [("ETag", "W/" + TAG)]),
        (b"{}", [("ETag", "*")]),
        (b"{}", [("ETag", "first,second")]),
        (b"{}", [("ETag", '"unterminated')]),
        (b"{}", [("ETag", '"')]),
        (b"{}", [("ETag", "tag\x00")]),
        (b"{}", [("ETag", "has space")]),
        (b"{}", [("ETag", "folded\r\n continued")]),
        (b"{}", [("ETag", "caf\xe9")]),
        (b"{", [("ETag", TAG)]),
        (b"\xff", [("ETag", TAG)]),
        (b"[]", [("ETag", TAG)]),
        (b'"object required"', [("ETag", TAG)]),
        (b"42", [("ETag", TAG)]),
        (b"true", [("ETag", TAG)]),
        (b'{"bad": NaN}', [("ETag", TAG)]),
        (b'{"bad": Infinity}', [("ETag", TAG)]),
        (b"{} trailing", [("ETag", TAG)]),
        (b'{"leak": "' + SECRET.encode() + b'"', [("ETag", TAG)]),
    ],
)
def test_rejects_malformed_config_or_etag(runtime, transport, body, headers):
    sock = queue(transport, reply(body, headers=headers))
    with pytest.raises(RuntimeFailure) as error:
        ServeConfigClient(runtime).read()
    assert_sanitized(error)
    assert "compatibility" in str(error.value)
    assert "local permissions" in str(error.value)
    assert sock.closed
    assert transport.factory.call_count == 1


@pytest.mark.parametrize(
    "etag",
    [
        "",
        None,
        7,
        "\r\n" + SECRET,
        "tag\nInjected: value",
        "weak\r",
        "W/tag",
        "*",
        "a,b",
        "a b",
        '"bad',
    ],
)
def test_invalid_etag_cannot_cause_unconditional_post(runtime, transport, etag):
    with pytest.raises(RuntimeFailure) as error:
        ServeConfigClient(runtime).write({}, etag)
    assert_sanitized(error)
    transport.factory.assert_not_called()


@pytest.mark.parametrize("config", [None, [], {"bad": float("nan")}, {"bad": object()}])
def test_invalid_write_config_fails_before_connect(runtime, transport, config):
    with pytest.raises(RuntimeFailure) as error:
        ServeConfigClient(runtime).write(config, TAG)
    assert_sanitized(error)
    transport.factory.assert_not_called()


def test_cyclic_config_fails_before_connect(runtime, transport):
    config = {}
    config["cycle"] = config
    with pytest.raises(RuntimeFailure) as error:
        ServeConfigClient(runtime).write(config, TAG)
    assert_sanitized(error)
    transport.factory.assert_not_called()


def test_412_is_concurrent_sanitized_and_never_retried(
    runtime, transport, capsys, caplog
):
    sock = queue(transport, reply(SECRET.encode(), status=412))
    client = ServeConfigClient(runtime)
    with pytest.raises(RuntimeFailure, match="concurrent") as error:
        client.write({}, TAG)
    assert_sanitized(error)
    assert transport.factory.call_count == 1
    assert sock.request()[0].startswith("POST ")
    assert sock.request()[1]["If-Match"] == TAG
    assert sock.closed
    assert capsys.readouterr() == ("", "")
    assert not caplog.records


@pytest.mark.parametrize("method", ["read", "write"])
@pytest.mark.parametrize("status", [201, 204, 301, 302, 307, 308, 401, 403, 404, 500])
def test_other_statuses_and_redirects_fail_without_followup(
    runtime, transport, method, status
):
    queue(
        transport,
        reply(
            SECRET.encode(),
            status=status,
            headers=[("Location", "http://private-admin.invalid/")],
        ),
    )
    client = ServeConfigClient(runtime)
    with pytest.raises(RuntimeFailure) as error:
        client.read() if method == "read" else client.write({}, TAG)
    assert_sanitized(error)
    assert transport.factory.call_count == 1


@pytest.mark.parametrize("method", ["read", "write"])
@pytest.mark.parametrize(
    "phase", ["connect", "send", "receive", "protocol", "truncated"]
)
def test_transport_failures_are_sanitized_and_not_retried(
    runtime, transport, method, phase
):
    sock = queue(transport, reply())
    if phase == "connect":
        transport.factory.side_effect = OSError(SECRET)
    elif phase == "send":
        sock.send_error = OSError(SECRET)
    elif phase == "receive":
        sock.receive_error = TimeoutError(SECRET)
    elif phase == "protocol":
        sock.response = SECRET.encode() + b"\r\n\r\n"
    else:
        sock.response = b"HTTP/1.1 200 OK\r\nETag: tag\r\nContent-Length: 999\r\n\r\n{"
    client = ServeConfigClient(runtime)
    with pytest.raises(RuntimeFailure) as error:
        client.read() if method == "read" else client.write({}, TAG)
    assert_sanitized(error)
    if method == "write":
        assert "outcome may be unknown" in str(error.value)
        assert "Inspect local Serve status before retrying" in str(error.value)
    else:
        assert "compatibility" in str(error.value)
        assert "local permissions" in str(error.value)
    assert transport.factory.call_count == 1
    if phase != "connect":
        assert sock.closed


@pytest.mark.parametrize("method", ["read", "write"])
def test_response_size_is_bounded(runtime, transport, method):
    sock = queue(transport, reply(b" " * (8 * 1024 * 1024 + 1)))
    client = ServeConfigClient(runtime)
    with pytest.raises(RuntimeFailure) as error:
        client.read() if method == "read" else client.write({}, TAG)
    assert_sanitized(error)
    assert sock.stream.read_sizes == [8 * 1024 * 1024 + 1]
    assert sock.closed and sock.stream.closed
    assert transport.factory.call_count == 1


def test_exact_response_limit_remains_readable(runtime, transport):
    body = b"{}" + b" " * (8 * 1024 * 1024 - 2)
    sock = queue(transport, reply(body))
    assert ServeConfigClient(runtime).read() == ({}, TAG)
    assert sock.stream.read_sizes == [8 * 1024 * 1024]
    assert sock.closed and sock.stream.closed


def test_chunked_config_response_retains_tag(runtime, transport):
    wire = (
        b"HTTP/1.1 200 OK\r\nETag: chunked-revision\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"4\r\nnull\r\n0\r\n\r\n"
    )
    queue(transport, wire)
    assert ServeConfigClient(runtime).read() == ({}, "chunked-revision")


def test_connection_close_config_response_retains_tag(runtime, transport):
    wire = b"HTTP/1.0 200 OK\r\nETag: closed-revision\r\n\r\n{}"
    sock = queue(transport, wire)
    assert ServeConfigClient(runtime).read() == ({}, "closed-revision")
    assert sock.closed and sock.stream.closed


def test_unix_transport_uses_socket_path_canonical_host_and_no_auth(
    runtime, transport, monkeypatch
):
    runtime._run.return_value = subprocess.CompletedProcess([], 0, UNIX_CREDS, SECRET)
    sockets = [ControlledSocket(reply(b"null")), ControlledSocket(reply(b""))]
    factory = Mock(side_effect=sockets)
    monkeypatch.setattr(socket, "socket", factory)
    client = ServeConfigClient(runtime)
    config, tag = client.read()
    client.write(config, tag)
    assert factory.call_args_list == [((socket.AF_UNIX, socket.SOCK_STREAM),)] * 2
    for sock in sockets:
        assert sock.address == UNIX_PATH
        assert sock.timeout == 10
        assert sock.closed
        assert sock.request()[1]["Host"] == "local-tailscaled.sock"
        assert "Authorization" not in sock.request()[1]
    assert sockets[0].request()[0] == "GET /localapi/v0/serve-config HTTP/1.1"
    assert sockets[1].request()[1]["If-Match"] == TAG
    assert sockets[1].request()[2] == b"{}"
    transport.factory.assert_not_called()
    runtime._run.assert_called_once_with(
        "tailscale", "debug", "local-creds", check=False
    )


def test_unix_connect_failure_is_sanitized_and_closed(runtime, transport, monkeypatch):
    runtime._run.return_value = subprocess.CompletedProcess([], 0, UNIX_CREDS, SECRET)
    sock = ControlledSocket(reply())
    sock.connect_error = OSError(SECRET)
    factory = Mock(return_value=sock)
    monkeypatch.setattr(socket, "socket", factory)
    with pytest.raises(RuntimeFailure) as error:
        ServeConfigClient(runtime).read()
    assert_sanitized(error)
    assert sock.closed
    assert factory.call_count == 1
    transport.factory.assert_not_called()


def test_post_lost_response_has_no_retry_or_rollback(runtime, transport):
    sock = queue(transport, b"")
    sock.receive_error = ConnectionResetError(SECRET)
    with pytest.raises(RuntimeFailure) as error:
        ServeConfigClient(runtime).write({"TCP": {}}, TAG)
    assert_sanitized(error)
    assert sock.request()[0] == "POST /localapi/v0/serve-config HTTP/1.1"
    assert json.loads(sock.request()[2]) == {"TCP": {}}
    assert transport.factory.call_count == 1
