# Private Tailscale HTTPS and ReadKit checkpoint

This is the operational runbook for [issue #3](https://github.com/brucepucci/rss-aggregator/issues/3).
Command syntax, settings, JSON and failure contracts are in [README](../README.md#readkit-connection--private-https).
Use placeholders in shared evidence; hostnames, device addresses, usernames, tokens,
passwords and private feed URLs do not belong here.

## Prerequisites and ownership

1. Use the existing Tailscale installation/session on the server Mac, reader Mac
   and iOS device. Do not replace it or stop it with the application.
2. Finish the loopback FreshRSS installer with SQLite and form authentication.
   The PHP security gates must pass before adding credentials.
3. Check existing Serve listeners locally. This implementation requires exclusive
   project ownership of the node's HTTPS port 443; it deliberately refuses to share
   a port with another project's root handler, subpath or Funnel listener. It leaves
   other ports, virtual services, certificates and the Tailscale service alone.
4. An administrator must approve/enable MagicDNS and HTTPS certificates if absent.
   Certificate hostnames appear in public Certificate Transparency logs. Serve
   remains private; do not configure public DNS, router forwarding or Funnel.
5. Grant only the intended reader devices TCP 443 to the server Mac, including
   their IPv4/IPv6 addresses as appropriate. Do not grant 8080: the backend stays
   on `127.0.0.1`. Preserve unrelated grants. Recheck access after device changes.

Start checks `tailscale status --json`, `tailscale serve status --json` and the
local effective `tailscale debug netmap` packet filter before creating forwarding.
The netmap is a diagnostic interface, verified against the installed CLI version;
unrecognized output fails rather than assuming authorization. A TCP 443 rule is
only a coarse prerequisite, **not proof that both specific clients can connect**.
No command changes the central policy, enables tailnet-wide HTTPS or uses `--yes`.
If certificate/access authorization fails, inspect it locally and obtain approval;
there is no insecure bypass.

## Connect and verify the server

Use the same `RSS_STATE_DIR`, `RSS_PORT` and private external `RSS_BACKUP_DIR` as
normal operation. A different worktree must explicitly point at the original
ownership directory; do not copy the ownership record.

```sh
export RSS_TAILSCALE=1
./scripts/rss start
./scripts/rss status
```

Use the HTTPS origin returned locally by `start`. Check the root/login page and
`/api/` in a browser. Links, redirect destinations and the displayed GReader API
address should use this same HTTPS origin, not HTTP, loopback or container names.
FreshRSS recognizes Serve's forwarded protocol itself; Apache forwarded-client-IP
trust stays disabled. Web login remains form-authenticated; Tailscale identity does
not become a FreshRSS login.

In FreshRSS **Profile**, set a strong API password distinct from the web password.
Private start enables API access and enforces form login/no anonymous reading or
refresh. Existing users, subscriptions and reading state are retained.

```sh
./scripts/rss api-check
```

Enter the FreshRSS username and separate API password at the local prompts. JSON
must report `diagnostics: "PASS"`, `authenticated: true`, subscriptions and a
sample article. Try an intentionally incorrect password once: expect exit 1 and
sanitized rejection without JSON. Then repeat with the valid password. Default mode
POSTs over verified TLS and reports `transport: "https"`, `https_verified: true`;
redirects are refused rather than forwarding tokens. Do not publish raw HTTP
responses, HTML, authentication payloads or command tracing.

The mini's own HTTPS-origin requests currently fail during TLS despite successful
iPhone diagnostics. The approved supplemental local authentication check is:

```sh
./scripts/rss api-check --loopback
```

It checks only the running/security-verified owned app's actual sole `127.0.0.1`
publication and still requires active owned forwarding. JSON explicitly reports
`transport: "loopback"`, `https_verified: false`. This sends no credentials beyond
loopback and is **not** an insecure TLS bypass or a substitute for HTTPS/client tests.
There is no automatic HTTPS-to-HTTP fallback. Do not change grants or the Tailscale
installation to work around an unproven cause.

## Actual ReadKit clients — human-assisted

Upstream [FreshRSS documentation](https://freshrss.github.io/FreshRSS/en/users/06_Mobile_access.html)
requires a per-user API password and links `/api/` diagnostics.
The [Google Reader API reference](https://freshrss.github.io/FreshRSS/en/developers/06_GoogleReader_API.html)
documents `<HTTPS-ORIGIN>/api/greader.php` as the API endpoint. The
[ReadKit FAQ](https://readkit.app/faq) lists FreshRSS support but does not establish
its account URL field's form. **The user has now verified the instance-origin
entry `<HTTPS-ORIGIN>` unchanged, without `/api/greader.php`, in ReadKit on the Air.**
ReadKit on the iPhone also connected using the same instructions. Both show the
public feed. These actual-app checks, not generic endpoint documentation, establish
the tested account URL form. Exact app/OS versions were not recorded.

On **both** actual macOS and iOS apps:

1. Record app/OS versions without device names or account identifiers.
2. Choose the **FreshRSS** integration, not Google or Fever. Use the same FreshRSS
   user and its separate API password on both devices.
3. Observe the URL field/help and try the indicated form. Record only whether the
   accepted form is `<HTTPS-ORIGIN>` or `<HTTPS-ORIGIN>/api/greader.php`, including
   any trailing-slash requirement. Do not publish the actual origin or credentials.
4. Synchronize and confirm the public FreshRSS releases feed and fetched articles.
5. Confirm no certificate warning/bypass. If unreachable, check Tailscale on all
   devices, current TCP 443 authorization, DNS, clock, certificate trust and any
   relevant macOS/iOS local-network permission prompt. No shared LAN is required.

### Reading-state matrix

Use identifiable articles from the public test feed, not a private feed. After
each action, explicitly synchronize both clients and reload FreshRSS's web page.
Verify the result in **both** destinations; merely displaying articles is not enough.

| Origin | Action | Other ReadKit client | FreshRSS web |
| --- | --- | --- | --- |
| macOS ReadKit | read, then unread | Pass (user observed) | Pass (user observed) |
| iOS ReadKit | read, then unread | Pass (user observed) | Pass (user observed) |
| macOS ReadKit | star, then unstar | Pass (user observed) | Pass (user observed) |
| iOS ReadKit | star, then unstar | Pass (user observed) | Pass (user observed) |

The user exercised a single article through iPhone read/star, Mac unread/unstar,
Mac read/star, then iPhone unread/unstar, checking the other reader and web after
each step. Propagation was reported as roughly **30 seconds**, not a guaranteed
latency bound. Optional additional web-originated edits were not separately tested.
Reading state is server-side; ReadKit synchronization is distinct from publisher
feed fetching (startup plus UTC minutes 00/20/40, subject to caching/backoff).

### Stop/start and preservation

1. Leave one public article read and starred; record a sanitized baseline.
2. Run normal `stop` with a private external backup destination. Confirm project
   forwarding is absent, the VM stops, and **both** readers cannot reach FreshRSS.
   Cached articles may remain readable; that is not live synchronization.
3. Tailscale must remain connected and unrelated Serve/services unchanged.
4. Keep `RSS_TAILSCALE=1` and run `start`, synchronize both clients, and verify
   subscriptions/articles and the deliberate read/star state are retained.
5. Repeat start/stop; no duplicate route, instance or subscriptions should appear.

## Sanitized local checkpoint

- `uv sync --locked` and `uv run --locked rss-dev check` pass: **190 default tests
  in 78.40s**, six live deselected, Ruff lint/format clean. See README's issue #3
  checkpoint for failure history and the verification scope.
- With the existing ownership directory, private external backup directory and
  `RSS_TAILSCALE=1`, `uv run --locked pytest -p no:cacheprovider -m live -q --tb=short`
  passed **six retained Mac/Podman checks in 66.79s**, 189 default deselected.
  A final real private start restored serving and the application security audit.
  A later API protocol-error redaction guard passed the full default baseline;
  the retained live suite was not rerun for that diagnostic-only change.
- Real runtime: the user completed the installer locally and reported public-feed
  articles. Private starts pass both effective PHP gates, cron and fetching.
- A real iPhone API HTTP 503 exposed incorrect optional-boolean CLI syntax. Serving
  was stopped with a verified external backup; flags now use `--option=value`, and
  effective configuration is verified before forwarding. Real loopback GReader
  compatibility now returns PASS, with API enabled and anonymous access disabled.
- Tailnet policy: the user approved and applied a narrow reader-device TCP 443 grant;
  the effective incoming filter then contained TCP 443. No private identifiers recorded.
- Certificate-only retrieval succeeded; private Serve is running. The user reported
  iPhone browser reachability. The mini's own HTTPS-origin requests fail during TLS;
  the user confirmed iPhone `/api/` diagnostics PASS after the fix. ReadKit on the
  Air and iPhone authenticate and display the public feed. The user confirmed neither
  device required a certificate-warning bypass. Do not claim default host-side HTTPS
  `api-check` success. Explicit `--loopback` diagnostics
  are approved as a supplemental authentication seam, not proof of HTTPS.
- Local authentication: the user ran `api-check --loopback` with their real API
  credentials and reported PASS, authenticated true, one subscription and one
  sampled article; transport explicitly reported loopback and no HTTPS verification.
  A separate deliberately invalid loopback credential probe returned HTTP 401.
  Real HTTPS authentication is now demonstrated by the user connecting actual
  ReadKit on the Air and iPhone using the FreshRSS integration.
- ReadKit account URL form: instance origin unchanged, without `/api/greader.php`.
  Air initially showed ten unread public-feed articles. After reading one on the
  Air, the iPhone reflected that change. The user subsequently confirmed the full
  matrix above, including FreshRSS web corroboration and ~30-second propagation.
- Normal stop: verified backup, owned forwarding removed, loopback closed, project
  VM stopped, Tailscale still connected. The user confirmed sync failed on **both**
  clients while stopped (cached content may remain). After normal private start,
  the user confirmed both synchronize and the deliberately read/starred article
  retains both markings. The application is left running privately.
- The actual pre-existing Serve configuration was empty. Isolated command tests
  preserve unrelated listeners, refuse changed/unowned routes and cover repetitions;
  no claim is made that mocks prove real unrelated infrastructure behavior.
- Safe real certificate/policy-failure injection: **not performed**; isolated failure
  checks do not prove these real infrastructure failure modes.

Limitations: the mini self-HTTPS cause remains unresolved (CLI 1.104.7 versus
1.104.2 daemon; no upgrade/restart performed). Exact ReadKit/OS versions and optional
web-originated state edits were not recorded. No new CI, alternate Python/platform
or typechecker result is claimed. Verification backups are temporary private files,
not a durable backup configuration. Use placeholders rather than private identifiers
in any handoff. Publication, review, issue closure and merge are separate user choices.
