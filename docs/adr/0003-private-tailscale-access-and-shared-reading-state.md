# Use private Tailscale HTTPS access to shared FreshRSS reading state

Status: Accepted; implementation pending.

ReadKit on macOS and iOS must share one FreshRSS user's subscriptions and reading state, and all devices already use Tailscale. Keep the application listener on Mac loopback and use Tailscale Serve for private HTTPS access rather than a public endpoint or direct LAN exposure. Use ReadKit's FreshRSS integration through the Google Reader-compatible API, with a separate API password rather than Google credentials or Fever by default.

## Considered options

- Loopback-only access would not let the iOS reader reach the Mac's service.
- Direct LAN access would require a broader application listener and tie access to the local network rather than the existing tailnet.
- Public hosting or Tailscale Funnel would add exposure unnecessary for this development milestone.
- Separate reader-local subscriptions would not establish the shared server-side reading workflow we need to validate.

## Consequences

- Tailnet access controls and trusted HTTPS supplement FreshRSS authentication rather than replacing it. Keep web login authenticated and enter the separate per-user API password locally.
- Verify actual certificate issuance, client access, reverse-proxy behavior, and both ReadKit clients; planning-time connectivity is not end-to-end verification. Certificate hostnames may appear in public Certificate Transparency logs even though the service remains private.
- Start/stop manages only project-owned Serve forwarding. Preserve existing routes and the pre-existing Tailscale installation, and obtain approval for any account-wide changes.
- FreshRSS holds shared reading state. Validate read/unread and star/unstar changes in both directions between both clients and the instance; merely displaying articles is insufficient.
- Keep feed fetching separate from client synchronization. FreshRSS fetches on startup and periodically while running; refreshing ReadKit is not assumed to fetch new publisher content.
- Private feed URLs are credentials, even when they look like ordinary links. Enter the paid MacStories feed locally, keep it out of Git and shared evidence, and treat backups containing it as sensitive.
- The Mac development instance is unavailable when stopped. Always-on Linux hosting, Feedbin OPML import, and public access remain deferred.

Implementation and verification: [Tailscale and ReadKit](https://github.com/brucepucci/rss-aggregator/issues/3) and [paid MacStories feed](https://github.com/brucepucci/rss-aggregator/issues/4).
