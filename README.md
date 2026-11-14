# Personal feed reading

A planned self-hosted FreshRSS instance for collecting feeds and sharing reading state across feed readers. See the [glossary](GLOSSARY.md) for domain terminology and the [architecture decisions](#architecture) for accepted constraints.

## Current status

**Main is documentation-only.** It has repository instructions, domain/architecture documentation, and the Python standard introduced by [#8](https://github.com/brucepucci/rss-aggregator/issues/8). It does not yet contain a runtime, Python project metadata or lockfile, a test suite, a development setup command, managed hooks, or GitHub Actions workflows.

Unmerged implementation work for [#1](https://github.com/brucepucci/rss-aggregator/issues/1) is not an available main-branch feature. This README deliberately does not copy its commands, configuration defaults, or verification claims into main. Inspect that branch's latest implementation and documentation when adopting the standard; do not discard existing work.

## Getting Started

### Prerequisites and installation

For the current documentation checkout, use Git and a Markdown reader. The GitHub CLI is optional for reading issues and required by the repository's [issue-tracker instructions](docs/agents/issue-tracker.md) for issue operations.

```sh
git clone --branch main https://github.com/brucepucci/rss-aggregator.git
cd rss-aggregator
```

These commands obtain main's documentation; they do not install or start FreshRSS. There is no application or Python-tooling installation command on main yet. Do not install FreshRSS PHP/database dependencies on the host. Planned runtime isolation and conservative host-package handling are defined in [ADR 0001](docs/adr/0001-isolate-local-development-with-podman.md).

### First-run verification

From the checkout:

```sh
git status --short --branch
git ls-tree -r --name-only HEAD
```

For a new clone, expect a clean `main` branch and documentation files, not a runnable service. Read the [Python standard](docs/python-standards.md) before Python work. These Git checks confirm checkout state only; they do not verify a FreshRSS instance, dependencies, or quality gates.

### Normal start and stop — planned

No start, stop, status, or feed-fetching commands are available on main. [#1](https://github.com/brucepucci/rss-aggregator/issues/1) owns the first local runtime slice. The accepted architecture uses the official FreshRSS container in a dedicated rootless Podman machine on the Mac, with explicit project ownership and no automatic login startup. Application writers must stop before the VM stops. These are design requirements, not a claim of implemented lifecycle behavior here.

### ReadKit connection — planned

No connection setup is available on main. [#3](https://github.com/brucepucci/rss-aggregator/issues/3) owns private Tailscale HTTPS access and ReadKit verification on macOS and iOS. [ADR 0003](docs/adr/0003-private-tailscale-access-and-shared-reading-state.md) selects the Google Reader-compatible API with a separate FreshRSS API password, not Google credentials. Tailnet access supplements FreshRSS authentication; it does not replace it. Actual device access and bidirectional reading-state synchronization remain to be demonstrated.

### Backup and restore — planned

No backup or restore commands are available on main. [#2](https://github.com/brucepucci/rss-aggregator/issues/2) owns complete, consistent instance backups outside both the VM and Git checkout, integrity verification, and restoration into a fresh selected volume. [ADR 0002](docs/adr/0002-portable-state-with-consistent-external-backups.md) defines the target shutdown/backup contract. A subscription export is not an instance backup and does not preserve articles or reading state. Do not treat a VM or a planned command as a recovery guarantee.

### Safe cleanup — planned

Main provides no resource-removal or host-package-uninstall commands. [#5](https://github.com/brucepucci/rss-aggregator/issues/5) owns guarded removal and recreation. Preserve unrelated machines, connections, volumes, packages, Tailscale routes, and external backups. VM removal requires a fresh successful, verified external backup; permanent data deletion is a separate, explicitly confirmed operation. Do not use blanket pruning or cleanup as a substitute for project ownership checks.

### Common troubleshooting

- **No runtime or setup command:** expected on main. Consult the relevant issue and the current branch's own documentation; do not guess commands from the future standard.
- **Python checks cannot run:** main has no Python environment, test entrypoint, Ruff configuration, or CI workflow. Adoption belongs to #9 below; this is not a passing test result.
- **Hooks do not run:** this change installs none. After adoption, follow the verified setup instructions, including rerunning setup after adding a remote and explicitly resolving existing hook arrangements.
- **Implementation exists in another checkout:** check `git status --short --branch` and that checkout's README. Main's documentation is not evidence that an unmerged implementation has passed acceptance.
- **Credentials or private feed URLs are requested:** main has no consumer for them. Keep them out of issues, Git, command examples, logs, and shared verification evidence.

## Configuration

### Supported settings and defaults

**There are currently no supported application settings, environment variables, configuration files, or Python-tooling settings on main.** No required or optional runtime values or defaults have been implemented here. The Python standard is a policy document, not executable configuration.

The following are planned areas, not supported settings or instructions to create files:

| Area | Intended purpose | Current values/defaults and status |
| --- | --- | --- |
| Runtime ownership, image, listener, fetching | Isolated instance lifecycle and feed fetching | Not available on main; #1 must document actual settings, defaults, and required/optional values. |
| State and external backups | Durable articles/reading state and recoverability | Not available on main; #2 must document backup destinations and recovery settings. |
| Private access and API authentication | Tailscale HTTPS and ReadKit client synchronization | Not available on main; #3 must document local configuration and authentication. |
| Python compatibility and dependencies | Reproducible development and command execution | Not declared on main; #9 must supply metadata, development dependencies, and an application lockfile. |
| Tests, lint, formatting, and hooks | Consistent local and CI checks | Not configured on main; #9 owns baseline adoption, coordinated with #7 integration coverage. |

### Changing and applying configuration

There is no application reload/apply procedure yet. Use the implementing branch's verified instructions for any branch-specific configuration; do not assume changes are supported on main. Each implementation must update this README with setting names, purposes, defaults, required versus optional values, and the steps to change and apply them. Preserve the accepted architecture and existing resources when introducing configuration.

### Secrets and private values

Do not commit credentials, private feed URLs, or instance backups. Enter secrets locally only when an implemented workflow documents how to do so safely; use placeholders such as `<api-password>` and `<private-feed-url>` in shared examples. Backups can contain credentials and private feed URLs: keep them outside Git with restrictive permissions. Permissions are not encryption. No secret files or secret-loading mechanism are configured by this change.

## Python development and adoption

The canonical, copy/paste-friendly policy is [docs/python-standards.md](docs/python-standards.md); [AGENTS.md](AGENTS.md) points agents to it. Repository-specific setup and exceptions belong here, not in the reusable policy.

### Approved rollout

1. Merge the documentation-only [#8](https://github.com/brucepucci/rss-aggregator/issues/8) change into main first. It has no blocker on #1's closure or merge.
2. Incorporate those standards into #1's existing implementation branch, preserving its work.
3. Implement [#9 — adoption/rewrite](https://github.com/brucepucci/rss-aggregator/issues/9) from that updated implementation branch and integrate it back into #1's branch, not independently into main.
4. Verify the combined #1/#9 implementation against both issues, then squash-merge it into main as **one implementation commit**, separately from this documentation change.

This is the approved integration plan, not authorization to publish or merge automatically.

### Enforcement status and exceptions

No Python policy exceptions are granted here. The absence of enforcement on main is an explicit **pending-adoption status**, not an implemented exemption or a certification of #1's branch. That branch's earlier unittest-style command tests, argparse CLI, and text output are branch-specific prior art, not alternative standards; #9 must inventory the latest state before migration.

There are **no working uv setup, pytest, Ruff, formatting/safe-fix, or hook-installation commands to publish for main yet**. #9 must document and verify them when implemented, including check-only gates, setup with/without a remote, rerunning setup after a remote is added, and preservation or explicit resolution of existing hooks. Hooks must cover pushes to any Git remote; they can be absent or bypassed and do not replace CI.

Host-side Python development tooling does not authorize installing FreshRSS's PHP/database runtime on the host, unexpected host-package upgrades, or changes to unrelated resources. All accepted ADRs remain in force; this change neither changes them nor certifies unmerged runtime behavior.

### Verification responsibilities

- **#8 (this specification):** check each policy and exemption against the canonical document, agent discovery, and repository-specific guidance. Verify current-main claims against its file inventory and commands. No new runtime test seam is needed, and this issue changes no production Python, tests, tooling configuration, hooks, or CI workflows.
- **#9 (baseline adoption):** supply uv-managed tooling, pytest, callable APIs with useful Click boundaries, production typing/docstrings, Ruff, pre-push gates, and matching GitHub Actions checks. The complete default suite must be deterministic and isolated, with no credentials, network, or production services. Preserve meaningful command-boundary success/failure coverage and prove checks reject controlled failures without rewriting files. Actual passing/failing local-hook and CI evidence belongs to adoption, not this specification.
- **[#7 (shared integration infrastructure)](https://github.com/brucepucci/rss-aggregator/issues/7):** owns broader real rootless-Podman/runtime/HTTP coverage and platform responsibilities, with separately selected integration suites, safe fixtures, and explicit Mac VM-provider/manual-device limits. Feature issues retain their own acceptance tests. Coordinate shared entrypoints/workflows with #9 rather than creating competing systems or making either a new blocker for the other.

A baseline pass is not proof of effective PHP security, real feed fetching, durable reading state, actual backup recovery, tailnet access, paid-feed authorization, or physical ReadKit client behavior. Record actual checks, unavailable prerequisites, unperformed checks, and platform limitations rather than claiming full coverage.

## Architecture

- [Isolate local development with Podman](docs/adr/0001-isolate-local-development-with-podman.md)
- [Portable state and consistent external backups](docs/adr/0002-portable-state-with-consistent-external-backups.md)
- [Private Tailscale access and shared reading state](docs/adr/0003-private-tailscale-access-and-shared-reading-state.md)

These decisions describe accepted architecture; their pending implementations are not features delivered by this standards change.
