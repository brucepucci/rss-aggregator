# Private Tailscale HTTPS and ReadKit

Use this after [local setup](../README.md#getting-started). For an existing Podman
instance, follow the [owner-assisted migration window](operations.md#one-time-migration-from-podman)
before changing the live route. Keep names, addresses, passwords, tokens and
private feed URLs local. See the [CLI reference](cli.md) and [configuration](configuration.md).

## Prerequisites and ownership

1. Use the existing Tailscale installation/session on the Mac and reader devices.
   The application listens only on `127.0.0.1`; use private **Serve**, never Funnel,
   public DNS/router forwarding or a LAN listener. Complete the FreshRSS installer
   on loopback with SQLite and form authentication before exposing it to the tailnet.
2. Inspect `tailscale serve status` locally, including any foreground Serve sessions
   or Funnel reaching this backend. The project requires exclusive ownership of
   node HTTPS port 443 and refuses an unowned/changed route even with an identical
   target. It does not adopt/reset unrelated routes. **For rehearsals use `RSS_TAILSCALE=0`
   and choose a port that is not a target of any known forwarding**, including
   unrelated Serve/Funnel or another proxy. A free port or disabled project Serve
   alone does not mean a backend on that port cannot be reached.
3. MagicDNS and trusted HTTPS certificates must be enabled. Obtain approval for
   any tailnet-wide change; public Certificate Transparency logs may list certificate
   hostnames even though Serve is private. Grant only the intended devices TCP 443
   to the server as appropriate for IPv4/IPv6. Preserve other grants and do not open
   the loopback port. A policy preflight is not proof each device is authorized.

## Connect and verify the server

**Coordinate activation** with the owner. Use a stable synchronized checkout, the
same owned native `RSS_STATE_DIR` and `RSS_PORT`, and the approved private
`RSS_BACKUP_DIR`. A change of access mode also changes job settings: installed
definitions keep their captured values, which a later recovery or backup may use.

1. With the **old** settings, record both jobs' enabled states and capture, including
   the daily job's UTC hour/minute and destination if enabled. Run `uv run --locked
   rss backup` and confirm verification, previous serving mode, successful resumption
   and no maintenance hold. Retain the archive/checksum privately. Then use
   `supervision-remove` and `backup-disable` to remove any installed owned jobs.
   Verify both are disabled; do not proceed on partial removal, contention or an
   unclear state. See [job recapture safeguards](operations.md#upgrade-installed-mac-jobs-safely).
2. Keep the same state/port/destination. Only after the backup and job removal:

   ```sh
   export RSS_TAILSCALE=1
   uv run --locked rss start &&
     uv run --locked rss verify &&
     uv run --locked rss status
   ```

   Require successful guarded startup and `verify`, the intended HTTPS `url`,
   `http_ready: true` and owned `forwarding.status: "running"`. `verify` briefly
   interrupts service. Inspect `tailscale serve status` locally and confirm only
   the intended route changed. Failed checks do not authorize forwarding or
   bypassing authentication; inspect any uncertain cleanup before retrying.
3. Open the exact returned HTTPS origin in a browser and sign in with the web
   password. Private startup requires form authentication, no anonymous reading
   or refresh, and enabled API access; tailnet identity does not replace login.
   In FreshRSS **Profile** set or confirm a strong, **separate API password**. In
   migration reuse the preserved credential unless the owner explicitly rotates it.
   Check `<HTTPS-ORIGIN>/api/`: diagnostics, redirects and API links must use the
   same trusted origin, not loopback or plain HTTP.
4. Run `uv run --locked rss api-check`; enter the FreshRSS username and the separate
   API password only at the local prompts (password hidden). It sends credentials
   by POST over trusted TLS, without ambient proxies or redirect following. Expect
   sanitized JSON with `diagnostics: "PASS"`, `authenticated: true`,
   `transport: "https"`, `https_verified: true` and subscription/sample counts. The API sample
   is at most one article, not the total. In a coordinated fresh check a deliberately
   wrong password should fail nonzero without exposing secrets or raw response data.

### Supplemental loopback diagnostic

`uv run --locked rss api-check --loopback` checks the verified owned local HTTP
backend with the same local prompts. It reports `transport: "loopback"` and
`https_verified: false`. There is no automatic HTTPS-to-HTTP fallback; this proves
neither trusted TLS nor remote reachability and is not a ReadKit test. If the Mac
cannot reach its own HTTPS origin, continue owner-approved checks from a trusted
remote device rather than weakening TLS or changing unrelated policy.

## Connect ReadKit

On both **actual macOS and iOS** clients choose **FreshRSS** (Google Reader-compatible
API), not Google credentials or Fever. Enter the **HTTPS origin without
`/api/greader.php`**, the same FreshRSS user, and its separate API password. The
underlying API path is `/api/greader.php`; it is not the ReadKit account URL.
During cutover preserve the same origin, user and credentials so existing readers
need no reconfiguration.

Synchronize both clients and check subscriptions and articles. Change one article's
read/unread and star/unstar markings in each client, synchronizing both and
reloading FreshRSS web after each change. Confirm both destinations reflect every
change. Check a fresh authentication attempt (cached synchronization alone is
insufficient) and require trusted TLS without warnings. Feed fetching is separate
from client synchronization. See [FreshRSS mobile access](https://freshrss.github.io/FreshRSS/en/users/06_Mobile_access.html)
and the [ReadKit FAQ](https://readkit.app/faq) for product details.

## Optionally recapture the Mac jobs

Leave jobs disabled for manual use. Only after successful security and private
browser/API/client checks, explicitly install **only** wanted jobs in the user
session with the reviewed state, port, backup destination, Tailscale dependency
and `RSS_TAILSCALE=1`. Use [supervision](operations.md#enable-or-remove-supervision)
and [daily backup setup](backup-restore.md#opt-in-daily-mac-backups) separately.
For daily backups reuse the saved UTC hour/minute, not necessarily defaults.
Inspect the actual capture and health. Registration, a manual backup or manual
`backup-due` is not evidence of unattended recovery/protection. Recapture changes
the policy evidence needed to claim protection.

## Stop, resume and return to local-only use

`stop` withdraws matching owned forwarding and verifies a shutdown archive. The
Tailscale installation/session and unrelated routes remain. When the instance is
stopped, cached reader articles are not evidence of live availability. `backup`
attempts to restore the previous serving mode when safe even on snapshot failure;
do not infer stopped service from failure. See [backup failure behavior](backup-restore.md#consistency-and-failure-behavior).

For a deliberate transition to loopback, coordinate with the owner and take a
verified backup using the current settings. Successfully remove supervision and
disable daily backup before changing **either** captured mode. Keep the same state,
port and backup destination; set `RSS_TAILSCALE=0`, then `start`, `verify` and
`status`. Require the intended HTTP loopback URL, readiness and owned forwarding
stopped (or unconfigured if never managed). Check the actual Serve state locally;
verify local sign-in and loopback links. Retained API passwords/ownership receipts
are not erased. Retain Tailscale availability when checking existing forwarding
records; never delete receipts to avoid those checks. Only recapture wanted jobs
with the new settings after verification.

## Troubleshooting

- **Certificate or policy failure:** check existing Tailscale sessions, MagicDNS,
  HTTPS approval, clocks/trust and grants from the actual reader. No `curl -k`,
  self-signed bypass, Funnel or speculative daemon restart/upgrade.
- **Foreign or uncertain route:** inspect actual listeners and forwarding locally.
  Conditional writes may refuse a concurrent change; there is no global reset,
  automatic rollback or permission to adopt a route on uncertain ownership. Do
  not edit `serve.json`; a failed operation is not proof access is closed.
- **PHP, readiness or authentication refusal:** resolve the cause locally before
  exposing access. Verify the instance, base URL, form authentication and separate
  API password. A wrong base URL can cause a redirect. Treat a failed confirmation
  of writer shutdown as possible continuing access, not successful cleanup.

## Real-HTTPS checks — human-assisted

Use an approved reader device. Establish fresh login and rejection of deliberately
wrong API credentials without disturbing the existing valid reader. For a
credential-free Google Reader compatibility check on that device:

```sh
curl -q -sS --noproxy '*' --proto '=https' --max-redirs 0 \
  --header 'Authorization: GoogleLogin auth=test/1' \
  --write-out '\nHTTP %{http_code}\n' \
  '<HTTPS-ORIGIN>/api/greader.php/check/compatibility'
```

Replace the origin locally. The header is **dummy data**, not a token. Expect
`PASS` and HTTP 200; HTTP errors may still produce a zero curl exit here, so read
the status. Redirects are not followed and TLS verification stays enabled. Inspect
the origin of any browser redirect before entering credentials. Report only
sanitized reachability, status and same-origin/marking outcomes, never raw
requests/responses, credentials or network captures.

### Historical device evidence and limits

The [legacy device checks](https://github.com/brucepucci/rss-aggregator/blob/28664ab936f8872da6ff3ddca1c4b0a725dfb0ec/docs/tailscale-readkit.md#historical-device-evidence-and-limits)
confirmed origin-only configuration and bi-directional marking changes on a Mac
and iPhone, with a server self-HTTPS diagnostic limitation. Those were Podman-era
results, not current native evidence. Both native ReadKit clients, trusted remote
HTTPS and post-cutover state are open owner-assisted checks for
[#30](https://github.com/brucepucci/rss-aggregator/issues/30). Synthetic loopback
or API tests do not pass them.
