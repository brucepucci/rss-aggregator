# Use private Tailscale HTTPS access to shared FreshRSS reading state

Status: Accepted and retained by [ADR 0004](0004-run-pinned-freshrss-natively-on-apple-silicon.md). Earlier macOS/iOS ReadKit evidence belongs to the legacy deployment, not native acceptance.

ReadKit on macOS and iOS should share one FreshRSS user's subscriptions and reading state across networks. Keep the application on Mac loopback and use optional Tailscale Serve HTTPS rather than public hosting, Funnel or a direct LAN listener. ReadKit uses the Google Reader-compatible FreshRSS API, with a separate API password rather than Google credentials or separate reader-local subscriptions.

## Consequences

- Tailnet access controls and trusted HTTPS supplement FreshRSS authentication. Keep web login authenticated and enter the per-user API password locally. Private feed URLs remain credentials, including in backups; certificate hostnames can appear in public Certificate Transparency logs even when access is private.
- Publish only after security, readiness, authentication and scheduler checks. Manage only the exact owned Serve route with conditional changes; refuse conflicts and preserve foreign routes. Backup withdrawal and resumption are constrained by ownership and intent. See [forwarding internals](../architecture.md#private-https-and-authentication).
- Feed fetching and client synchronization are different operations. A local API or TLS diagnostic is not evidence of shared reading state: check both actual clients and read/unread/star/unstar changes in both directions. See the [device procedure](../tailscale-readkit.md) and [verification limits](../development.md#verification-and-adoption-status).
