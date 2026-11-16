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
   a port with another project's root handler, subpath or Funnel listener, including
   foreground-session listeners. These are refused before VM/application changes.
   Enabled Funnel routes on any port that proxy/forward to this FreshRSS loopback
   backend also cause refusal before backend changes, including subpath proxies
   and foreground/virtual-service configurations. The foreign route is not changed.
   Genuinely unrelated ports/services, private other-port forwarding, certificates
   and Tailscale itself are preserved.
4. An administrator must approve/enable MagicDNS and HTTPS certificates if absent.
   Certificate hostnames appear in public Certificate Transparency logs. Serve
   remains private; do not configure public DNS, router forwarding or Funnel.
5. Grant only the intended reader devices TCP 443 to the server Mac, including
   their IPv4/IPv6 addresses as appropriate. Do not grant 8080: the backend stays
   on `127.0.0.1`. Preserve unrelated grants. Recheck access after device changes.

Start checks `tailscale status --json`, the local daemon's Serve configuration
(including foreground sessions), and the effective `tailscale debug netmap` packet
filter before creating forwarding. `tailscale debug local-creds` discovers only the
existing daemon's loopback/Unix-socket transport; its sensitive output is never
printed or shell-executed. Ownership validation uses the same configuration/ETag
snapshot as the conditional LocalAPI write (`If-Match`). Concurrent changes refuse
start/stop without unconditional retry or rollback; unrelated configuration is
copied unchanged. A rejected start (HTTP 412 or invalid input before sending)
restores the preceding local ownership record, or removes the newly created local
intent if there was none; it does not touch remote configuration. A competing
client's identical route is not thereby adopted by a later status/stop. Unknown
write outcomes retain pending intent for recovery. Unsupported transport/ETag
output or LocalAPI permissions fail
closed, not back to a separate unguarded `tailscale serve` mutation. A write-time
transport failure can have an unknown outcome: inspect local Serve status before
retrying, without an automatic rollback. These diagnostic interfaces are
version-dependent. Initial implementation verification combined a read-only GET
against the existing daemon (with ETag, no mutations) with isolated conditional-write
and lifecycle tests. That initial check alone is **not live-write/HTTPS evidence**;
subsequent isolated-candidate live verification is recorded separately in PR #18.
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

## Real-HTTPS checks — human-assisted

Use the working Air/iPhone path, not the mini's failing self-HTTPS path. Loopback
HTTP 401 and isolated redirect tests do not establish these real-HTTPS results.
Do not weaken TLS, change policy or publish raw requests/responses.

1. Begin a fresh ReadKit **FreshRSS** connection check with the existing HTTPS
   origin/user and a deliberately wrong **API** password; cached synchronization
   is not a new authentication check. Confirm authentication rejection, cancel the
   extra setup, and confirm the existing valid account still synchronizes. Do not
   change the server password or publish either password. Record only the outcome;
   an HTTP status is optional if safely available.
2. On the Air, replace `<HTTPS-ORIGIN>` locally and run these credential-free GETs:

   ```sh
   curl -q -sS --noproxy '*' --proto '=https' --max-redirs 0 \
     -o /dev/null -w 'HTTP %{http_code}\nRedirect: %{redirect_url}\n' \
     '<HTTPS-ORIGIN>/'
   curl -q -sS --noproxy '*' --proto '=https' --max-redirs 0 \
     -o /dev/null -w 'HTTP %{http_code}\nRedirect: %{redirect_url}\n' \
     '<HTTPS-ORIGIN>/api/'
   curl -q -sS --noproxy '*' --proto '=https' --max-redirs 0 \
     --header 'Authorization: GoogleLogin auth=test/1' \
     --write-out '\nHTTP %{http_code}\n' \
     '<HTTPS-ORIGIN>/api/greader.php/check/compatibility'
   ```

   `-q` ignores curl configuration; `-sS` hides progress but shows errors;
   `--noproxy '*'` bypasses ambient proxies; `--proto '=https'` permits HTTPS only.
   TLS verification remains enabled. No `--location` is used and redirects are not
   followed (`--max-redirs 0`). `-o /dev/null` discards page content; `-w` prints
   only status and redirect destination. The compatibility request prints its
   diagnostic body plus status; its header is **dummy diagnostic data**, not a real
   credential. Expected compatibility output is `PASS`/HTTP 200. Plain browser
   navigation omits this header and can report an Authorization-header failure;
   the misspelled `/check/compatability` instead returns HTTP 400.
   Curl exits nonzero for transport/TLS errors (for example code 35 with HTTP 000);
   without `--fail`, HTTP error statuses do not themselves make curl exit nonzero,
   so inspect the printed status/body. Do not substitute an insecure TLS flag.
3. Inspect each 3xx destination locally: it must stay on the exact same HTTPS
   origin, never HTTP, loopback, container names or another host. Repeat the first
   command with that destination until reaching HTTP 200 with no redirect. Do not
   follow an unexpected origin or submit credentials there. Open `/api/` in Safari
   and confirm the displayed Google Reader API address uses the same HTTPS origin.
4. Share only endpoint paths, status codes and same-origin HTTPS yes/no; no full
   origins, request identifiers, authentication bodies, headers or network exports.

### Observed remote results after the PR #18 review

The user confirmed the Air's fresh wrong-API-password rejection and subsequent
normal valid-account synchronization; no remote authentication HTTP status was
captured. The Air's curl requests used trusted TLS, no proxy and no redirect
following; the user supplied these sanitized results:

| Check | Observed result |
| --- | --- |
| `/api/greader.php/check/compatibility` with dummy Authorization header | `PASS`, HTTP 200, no redirect |
| `/` | HTTP 302 to the same HTTPS origin at `/i/?rid=<REQUEST-ID>` |
| That exact `/i/` redirect destination | HTTP 200, no further redirect |
| `/api/` | HTTP 200, no redirect |
| Browser `/api/` diagnostics and displayed API addresses | Google Reader PASS; API links use the same HTTPS origin, including `/api/greader.php` |

This closes the review's remote invalid-credential and redirect/origin evidence
gap. It does not claim the mini's own HTTPS failure is fixed, prove rejection of a
real injected cross-origin redirect, or repeat earlier stopped-client/read-star
checks. Those earlier device observations remain historical evidence below.

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

Explicit `backup` attempts to restore prior application/forwarding state after
quiescing, even on snapshot failure and regardless of the current opt-in setting.
A failed explicit backup is not proof remote access is closed. Shutdown export
failure instead leaves writers/forwarding stopped and the VM running; preflight
refusals may leave existing serving state untouched.

## Sanitized local checkpoint

- **PR #18 local review fixes, after `62626a8`:** full default baseline passed
  **329 tests in 81.61s**, six live deselected, Ruff lint/format clean. Foreground
  conflicts, concurrent mutations and backup-state regressions passed; the new
  LocalAPI adapter has wire-level conditional-write/redaction/failure coverage.
  Read-only LocalAPI GET/ETag discovery passed on the existing daemon with no
  mutations. Real conditional writes and live lifecycle checks were not run during
  implementation. The user subsequently completed the remote invalid-credential
  and redirect/origin checks above. The older device/runtime results below predate
  these fixes and were not repeated.
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
