"""Conditional Serve configuration access through one discovered LocalAPI transport."""

from __future__ import annotations

import base64
import json
import re
import socket
from http.client import HTTPConnection
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime

_PATH = "/localapi/v0/serve-config"
_HOST = "local-tailscaled.sock"
_TIMEOUT = 10
_MAX_RESPONSE = 8 * 1024 * 1024
_TCP_CREDS = re.compile(
    r"curl -u:([A-Za-z0-9._~+/=-]+) http://localhost:([1-9][0-9]{0,4})"
    r"/localapi/v0/status\n"
)
_UNIX_CREDS = re.compile(
    r"curl --unix-socket (/[^\s\x00-\x1f\x7f]+) "
    r"http://local-tailscaled\.sock/localapi/v0/status\n"
)


class _UnixHTTPConnection(HTTPConnection):
    """Send ordinary HTTP requests over a discovered Unix-domain socket."""

    def __init__(self, path: str) -> None:
        self._socket_path = path
        super().__init__(_HOST, timeout=_TIMEOUT)

    def connect(self) -> None:
        """Connect only to the discovered socket, with a bounded timeout."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(_TIMEOUT)
            sock.connect(self._socket_path)
        except Exception:
            sock.close()
            raise
        self.sock = sock


def _valid_etag(etag: str) -> bool:
    """Accept a single opaque, header-safe strong tag without rewriting it."""
    if (
        not isinstance(etag, str)
        or not re.fullmatch(r"[\x21-\x7e]+", etag)
        or etag.startswith("W/")
        or etag == "*"
        or "," in etag
    ):
        return False
    if '"' in etag:
        return (
            len(etag) >= 2
            and etag.startswith('"')
            and etag.endswith('"')
            and '"' not in etag[1:-1]
        )
    return True


def _reject_constant(value: str) -> None:
    """Reject non-JSON numeric constants accepted by Python's default decoder."""
    raise ValueError("Non-JSON numeric constant")


class ServeConfigClient:
    """Read and conditionally replace Serve configuration without CLI mutations.

    Discovery happens once at construction. Each request uses that same direct
    LocalAPI endpoint; no request is retried, including an uncertain POST.
    """

    def __init__(self, runtime: Runtime) -> None:
        """Discover a supported endpoint without exposing credential diagnostics."""
        from rss_aggregator.runtime import RuntimeFailure, ServeConfigRejected

        self._failure = RuntimeFailure
        self._rejected = ServeConfigRejected
        self._port: int | None = None
        self._socket_path: str | None = None
        self._headers = {"Host": _HOST}
        try:
            result = runtime._run("tailscale", "debug", "local-creds", check=False)
            if result.returncode != 0 or not isinstance(result.stdout, str):
                raise ValueError("Discovery failed")
            tcp = _TCP_CREDS.fullmatch(result.stdout)
            unix = _UNIX_CREDS.fullmatch(result.stdout)
            if tcp:
                self._port = int(tcp[2])
                if not 1 <= self._port <= 65535:
                    raise ValueError("Invalid port")
                credentials = base64.b64encode((":" + tcp[1]).encode("ascii"))
                self._headers["Authorization"] = "Basic " + credentials.decode("ascii")
            elif unix:
                self._socket_path = unix[1]
            else:
                raise ValueError("Unsupported discovery output")
        except Exception:
            raise self._failure(
                "Unable to discover a supported Tailscale LocalAPI; check installed "
                "CLI/daemon compatibility and local permissions."
            ) from None

    def read(self) -> tuple[dict[str, Any], str]:
        """Read a configuration and its exact server-issued concurrency token.

        Returns
        -------
        tuple[dict[str, Any], str]
            Configuration (an initial JSON null becomes an empty dictionary) and
            the unmodified ETag belonging to the original response bytes.

        Raises
        ------
        RuntimeFailure
            If the response is not a bounded JSON object/null with one strong ETag.
        """
        status, etags, body = self._request("GET")
        if status != 200:
            raise self._failure(
                "Unable to read Tailscale Serve configuration; check installed "
                "CLI/daemon compatibility and local permissions."
            )
        try:
            if len(etags) != 1 or not _valid_etag(etags[0]):
                raise ValueError("Invalid ETag")
            config = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
            if config is not None and not isinstance(config, dict):
                raise ValueError("Invalid configuration")
        except Exception:
            raise self._failure(
                "Invalid Tailscale Serve configuration response; check installed "
                "CLI/daemon compatibility and local permissions."
            ) from None
        return ({} if config is None else config), etags[0]

    def write(self, config: dict[str, Any], etag: str) -> None:
        """Replace configuration only if the supplied server token still matches.

        Parameters
        ----------
        config : dict[str, Any]
            JSON object to submit, without a client-computed concurrency token.
        etag : str
            Exact nonempty ETag returned by the snapshot GET.

        Raises
        ------
        ServeConfigRejected
            On invalid input before a request, authentication/authorization
            refusal (HTTP 401/403), or concurrent modification (HTTP 412), when
            this operation is known not to have applied a change.
        RuntimeFailure
            On other request failures. An uncertain write is never retried or
            rolled back here.
        """
        try:
            if not isinstance(config, dict) or not _valid_etag(etag):
                raise ValueError("Invalid conditional write")
            body = json.dumps(config, allow_nan=False).encode("utf-8")
        except Exception:
            raise self._rejected(
                "Invalid conditional Tailscale Serve configuration."
            ) from None
        status, _, _ = self._request("POST", body, etag)
        if status in (401, 403):
            raise self._rejected(
                "Tailscale Serve configuration authorization rejected; check "
                "local credentials and write permissions before retrying."
            )
        if status == 412:
            raise self._rejected(
                "Tailscale Serve configuration changed concurrently; "
                "inspect local Serve status before retrying."
            )
        if status != 200:
            raise self._failure(
                "Unable to confirm Tailscale Serve configuration write; the outcome "
                "may be unknown. Inspect local Serve status before retrying."
            )

    def _request(
        self, method: str, body: bytes | None = None, etag: str | None = None
    ) -> tuple[int, list[str], bytes]:
        """Make one bounded direct request, suppressing all remote diagnostics."""
        connection = None
        response = None
        try:
            if self._socket_path is not None:
                connection = _UnixHTTPConnection(self._socket_path)
            else:
                connection = HTTPConnection("127.0.0.1", self._port, timeout=_TIMEOUT)
            headers = dict(self._headers)
            if etag is not None:
                headers.update({"If-Match": etag, "Content-Type": "application/json"})
            connection.request(method, _PATH, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            # Error bodies are not needed, parsed, or exposed to the caller.
            if status != 200:
                return status, [], b""
            etags = [
                value for key, value in response.getheaders() if key.lower() == "etag"
            ]
            payload = response.read(_MAX_RESPONSE + 1)
            if len(payload) > _MAX_RESPONSE or response.length not in (None, 0):
                raise ValueError("Response oversized or incomplete")
            return status, etags, payload
        except Exception:
            if method == "POST":
                message = (
                    "Tailscale Serve LocalAPI request failed; the write outcome may "
                    "be unknown. Inspect local Serve status before retrying."
                )
            else:
                message = (
                    "Tailscale Serve LocalAPI read failed; check installed "
                    "CLI/daemon compatibility and local permissions."
                )
            raise self._failure(message) from None
        finally:
            # Closing must neither leak transport details nor trigger a retry.
            for resource in (response, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
