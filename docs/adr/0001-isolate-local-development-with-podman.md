# Isolate local development in a dedicated Podman machine

Status: Superseded for this deployment by [ADR 0004](0004-run-pinned-freshrss-natively-on-apple-silicon.md).

The initial Mac deployment used the official FreshRSS container in a dedicated rootless Podman machine rather than installing shared Homebrew PHP. This accepted the Linux guest's resource cost in exchange for isolation, reproducible image identity and an explicit removal boundary. The repository managed lifecycle and configuration, not a FreshRSS fork or custom application image.

Native deployment changes the isolation boundary, not the obligation to preserve unrelated resources. The old VM is not a current copy after native writes begin. Keep legacy state intact until the owner-assisted migration and recovery gates pass. The [legacy architecture and safeguards](https://github.com/brucepucci/rss-aggregator/blob/28664ab936f8872da6ff3ddca1c4b0a725dfb0ec/docs/architecture.md) and [guarded removal procedure](https://github.com/brucepucci/rss-aggregator/blob/28664ab936f8872da6ff3ddca1c4b0a725dfb0ec/docs/operations.md#preview-and-remove-the-vm) remain at the legacy revision; the native CLI does not remove a VM.
