# Private Tailscale HTTPS and ReadKit

Operator runbook for connecting devices to the Mac instance. Start with the
[local installation](../README.md#getting-started). Exact syntax/results are in
[CLI reference](cli.md), settings in [configuration](configuration.md), and
forwarding ownership internals in [architecture](architecture.md#private-https-and-authentication).
Hostnames, addresses, usernames, passwords, tokens and private feed URLs must stay
local; use placeholders in shared evidence.

## Prerequisites and ownership

1. Use the existing Tailscale installation/session on the server Mac and readers.
   Do not install another copy or stop Tailscale with the application.
2. Complete the loopback FreshRSS installer with SQLite and form authentication.
   PHP security gates must pass before entering credentials.
3. Inspect `tailscale serve status` locally. The project requires exclusive
   ownership of the node's HTTPS port 443; an unowned route, another handler/subpath,
   foreground listener or Funnel causes refusal, even with an identical target.
   Public Funnel routes reaching this backend on other ports are also refused.
   Foreign routes are preserved; there is no reset/adoption/bypass flag.
4. MagicDNS and HTTPS certificates must already be enabled. Obtain approval for any
   tailnet-wide DNS/HTTPS/policy changes. Certificate hostnames appear in public
   Certificate Transparency logs even though Serve is private. Do not enable Funnel,
   public DNS or router forwarding.
5. Grant only intended reader devices TCP 443 to the server Mac, including IPv4/IPv6
   as appropriate. Preserve unrelated SSH/SMB/other grants and existing Funnel
   permissions; do not grant 8080, which remains loopback-only. Recheck after device
   changes. The command's policy preflight does not prove each device can connect.

Keep the same ownership directory and `RSS_PORT` for all operations. Another
worktree must explicitly set `RSS_STATE_DIR` to the original private directory;
never copy or fabricate `owner.json`/`serve.json` to adopt resources.

## Connect and verify the server

Keep a private external `RSS_BACKUP_DIR` configured for backup/shutdown.

```sh
export RSS_TAILSCALE=1
./scripts/rss start
./scripts/rss status
```

Use the private HTTPS `url` returned locally by `start`. Open it and sign in with
the web password. In **Profile**, set a strong, separate **API password**. Enter
both only locally, never in command arguments, Git, chat or screenshots. Private
start enables API access and enforces form login/no anonymous reading or refresh;
existing users, subscriptions and reading state are retained.

Open `<HTTPS-ORIGIN>/api/` and check Google Reader diagnostics. Links, redirects and
API addresses must use that same HTTPS origin, not HTTP, loopback or container
names. Tailscale identity does not replace FreshRSS authentication.

```sh
./scripts/rss api-check
```

Enter the FreshRSS username and API password at local stderr prompts; the password
is hidden. Success is sanitized JSON: `diagnostics: "PASS"`, `authenticated: true`,
subscription/sample-article counts, `transport: "https"`, `https_verified: true`.
The sample requests at most one article, not a total article count. Try an
intentionally incorrect API password separately: expect exit 1 and sanitized
rejection without JSON. Then repeat with valid credentials.

The diagnostic uses POST for credentials, trusted TLS, no ambient HTTP proxy and
no redirects; never publish raw responses, HTML, tokens or tracing. It requires
an owned running app with its matching security audit and active owned forwarding.

### Supplemental loopback diagnostic

The historically tested server Mac (mini) cannot complete its own HTTPS-origin
requests during TLS, despite successful remote clients. This limitation remains
unresolved. The explicit supplemental authentication check is:

```sh
./scripts/rss api-check --loopback
```

It verifies the actual sole owned IPv4 loopback publication and retains the running,
security-checked app and active-forwarding requirements. It reports
`transport: "loopback"`, `https_verified: false`. There is no implicit HTTPS-to-HTTP
fallback. **This does not verify certificates or remote access.** Continue actual
remote HTTPS/client checks; do not broaden policy, disable TLS verification or
restart/upgrade Tailscale speculatively.

## Connect ReadKit

On both macOS and iOS, choose **FreshRSS** (Google Reader-compatible API), not Google
credentials or Fever. Enter **`<HTTPS-ORIGIN>` unchanged, without `/api/greader.php`**,
the same FreshRSS user and its separate API password. This URL form was verified
in actual ReadKit on the reader Mac (Air) and iPhone; exact app/OS versions were
not recorded. The underlying API endpoint is `/api/greader.php`, not the account
URL used in those checks.

Synchronize both clients and confirm the public feed and articles. Change one
article's read/unread and star/unstar state in each client, synchronizing both and
reloading FreshRSS web after each change. Confirm both destinations reflect it.
No certificate-warning bypass should be needed. Other reader integrations are not
verified here; consult [FreshRSS mobile access](https://freshrss.github.io/FreshRSS/en/users/06_Mobile_access.html)
and the [ReadKit FAQ](https://readkit.app/faq).

## Stop, resume and return to local-only use

Normal `stop` removes only matching project-owned forwarding, creates a verified
external backup and stops the VM. Repeated stop also removes a stale owned route
when the VM is already stopped. Tailscale stays connected, but stopped FreshRSS
cannot synchronize; cached articles in readers are not live service availability.
Keep `RSS_TAILSCALE=1` for later `start`/`verify`/`replace`.

Explicit `backup` temporarily removes forwarding and attempts to restore the
observed prior application/forwarding state **even if snapshot creation fails**,
regardless of this shell's opt-in. A previously private service resumes privately,
and loopback-only remains loopback-only. Failure therefore does not prove access
is closed. Shutdown export failure after quiescing instead leaves writers and
forwarding stopped with the VM running. Preflight refusal can leave existing
serving state untouched; failed resumption can leave the application stopped.
See [backup failure behavior](backup-restore.md#consistency-and-failure-behavior).

To deliberately return to local-only browser operation, **remove owned supervision
before changing mode**. An installed job retains its captured `RSS_TAILSCALE=1`;
changing this shell alone can let the next recovery tick reopen forwarding.

1. Coordinate the change with the owner and obtain a verified private instance
   backup, following the [supervision safeguards](operations.md#enable-or-remove-supervision).
   Use the same synchronized stable controlling checkout, existing `RSS_STATE_DIR`
   and `RSS_PORT` throughout, including after stop. From another worktree, explicitly
   export the original ownership directory; never copy or fabricate its records.
   With the existing mode still selected:

   ```sh
   ./scripts/rss supervision-status
   ./scripts/rss supervision-remove
   ```

   Continue only after removal succeeds with `enabled: false`, `state: "disabled"`.
   Removal is repeatable if absent and does not stop FreshRSS or Tailscale, erase
   intent or delete data/backups. On contention or changed/unowned-job refusal,
   resolve it locally before continuing; never hand-edit or delete managed records/jobs.
2. Keep those checkout/ownership/port selectors unchanged, then apply the new mode
   through the guarded startup and verification path:

   ```sh
   export RSS_TAILSCALE=0
   ./scripts/rss start && ./scripts/rss verify && ./scripts/rss status
   ```

   Require successful results: `start`/`verify` return
   `url: "http://127.0.0.1:<RSS_PORT>"`, and `status` reports the application running,
   `http_ready: true` and `forwarding.status: "stopped"` (or `"unconfigured"` if never recorded).
   Inspect `tailscale serve status` locally to confirm the owned 443 route is gone,
   preserving unrelated routes. Open the returned loopback URL and confirm sign-in
   and loopback links/redirects. Recorded owned forwarding is removed and the
   installed instance's base URL returns to loopback; API enablement and API
   passwords are not erased. A failed command is not proof access is closed;
   inspect locally using the [failure safeguards](#troubleshooting) before retrying.
3. Leave supervision removed for manual operation. If automatic recovery is still
   wanted, explicitly reinstall only after successful loopback verification, from
   the same stable checkout with `RSS_TAILSCALE=0` and all intended settings exported:

   ```sh
   ./scripts/rss supervision-install && ./scripts/rss supervision-status
   ```

   Follow the [captured-configuration instructions](configuration.md#captured-mac-supervision-configuration)
   and [installation safeguards](operations.md#enable-or-remove-supervision).
   Reinstallation captures the intended settings; it is not a live recovery test.

## Troubleshooting

- **Prerequisite/policy failure:** inspect the existing Tailscale session; have an
  administrator approve DNS/HTTPS and a narrow incoming TCP 443 grant. The coarse
  preflight is not proof of authorization from each actual device.
- **Ownership/443/public-backend conflict:** inspect `tailscale serve status`
  locally, including Funnel targets on other ports and foreground listeners.
  Preserve foreign configuration; never run `serve reset`, hand-edit ownership
  records or enable Funnel as a workaround. `stop` refuses changed routes.
- **Concurrent or uncertain forwarding write:** conditional updates refuse races,
  without automatic retry/rollback. Resolve ownership and availability before
  retrying. Definitively rejected starts grant no new authority; inactive removal
  records do not authorize adopting a possibly recreated identical listener.
  A verification error after removal does not prove the whole shutdown completed.
- **Late startup failure:** private/previously private post-start gate failures
  attempt to stop the ownership-checked backend, preserving foreign routes/data/VM.
  If the error warns cleanup could not be confirmed, the backend may still be
  exposed; inspect and close it locally before retrying. No backup destination is
  needed for this safety cleanup. [Failure boundaries](architecture.md#post-start-failure-cleanup).
- **Certificate/access failure:** check Tailscale on both devices, MagicDNS, HTTPS
  authorization, clocks/trust stores and local-network permissions if prompted.
  No shared LAN is required. Never use self-signed-certificate bypasses or `curl -k`.
- **API/authentication failure:** use the separate API password, confirm API access
  under Authentication and check `/api/`. A redirect often means a wrong base URL.
  Direct compatibility checks require the dummy header below; address-bar navigation
  omits it and can report an Authorization-header failure.

## Real-HTTPS checks — human-assisted

These optional diagnostic checks use a working remote device, not the mini's
failing self-HTTPS path. Loopback HTTP 401 and isolated tests are not HTTPS evidence.
Do not weaken TLS, change policy or publish raw requests/responses.

1. Begin a fresh ReadKit FreshRSS connection check with the existing origin/user
   and an intentionally wrong API password. Cached synchronization is not a fresh
   authentication check. Confirm rejection, cancel the extra setup and confirm
   the existing valid account still synchronizes. Record only the outcome.
2. On the reader Mac, replace `<HTTPS-ORIGIN>` locally and run credential-free GETs:

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
   `--noproxy '*'` bypasses proxies; `--proto '=https'` permits HTTPS only.
   TLS verification stays enabled. No `--location` is used; redirects are not
   followed (`--max-redirs 0`). `-o /dev/null` discards page content; `-w`/`--write-out`
   print status/redirect information. The compatibility check prints its diagnostic
   body; its header is **dummy data**, not credentials. Expect `PASS`/HTTP 200.
   Misspelled `/check/compatability` returns HTTP 400. Transport/TLS errors exit
   nonzero (e.g. code 35/HTTP 000); without `--fail`, HTTP errors require inspecting
   the printed status/body. Never substitute an insecure TLS flag.
3. Inspect every 3xx destination locally: it must remain the same HTTPS origin.
   Repeat the first command with that destination until HTTP 200/no redirect.
   Do not follow an unexpected origin or submit credentials there. Open `/api/`
   in Safari and check displayed Google Reader API addresses.
4. Share only endpoint paths, status codes and same-origin HTTPS yes/no, not full
   URLs, identifiers, credentials, headers or network exports.

### Historical device evidence and limits

Human-assisted issue #3 / [PR #18](https://github.com/brucepucci/rss-aggregator/pull/18)
checks established the following, not a new run for this documentation change:

| Check | Observed result |
| --- | --- |
| Actual ReadKit on Air and iPhone | Origin-only account URL; public feed visible; no certificate bypass |
| Air → iPhone and FreshRSS web | Read/unread and star/unstar changes passed |
| iPhone → Air and FreshRSS web | Read/unread and star/unstar changes passed |
| Normal stop/start | Both clients unable to sync while stopped; subscriptions/articles/read-star state retained after start; Tailscale stayed connected |
| Air wrong API password | Rejected on fresh connection check; valid account still synchronized; HTTP status not captured |
| Air `/api/greader.php/check/compatibility` with dummy header | `PASS`, HTTP 200, no redirect |
| Air `/` | HTTP 302 to same HTTPS origin `/i/?rid=<REQUEST-ID>`, then HTTP 200/no further redirect |
| Air `/api/` and browser diagnostics | HTTP 200/no redirect; Google Reader PASS; API links on same HTTPS origin |
| Mini explicit loopback API check | PASS/authenticated with valid credentials; invalid credentials HTTP 401; not HTTPS evidence |

State propagation was roughly 30 seconds, not a guarantee. Optional web-originated
edits, exact app/OS versions, real certificate/policy-failure injection and real
cross-origin-redirect injection were not recorded. The mini's self-HTTPS failure
remains unresolved (historical CLI 1.104.7 versus daemon 1.104.2; no speculative
upgrade/restart). The original Serve configuration was empty: isolated preservation
checks are not proof about real unrelated routes. Verification backups were
private temporary files, not durable backup configuration. No current running state
is implied by these historical results. See [developer verification scope](development.md#verification-and-adoption-status)
for automated versus live/platform coverage.
