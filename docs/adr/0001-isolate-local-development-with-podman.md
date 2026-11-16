# Isolate local development in a dedicated Podman machine

Status: Accepted. Initial local implementation: issue #1; see [developer verification and adoption status](../development.md#verification-and-adoption-status) for historical evidence, current scope and acceptance limits.

We prefer development on the Mac but want to avoid installing FreshRSS's PHP runtime and database dependencies directly on it. Use the official FreshRSS container in a dedicated rootless Podman machine, accepting the Linux VM's resource cost in exchange for an isolated runtime and an explicit removal boundary. The repository owns reproducible configuration and lifecycle commands, not a fork of FreshRSS or a custom application image.

## Considered options

- Native Homebrew PHP was the initial choice, but Homebrew can upgrade shared dependencies and leave configuration behind; package removal is not a pristine rollback guarantee.
- Development on the available Linux desktop would keep application dependencies off the Mac, but would sacrifice the preferred local Mac workflow.
- A dedicated Podman VM keeps application dependencies isolated while allowing explicit start/stop during development.

## Consequences

- Install the Podman CLI without Desktop, implicit login startup, or a privileged host helper. Record host-package additions and pause before upgrading pre-existing packages; the container approach still has a host-tool footprint.
- Explicitly target the project-owned machine and connection. Preserve unrelated Podman resources and pre-existing host dependencies; cleanup must account for dependencies subsequently adopted elsewhere.
- Stop the application and its writers before stopping the VM. Manual development remains supported with no always-on availability requirement, and guest-free RAM is not assumed to return automatically to macOS while the VM runs.
- Issue #22 extends the initial session-only decision with **explicit opt-in normal-user-session launchd supervision**, not a privileged/pre-unlock service. Persisted explicit stop survives ticks/login/reboot until explicit start; stop records intent before maintenance refusal, while start records running intent only after its maintenance guard. Temporary backup quiescing is not operator stop. Observe health before recovery, serialize with lifecycle/maintenance, and bound retries through the existing ownership/security gates. Capture one stable checkout and non-secret environment rather than relying on an interactive shell. Daily backup scheduling is a separate opt-in user job, not authorization for application supervision. Any durable maintenance hold blocks writer recovery; clearing it requires explicit ownership/idle reconciliation without starting, though retained running intent permits a later tick. No automatic login, FileVault or power-setting changes are authorized; actual reboot/login limitations require owner-assisted evidence and acceptance. See [operations](../operations.md#opt-in-mac-startup-and-recovery).
- Pin the official image by verified digest and record its version and platform. Verify actual PHP security fixes and effective web-server configuration before adding credentials or enabling client access; container isolation and package metadata alone are not security verification. The initial implementation preserves the official entrypoint/default command and keeps Apache on an internal-only listener until its effective PHP check passes and the temporary diagnostic is removed.
- Linux home-server deployment remains a later phase. Repeatability here does not imply that an architecture-specific image or the Mac VM can be copied unchanged to another host.

Implementation and verification: [local runtime](https://github.com/brucepucci/rss-aggregator/issues/1) and [safe removal and recreation](https://github.com/brucepucci/rss-aggregator/issues/5). Opt-in session startup/recovery: [issue #22](https://github.com/brucepucci/rss-aggregator/issues/22); controlled tests are not real reboot acceptance.
