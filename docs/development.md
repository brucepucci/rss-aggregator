# Development and verification

This is the repository's adoption guide for the [Python standard](python-standards.md). See the [CLI reference](cli.md) for command contracts, the [glossary](../GLOSSARY.md) and [architecture](architecture.md) for domain decisions, and [operations](operations.md) for operator procedures. Agent-only instructions start in [AGENTS.md](../AGENTS.md).

## Environment and baseline

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/) on `PATH`. From the checkout:

```sh
uv sync --locked
uv run --locked rss-dev check
```

Sync prepares the checkout-local `.venv` from the committed lock, including development dependencies and the editable project. It can download the requested Python, packages and build backend; it does not install hooks or start FreshRSS. `.python-version` requests **3.13.18**; uv can reuse a matching installed interpreter. Declared production compatibility is **Python 3.10+**, not evidence that every interpreter version has been tested. Use uv, not global pip or a host-package upgrade.

`rss-dev check` runs every check even when an earlier one fails, with the selected environment's Python:

| Check | Child arguments |
| --- | --- |
| Complete default pytest suite | `-I -B -m pytest -p no:cacheprovider -m 'not live'` |
| Ruff lint | `-I -B -m ruff check --no-fix --no-fix-only --no-cache` |
| Ruff format check | `-I -B -m ruff format --check --no-cache` |

