"""Private HTTPS forwarding, scoped to the project's exclusive Serve listener."""

import json
import os
import re
from http.client import HTTPException
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime

SERVE_RECORD = "serve.json"


class _NoRedirect(HTTPRedirectHandler):
    """Never forward credentials or tokens to a redirected API destination."""

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        """Refuse redirects, including HTTPS-to-HTTP downgrades."""
        return None


class PrivateAccess:
    """Preserve all Tailscale configuration outside one owned HTTPS listener."""

    def __init__(self, runtime: "Runtime") -> None:
        """Use the runtime's serialized external-command and private-state boundary."""
        from rss_aggregator.runtime import RuntimeFailure

        self.runtime = runtime
        self.failure = RuntimeFailure
        self.path = runtime.state_dir / SERVE_RECORD

    def enabled(self) -> bool:
        """Read the explicit opt-in without changing the existing Tailscale service."""
        value = os.environ.get("RSS_TAILSCALE", "0")
        if value not in ("0", "1"):
            raise self.failure(
                "RSS_TAILSCALE must be 0 (loopback) or 1 (private HTTPS)."
            )
        return value == "1"

    def _query(self, *args: str) -> dict[str, Any]:
        """Read JSON without disclosing private Tailscale output in errors."""
        result = self.runtime._run("tailscale", *args, check=False)
        if result.returncode:
            raise self.failure(
                "Tailscale query failed; check the installed CLI, connection and local permissions."
            )
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except ValueError as error:
            raise self.failure(
                "Unusable Tailscale JSON; inspect the CLI locally."
            ) from error

    def _record(self, owner: dict[str, Any]) -> dict[str, Any] | None:
        """Reject another ownership context rather than adopting its forwarding."""
        if not self.path.exists():
            return None
        record = json.loads(self.path.read_text())
        if record.get("owner") != owner["token"]:
            raise self.failure(
                "Serve ownership differs; do not edit serve.json to adopt it."
            )
        return record

    def prepare(self, owner: dict[str, Any], port: int) -> str:
        """Refuse listener conflicts before changing the VM or application."""
        status = self._query("status", "--json")
        if status.get("BackendState") != "Running":
            raise self.failure(
                "Connect the existing Tailscale installation before starting private HTTPS."
            )
        host = status.get("Self", {}).get("DNSName", "").rstrip(".")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.ts\.net", host):
            raise self.failure(
                "Tailscale DNS name unavailable; check MagicDNS/HTTPS with the tailnet administrator."
            )
        if host not in (status.get("CertDomains") or []):
            raise self.failure(
                "HTTPS certificates are not enabled for this device; obtain approval before changing tailnet DNS/HTTPS settings."
            )
        config = self._query("serve", "status", "--json")
        record = self._record(owner)
        self._validate(config, record)
        netmap = self._query("debug", "netmap")
        rules = netmap.get("PacketFilter")
        if not isinstance(rules, list):
            raise self.failure(
                "Cannot inspect effective Tailscale access policy; inspect the CLI/netmap locally before enabling Serve."
            )
        if not any(
            (rule.get("Srcs") or rule.get("SrcCaps"))
            and (not rule.get("IPProto") or 6 in rule["IPProto"])
            and any(
                dest.get("Ports", {}).get("First", 65536)
                <= 443
                <= dest.get("Ports", {}).get("Last", -1)
                for dest in rule.get("Dsts", [])
            )
            for rule in rules
        ):
            raise self.failure(
                "Effective Tailscale policy has no incoming TCP 443 grant; obtain approval for a narrowly scoped reader-device rule. No policy was changed."
            )
        return "https://" + host

    def _scope(self, config: dict[str, Any]) -> dict[str, Any]:
        """Select only the node's port 443, leaving service listeners untouched."""
        return {
            "TCP": config.get("TCP", {}).get("443"),
            "Web": {
                key: value
                for key, value in config.get("Web", {}).items()
                if key.endswith(":443")
            },
            "Funnel": any(
                value
                for key, value in config.get("AllowFunnel", {}).items()
                if key.endswith(":443")
            ),
        }

    def _validate(self, config: dict[str, Any], record: dict[str, Any] | None) -> bool:
        """Accept an absent listener or exactly the recorded, non-Funnel route."""
        scope = self._scope(config)
        if not scope["TCP"] and not scope["Web"] and not scope["Funnel"]:
            return False
        if (
            record
            and record.get("phase") in ("pending", "active")
            and scope
            == {
                "TCP": {"HTTPS": True},
                "Web": {
                    record["host"] + ":443": {
                        "Handlers": {"/": {"Proxy": record["target"]}}
                    }
                },
                "Funnel": False,
            }
        ):
            return True
        raise self.failure(
            "Serve HTTPS port 443 is not exclusively project-owned; preserve the existing listener and resolve its ownership locally."
        )

    def status(self, owner: dict[str, Any]) -> dict[str, Any]:
        """Inspect owned forwarding without starting it or exposing configuration."""
        record = self._record(owner)
        if record is None:
            return {"status": "unconfigured"}
        active = self._validate(self._query("serve", "status", "--json"), record)
        return {
            "status": "running" if active else "stopped",
            "url": "https://" + record["host"],
        }

    def stop(self, owner: dict[str, Any]) -> bool:
        """Remove only an exactly matching owned route, even with the VM stopped."""
        record = self._record(owner)
        if record is None:
            return False
        config = self._query("serve", "status", "--json")
        active = self._validate(config, record)
        if active:
            self._mutate("--https=443", "--set-path=/", "off")
            after = self._query("serve", "status", "--json")
            if self._validate(after, record):
                raise self.failure(
                    "Project Serve forwarding did not stop; inspect it locally."
                )
            self._preserved(config, after)
        self.runtime._save(SERVE_RECORD, {**record, "phase": "inactive"})
        return active

    def _mutate(self, *args: str) -> None:
        """Run scoped Serve commands without account-wide prompts or secret output."""
        result = self.runtime._run("tailscale", "serve", *args, check=False, input="")
        if result.returncode:
            raise self.failure(
                "Project Serve change failed; check certificate/HTTPS authorization, local permissions and port ownership. "
                "Inspect tailscale serve status locally; no global reset or account changes were attempted."
            )

    def _preserved(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        """Verify configuration outside the reserved listener was not altered."""

        def unrelated(config: dict[str, Any]) -> dict[str, Any]:
            """Normalize empty CLI maps while retaining unrelated services/settings."""
            result = dict(config)
            for key in ("TCP", "Web", "AllowFunnel"):
                values = {
                    k: v
                    for k, v in config.get(key, {}).items()
                    if k != "443" and not k.endswith(":443")
                }
                result.pop(key, None)
                if values:
                    result[key] = values
            return result

        if unrelated(before) != unrelated(after):
            raise self.failure(
                "Unrelated Serve configuration changed concurrently; inspect locally. No global rollback attempted."
            )

    def check_api(self, origin: str, username: str, password: str) -> dict[str, Any]:
        """Check GReader authentication over the caller's verified application origin.

        The runtime selects trusted HTTPS by default or explicit owned loopback.
        Credentials are sent only as a POST body. Tokens and response bodies stay
        in memory, redirects are refused, and ambient HTTP proxies are bypassed.
        HTTPS uses the default TLS trust store; there is no insecure TLS bypass.
        """
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        endpoint = origin + "/api/greader.php"

        def receive(
            path: str, *, data: bytes | None = None, token: str = "test/1"
        ) -> str:
            """Read a bounded API response without leaking upstream error bodies."""
            request = Request(
                endpoint + path,
                data=data,
                headers={"Authorization": "GoogleLogin auth=" + token},
            )
            try:
                with opener.open(request, timeout=10) as response:
                    body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise self.failure(
                        "FreshRSS API response exceeds the diagnostic limit."
                    )
                return body.decode("utf-8")
            except HTTPError as error:
                if error.code in (401, 403):
                    raise self.failure(
                        "FreshRSS API authentication rejected; use the separate per-user API password."
                    ) from None
                raise self.failure(
                    "FreshRSS API HTTP failure (status "
                    + str(error.code)
                    + "); check API enablement/base URL. Redirects are refused."
                ) from None
            except (OSError, HTTPException, UnicodeError):
                raise self.failure(
                    "FreshRSS HTTPS/API unreachable or certificate untrusted; check Tailscale connectivity, TCP 443 policy, certificate settings and project status."
                ) from None

        if receive("/check/compatibility") != "PASS":
            raise self.failure(
                "FreshRSS GReader compatibility check failed; inspect /api/ locally."
            )
        login = receive(
            "/accounts/ClientLogin",
            data=urlencode({"Email": username, "Passwd": password}).encode(),
        )
        token = next(
            (line[5:] for line in login.splitlines() if line.startswith("Auth=")), ""
        )
        if not token:
            raise self.failure(
                "FreshRSS API authentication rejected; use the separate per-user API password."
            )
        try:
            subscriptions = json.loads(
                receive("/reader/api/0/subscription/list?output=json", token=token)
            )["subscriptions"]
            articles = json.loads(
                receive(
                    "/reader/api/0/stream/contents/user/-/state/com.google/reading-list?n=1&output=json",
                    token=token,
                )
            )["items"]
            if not isinstance(subscriptions, list) or not isinstance(articles, list):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise self.failure(
                "FreshRSS returned unusable API diagnostics; inspect locally without publishing responses."
            ) from None
        return {
            "diagnostics": "PASS",
            "authenticated": True,
            "subscriptions": len(subscriptions),
            "sample_articles": len(articles),
        }

    def start(self, owner: dict[str, Any], port: int, origin: str) -> dict[str, Any]:
        """Publish the security-verified application with a durable ownership intent."""
        self.prepare(owner, port)
        before = self._query("serve", "status", "--json")
        self._validate(before, self._record(owner))
        record = {
            "owner": owner["token"],
            "host": origin.removeprefix("https://"),
            "target": f"http://127.0.0.1:{port}",
            "phase": "pending",
        }
        self.runtime._save(SERVE_RECORD, record)
        self._mutate("--bg", "--https=443", "--set-path=/", record["target"])
        after = self._query("serve", "status", "--json")
        if not self._validate(after, record):
            raise self.failure(
                "Project Serve route is missing; check certificate authorization locally."
            )
        self._preserved(before, after)
        self.runtime._save(SERVE_RECORD, {**record, "phase": "active"})
        return {"status": "running", "url": origin}
