## Documentation

Update `README.md` in the same change whenever setup, commands, configuration, or operational behavior changes. Maintain comprehensive sections:

- **Getting Started**: prerequisites, installation, first-run verification, normal start/stop, ReadKit connection, backup/restore, safe cleanup, and common troubleshooting.
- **Configuration**: supported settings, their purpose and defaults, required versus optional values, how to change and apply them, and safe local handling of secrets.

Verify documented commands and settings against the implementation before considering a change complete. Clearly distinguish planned or unverified behavior from working features; use placeholders rather than real credentials or private feed URLs.

Reviewers must assess whether each change requires updates to the README or affected domain/architecture documentation, including when the diff contains no documentation files. Report missing or stale required documentation as standards violations and identify the sections needing updates.

## Python development

Before writing or reviewing Python, or changing Python tooling, read `docs/python-standards.md`. See `README.md` for repository-specific adoption status, verified commands, and exceptions.

## Agent skills

### Issue tracker
For issue and spec operations, read `docs/agents/issue-tracker.md`.

### Triage labels
For issue classification, read `docs/agents/triage-labels.md`.

### Domain docs
Before codebase exploration, read `docs/agents/domain.md`.