Run the baseline under an unprivileged Unix account (not UID 0, including in containers). No GUI session, installed LaunchAgents, credentials, network, native runtime or live services are needed. Some disposable Git fixtures sync offline from the warmed cache; missing resources fail rather than permitting network inside tests. The gate does not write source fixes, bytecode or test/lint caches. It sends tool output to stderr and reports the results as [CLI JSON](cli.md#developer-commands-and-results).

For intentional edits, use `uv run --locked rss-dev format` (format only) or `uv run --locked rss-dev fix` (safe lint fixes only), then run the full check. Direct `uv run --locked pytest` selects the default non-live suite; direct paths and markers can narrow a focused run, not replace the gate. See [pytest selection](cli.md#pytest-selection).

### Selected-checkout isolation

`rss-dev check`, `format`, and `fix` use the **selected checkout's own `.venv`**, not the calling checkout's editable install. For another checkout first run `uv sync --locked --project /path/to/checkout`, then pass `--project /path/to/checkout` to the chosen `rss-dev` subcommand. The default is the current working directory. A missing environment fails; there is no fallback or automatic creation. Shared/external `UV_PROJECT_ENVIRONMENT` is not selected by the project entrypoint.

Python child tools use isolated import mode and suppress bytecode. All child environments remove `PYTHONPATH`, `PYTHONHOME`, `PYTEST_*`, and inherited repository selectors (`GIT_DIR`, `GIT_COMMON_DIR`, `GIT_WORK_TREE`, `GIT_IMPLICIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_PREFIX`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`). This prevents accidental imports or a push hook's Git context from selecting the wrong checkout. Git configuration/authentication variables remain available so hook guards still apply. Direct pytest retains normal pytest environment handling.

## Development configuration

| File | Contract |
| --- | --- |
| [`pyproject.toml`](../pyproject.toml) | Python `>=3.10`; runtime Click `>=8.1,<9`; pytest `>=8,<10`, Ruff `>=0.14,<0.15` and pre-commit `>=4,<5` in the dev group; Hatchling `>=1.27,<2` build backend; `rss` and `rss-dev` entrypoints; default pytest selection. |
| [`.python-version`](../.python-version), [`uv.lock`](../uv.lock) | Requested Python version and locked dependency resolution. Run `uv lock` only for intentional dependency changes, then locked sync and the full baseline. |
| [`.pre-commit-config.yaml`](../.pre-commit-config.yaml) | One local/system `development-check` pre-push hook: `always_run: true`, `pass_filenames: false`, entry `uv run --locked --offline rss-dev check`. |
| [`.github/workflows/python.yml`](../.github/workflows/python.yml) | Same baseline on PRs and pushes to `main`; Ubuntu runner, uv 0.13.8, pinned action revisions and read-only contents. |

Ruff uses target `py310`, rules `E4,E7,E9,F,I,D,UP`, and the NumPy docstring convention; `tests/**/*.py` is exempt from `D`. This covers baseline errors, unused imports/variables, imports, docstrings and modernization. Ruff replaces overlapping standalone formatter/lint tools, but review still checks annotated production signatures and useful proportional NumPy-style docstrings, including private callables. No separate type checker or required coverage percentage is configured. `.venv` and tooling caches are not FreshRSS data or backups; runtime uninstall is not a Python tooling cleanup.

## Managed pre-push hooks

After sync, run `uv run --locked rss-dev setup` to opt in. With a Git remote it can install the uv-managed pre-push hook. Without one it reports `active: false` successfully; explicitly rerun after adding a remote. Existing custom `core.hooksPath`, foreign/legacy/symlinked or recognized nonexecutable hooks are preserved and refused, not automatically chained or overwritten. Recognized executable hooks remain byte-for-byte unchanged. Resolve conflicts manually. `active` means installed or recognized, not that a push ran.

Linked worktrees share Git hooks. Configure them deliberately and push from a branch containing the workflow configuration. The hook runs the complete offline baseline on pushes to any remote, regardless of changed paths, and fails on missing tools/caches. Hooks can be missing or bypassed and are not a security boundary. Use disposable repositories and local bare remotes to test hook installation, refusal, or actual pushes.

## GitHub Actions

CI synchronizes with `uv sync --locked` and calls `uv run --locked rss-dev check`. It is the same check-only baseline as local development with separate environment setup. Action definitions and passing Linux checks are not branch protection, Apple Silicon execution, or evidence for Tailscale/Mac jobs.

## Callable Python interface and fixtures

`Runtime(root=..., state_dir=...).run(operation, **options)` is the guarded, Click-independent lifecycle boundary. The [`rss` CLI](cli.md#runtime-commands-and-results) delegates to it and handles prompts, operation JSON and exit status. Use it for deterministic state and timing assertions (the daily policy accepts an aware `now` clock); use public CLI tests for argument/result contracts. Don't bypass production ownership/readiness gates just to assert private invocation sequences. Use temporary private paths, synthetic feed and archive fixtures, and controlled dependency/process responses. Keep real user credentials and host registrations out of test inputs.

| Layer | Regression scope | What it does not prove |
| --- | --- | --- |
| Default pytest/Ruff | Tooling and selected-checkout/hook guards; pinned setup after fresh-state inspection/refusal, and fixture checkout isolation with `PYTHONSAFEPATH=1`; native lifecycle and readiness/forwarding guards; controlled UTC fetch slots and deadlines; archive safety, capture-time and restore path-budget refusal, and restore beside active writers; stopped-intent backup veto and failed daily export after quiescence with the previous archive and HTTP readiness preserved; disposable legacy fixtures; writer leases and parent-loss behavior using disposable subprocesses; job inspection deadlines, atomic plist publication and removal conflicts; unreadable archives, damaged scheduler records, explicit-stop recovery veto, reconciliation and upgrade evidence. | Actual macOS release execution, login/launchd, trusted client HTTPS, independently retrieved archive or production behavior. |
| Selected native integration | Repeatable pinned setup, effective PHP and owned loopback HTTP/API (ordinary web/API requests are gated during startup); startup fetch and a changed synthetic feed collected by manual fetch; read/star changes; isolated synthetic backup, authenticated restore of subscriptions, article content and markings with the source path absent, and uninstall preserving data and archives. | The fixture initializes its user through upstream CLI. It does not prove interactive setup, real jobs, genuine legacy data rehearsal, tailnet/client behavior or copy independence. |
| Owner-assisted acceptance | Actual migration, session/job behavior, both ReadKit clients and host-loss rehearsal. | Not run by the baseline or inferred from synthetic data. |

Test lock and interruption behavior at their real boundary where feasible (e.g., disposable subprocesses and temporary files), not just via exception unwinding. Use controlled clocks for slot and retry policy rather than waiting on production cadence. Focused tests never replace the complete baseline.

## Explicit live/platform checks

The synthetic Apple Silicon integration selection is:

```sh
uv run --locked pytest -m live tests/integration/test_native.py
```

It requires a normal user on an **isolated** native Apple Silicon Mac (macOS 12 or later), a synchronized checkout, access to the pinned downloads, and space on local temporary storage. Before invoking it, confirm the host has no Serve, Funnel or other proxy forwarding to the IPv4 loopback ephemeral ports the OS can assign; the test does not precheck forwarding before opening the installer on its dynamically assigned port. `RSS_TAILSCALE=0` does not disable existing forwarding. The test uses private temporary state, backup paths and synthetic credentials. The synthetic config and fixture PHP writes remain private under a caller umask of `0002`; the test also deliberately starts the runtime under `0002`. It checks that normal HTTP and API requests are refused until startup approval. It observes startup articles before changing the loopback publisher, waits for the test feed's TTL/cache to expire, and then verifies the new article after manual fetching before the next periodic fetch slot. It also checks the restored user's specific synthetic subscription through the authenticated API. It does not install jobs or use production data. This selection exercises real native code; it is not the Ubuntu baseline and does not certify a live deployment. An unsupported-platform failure or skip is not integration evidence.

The [legacy development guide and selectable Podman checks](https://github.com/brucepucci/rss-aggregator/blob/28664ab936f8872da6ff3ddca1c4b0a725dfb0ec/docs/development.md#explicit-liveplatform-checks) remain at the pinned legacy revision, along with its [guarded removal procedure](https://github.com/brucepucci/rss-aggregator/blob/28664ab936f8872da6ff3ddca1c4b0a725dfb0ec/docs/operations.md#preview-and-remove-the-vm). They are not native commands or permanent Linux integration requirements on this branch.

## Verification and adoption status

The reusable standard is adopted through uv/locked dependencies, pytest, Ruff, Click adapters and a shared check-only pre-push/CI entrypoint. Platform-dependent coverage is explicit rather than part of the default suite. Annotation/docstring quality and the documentation contracts still need review. Record revision-specific commands, outcomes, skips and unresolved failures in the issue/PR; avoid copying private logs or declaring a workflow definition a passing run.

Owner-assisted checks remain separate from local implementation verification:

- Rehearse with a verified real legacy archive at the same FreshRSS release, separate state, jobs, port and backup destination. Verify authenticated representative subscriptions, article content and read/unread/starred/unstarred state. Do not switch Serve during rehearsal.
- In an approved disposable Mac session, observe actual scheduled fetching, login/recovery, persistence of explicit stop, and an unattended daily archive that can be restored. A controlled launchctl test is not that check.
- In an approved window, perform an afresh final import with old writers stopped, verify locally, and cut over the existing endpoint. Verify real trusted HTTPS and both ReadKit clients, including marking changes in both directions; confirm native service and jobs have no VM dependency.
- Retrieve a scheduled native archive from a completed independent Time Machine copy and verify its authenticated restored state in isolation. Evidence for a staged local archive or the old runtime does not satisfy this. See [backup and restore](backup-restore.md). Only then coordinate final guarded legacy removal. Keep the importer and the legacy procedure available until the owner-assisted gates pass; retire the transitional importer after migration is complete.
