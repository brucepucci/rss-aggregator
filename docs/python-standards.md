# Lightweight Python development standard

This is reusable policy for Python projects, not an installation guide or proof of enforcement. Keep verified commands, supported versions, operational details, explicit exceptions, and adoption status in the adopting project's documentation. Copying this document requires no shared package, generator, or distribution framework.

## Environment and dependencies

- Use **uv** to manage Python environments and runtimes and to execute project commands. Avoid ad hoc global package installation.
- Declare supported Python versions and dependencies in project metadata. Separate development dependencies from runtime dependencies.
- Application projects commit their uv lockfile for reproducible dependencies. Library projects may explicitly document a different lockfile policy.
- Align tooling's Python target with the declared compatibility range.

## Tests

- Prefer **pytest** as the default test framework.
- The complete default suite must be deterministic, isolated, and runnable without credentials, network access, or production services. Use controlled substitutes and temporary state where needed; preserve real user resources.
- Live-service and platform-dependent integration tests require explicit selection, separate from the default suite. Document their prerequisites, selection, and limits; a default-suite pass is not proof of real infrastructure behavior.
- Cover meaningful success and failure behavior at agreed public or command boundaries. Choose test seams deliberately; an arbitrary coverage percentage is not required.

## Callable APIs and CLI boundaries

- Expose useful logical subsystem operations through **Click** when they support agent development or testing. Select operations by actual use, rather than wrapping every function or creating internal microservices.
- Keep Click as a thin adapter over callable Python behavior. Business logic must remain usable without starting a CLI process or depending on Click internals.
- CLI operation results default to **indented JSON on stdout**. Keep diagnostics and underlying process chatter out of the result stream; diagnostics go to **stderr**.
- Failures return a **nonzero exit code**. Document command result shapes and failure semantics in project guidance.
- Neither a universal result-envelope schema nor an additional text-output mode is required.

## Typing and documentation

- Annotate production function and method signatures, including non-public callables. A separate type checker, annotations on every local variable, and elaborate typing abstractions are not initially required.
- Write proportional **NumPy-style docstrings** for production functions and methods, including non-public callables. Test functions and trivial Python special methods are exempt from this docstring requirement.
- A summary-only docstring is sufficient when appropriate. Parameter, return, exception, and example sections should add useful information rather than restate the signature; include them where they clarify behavior.

## Formatting and linting

- Use **Ruff's formatter**, not a separate Black invocation.
- Use a focused lint selection covering baseline errors, import sorting, unused imports and variables, NumPy-compatible docstring checks, and Python modernization. Do not enable every rule.
- Configure the NumPy docstring convention and document the chosen lint/format contract and any exceptions. Align Ruff's Python target with declared project compatibility.
- Ruff replaces overlapping Black/isort/autoflake/pydocstyle/pyupgrade tooling. It does not literally run those tools or guarantee identical behavior.
- Ruff enforces only part of the documentation policy. Review must assess usefulness, proportionality, and private-callable coverage where tooling does not enforce them.
- Provide separate, explicit commands for formatting and safe fixes. Automated gates run **checks only**: they must not silently rewrite files or apply unsafe fixes.

## Pre-push checks

- Declare **pre-commit** as a uv-managed development dependency and use it to manage **pre-push** hooks.
- Provide a documented developer setup command that installs hooks when a Git remote is configured. If setup runs before a remote exists, explain that installation is not active and that setup must be rerun after adding a remote. Do not promise automatic remote discovery or later installation.
- Preserve or explicitly resolve existing hook arrangements during setup; never silently overwrite them.
- On pre-push, run the **complete default pytest suite**, **Ruff lint checks**, and **Ruff format checks**. File selection must not reduce the required complete test suite.
- Hooks apply to pushes to **any Git remote**, not just GitHub. Local hooks may be absent or bypassed and are not a security boundary.

## Continuous integration

- For GitHub projects, run the same baseline checks through **GitHub Actions**: the complete default pytest suite, Ruff lint, and Ruff format checks. Keep check definitions and success/failure semantics consistent with local gates; prerequisite setup may differ.
- Reuse existing test entrypoints and workflows when available rather than creating competing verification paths.
- Keep live-service and platform-dependent integration coverage separately selected. Preserve the project's additional integration requirements; passing baseline checks does not replace them.

## Adoption and exceptions

- Keep reusable policy free of private values and repository-specific resource names. Put setup commands, actual configuration, operational guidance, and explicit policy exceptions in project-specific documentation.
- Distinguish agreed policy from installed, working enforcement. Label planned commands and unverified behavior rather than presenting them as available features.
- Record actual verification results and limitations. Review must account for every applicable requirement and exemption, including requirements that tools cannot fully enforce.
