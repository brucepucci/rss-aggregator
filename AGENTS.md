## Documentation

Update the appropriate canonical documentation in the same change as relevant implementation changes. Keep these audience boundaries:

- `README.md`: owner/operator introduction, table of contents (including each FAQ), deployed architecture overview, shortest verified getting-started path, routine commands, essential configuration, common troubleshooting and task-based FAQ. Keep detailed procedures behind links; update summaries and navigation when affected.
- `docs/cli.md`: exhaustive project CLI reference. Cover every command, subcommand, argument and flag, including purpose, defaults, usage, output and failure behavior.
- `docs/configuration.md`: full runtime settings reference, required versus optional values, defaults, application steps and safe secret handling.
- `docs/operations.md`, `docs/backup-restore.md`, `docs/tailscale-readkit.md`: canonical operator procedures and safeguards; extend these rather than duplicating them in README.
- `docs/development.md`: developer setup, commands/checks, Python adoption/exceptions, test prerequisites, verification guidance and remaining coverage limits. Extend the existing test entrypoint/workflow documentation as feature coverage grows.
- `docs/architecture.md`, `docs/adr/`, `GLOSSARY.md`: implementation internals, architectural rationale and domain vocabulary respectively.
- `docs/agents/`: agent-only procedures, discovered through the pointers below; keep them separate from user and developer instructions.

Verify CLI references against implementation and `--help`, settings against implementation, and links/anchors when moving content. Clearly distinguish working, planned, unsupported and unverified behavior. Use placeholders instead of credentials or private feed URLs. Keep safeguards accessible at the point of use. Record revision-specific verification in issue/PR handoffs; retain only useful enduring evidence and limitations in documentation, not stale branch/publication status.

Reviewers must assess README, reference, procedure and domain/architecture documentation needs even when a diff contains no documentation files. Report missing/stale required documentation, broken references or CLI omissions as standards violations, identifying the canonical sections needing updates.

## Python development

Before writing or reviewing Python, or changing Python tooling, read `docs/python-standards.md`. See `docs/development.md` for repository-specific adoption status, verified commands, and exceptions.

## Agent skills

### Issue tracker
For issue and spec operations, read `docs/agents/issue-tracker.md`.

### Triage labels
For issue classification, read `docs/agents/triage-labels.md`.

### Domain docs
Before codebase exploration, read `docs/agents/domain.md`.
