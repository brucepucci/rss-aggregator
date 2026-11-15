# Isolate local development in a dedicated Podman machine

Status: Accepted. Initial local implementation: issue #1; see README's [verification and adoption status](../../README.md#verification-and-adoption-status) for historical evidence, current scope and acceptance limits.

We prefer development on the Mac but want to avoid installing FreshRSS's PHP runtime and database dependencies directly on it. Use the official FreshRSS container in a dedicated rootless Podman machine, accepting the Linux VM's resource cost in exchange for an isolated runtime and an explicit removal boundary. The repository owns reproducible configuration and lifecycle commands, not a fork of FreshRSS or a custom application image.

## Considered options

- Native Homebrew PHP was the initial choice, but Homebrew can upgrade shared dependencies and leave configuration behind; package removal is not a pristine rollback guarantee.
- Development on the available Linux desktop would keep application dependencies off the Mac, but would sacrifice the preferred local Mac workflow.
- A dedicated Podman VM keeps application dependencies isolated while allowing explicit start/stop during development.

## Consequences

- Install the Podman CLI without Desktop, automatic login startup, or a privileged host helper. Record host-package additions and pause before upgrading pre-existing packages; the container approach still has a host-tool footprint.
- Explicitly target the project-owned machine and connection. Preserve unrelated Podman resources and pre-existing host dependencies; cleanup must account for dependencies subsequently adopted elsewhere.
- Stop the application and its writers before stopping the VM. No always-on development availability is required, and guest-free RAM is not assumed to return automatically to macOS while the VM runs.
- Pin the official image by verified digest and record its version and platform. Verify actual PHP security fixes and effective web-server configuration before adding credentials or enabling client access; container isolation and package metadata alone are not security verification. The initial implementation preserves the official entrypoint/default command and keeps Apache on an internal-only listener until its effective PHP check passes and the temporary diagnostic is removed.
- Linux home-server deployment remains a later phase. Repeatability here does not imply that an architecture-specific image or the Mac VM can be copied unchanged to another host.

Implementation and verification: [local runtime](https://github.com/brucepucci/rss-aggregator/issues/1) and [safe removal and recreation](https://github.com/brucepucci/rss-aggregator/issues/5).
