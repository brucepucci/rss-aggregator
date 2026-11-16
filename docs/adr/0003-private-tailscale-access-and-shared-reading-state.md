# Use private Tailscale HTTPS access to shared FreshRSS reading state

Status: Accepted; private forwarding/API commands delivered by issue #3.
Human-assisted HTTPS/ReadKit state and restart checks passed on macOS and iOS;
see the [device evidence](../tailscale-readkit.md#historical-device-evidence-and-limits) for evidence and limits,
including the mini's unresolved self-HTTPS diagnostic failure.

ReadKit on macOS and iOS must share one FreshRSS user's subscriptions and reading state, and all devices already use Tailscale. Keep the application listener on Mac loopback and use Tailscale Serve for private HTTPS access rather than a public endpoint or direct LAN exposure. Use ReadKit's FreshRSS integration through the Google Reader-compatible API, with a separate API password rather than Google credentials or Fever by default.

## Considered options

- Loopback-only access would not let the iOS reader reach the Mac's service.
- Direct LAN access would require a broader application listener and tie access to the local network rather than the existing tailnet.
- Public hosting or Tailscale Funnel would add exposure unnecessary for this development milestone.
- Separate reader-local subscriptions would not establish the shared server-side reading workflow we need to validate.

## Consequences

- Tailnet access controls and trusted HTTPS supplement FreshRSS authentication rather than replacing it. Keep web login authenticated and enter the separate per-user API password locally.
- Verify actual certificate issuance, client access, reverse-proxy behavior, and both ReadKit clients; planning-time connectivity is not end-to-end verification. Certificate hostnames may appear in public Certificate Transparency logs even though the service remains private.
- Start/stop manages only project-owned Serve forwarding. Preserve existing routes and the pre-existing Tailscale installation, and obtain approval for any account-wide changes. The initial implementation opts in with `RSS_TAILSCALE=1`, requires exclusive ownership of the node's HTTPS port 443, and refuses shared/changed listeners rather than adopting or globally resetting them. Foreground-session listeners are subject to the same conflict checks before application changes; public Funnel routes reaching this backend are refused on any port, resolving node/foreground authorization and handler precedence independently and expanding supported proxy targets, while keeping virtual-service namespaces separate and preserving genuinely unrelated services.
- Private and previously private application startup and its PHP/readiness/scheduler/fetching/authentication/forwarding gates share ownership-checked failure cleanup, without changing foreign forwarding. This includes failures before the authenticated-access gate runs and access-gate failure during loopback fallback of a previously private instance; preflight and first-run unconfigured loopback behavior remain unchanged. Preserved private other-port routes must not retain access to a backend whose authentication gate failed. If cleanup cannot be confirmed, report possible remaining exposure explicitly.
- Forwarding changes use the ownership-validated LocalAPI configuration's ETag in a conditional write, refusing concurrent changes rather than relying on the project lock to serialize other Tailscale clients.
- Definitively rejected starts (including authentication/permission refusals, ETag conflicts, and connection failures before transmission) retire new intent before fallible restoration of preceding intent, rather than granting new ownership of competing routes. Restoration failure leaves no new authority and requires local storage/ownership inspection. Establish the TCP/Unix connection separately from sending; send/response failures remain uncertain regardless of exception type, and uncertain starts retain recovery intent. Removal persists inactive intent before the remote request so persistence failure prevents mutation; a definite refusal restores preceding intent. Confirmed or uncertain removal stays inactive for local inspection, preventing automatic adoption of a possibly recreated identical foreign listener even when verification fails. Neither case triggers remote rollback.
- Explicit backup temporarily removes forwarding and attempts to restore the observed prior application/forwarding mode even on snapshot failure, regardless of the current opt-in environment.
- FreshRSS holds shared reading state. Validate read/unread and star/unstar changes in both directions between both clients and the instance; merely displaying articles is insufficient.
- Keep feed fetching separate from client synchronization. FreshRSS fetches on startup and periodically while running; refreshing ReadKit is not assumed to fetch new publisher content.
- Private feed URLs are credentials, even when they look like ordinary links. Enter the paid MacStories feed locally, keep it out of Git and shared evidence, and treat backups containing it as sensitive.
- The Mac development instance is unavailable when stopped. Always-on Linux hosting, Feedbin OPML import, and public access remain deferred.

Implementation and verification: [Tailscale and ReadKit](https://github.com/brucepucci/rss-aggregator/issues/3) and [paid MacStories feed](https://github.com/brucepucci/rss-aggregator/issues/4).
