"""Preview and explicitly remove one unchanged, project-added Homebrew formula."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime

_FORMULA = re.compile(r"[a-z0-9][a-z0-9+_.-]*(?:@[a-z0-9][a-z0-9+_.-]*)?")


class PackageCleanup:
    """Use Runtime's existing lock and maintenance admission for package removal."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def remove(
        self, package: str, confirm: str | None, unused_elsewhere: bool
    ) -> dict[str, Any]:
        """Preview by default; apply only a fresh confirmed, externally unused candidate.

        Runtime owns command serialization and maintenance admission. Neither
        preview nor removal rewrites ownership ledgers. Confirmation is a digest
        of private evidence, not authorization to remove dependencies recursively.
        """
        if not isinstance(package, str) or not _FORMULA.fullmatch(package):
            self._fail("Package must be a single plain Homebrew formula name.")
        plan, report = self._plan(package)
        report["confirmation"] = self._fingerprint(plan)
        if confirm is None:
            return report
        if report["status"] == "absent":
            return {**report, "preview": False}
        if not unused_elsewhere:
            self._fail(
                "Applying package removal requires --unused-elsewhere for this candidate."
            )
        if confirm != report["confirmation"]:
            self._fail("Package removal confirmation is stale; preview again.")
        if report["retain"]:
            self._fail("Package must be retained: " + " ".join(report["retain"]))
        latest, current = self._plan(package)
        if self._fingerprint(latest) != confirm or current["retain"]:
            self._fail("Package removal plan changed; preview again.")
        self._command(
            latest["brew"]["path"],
            "uninstall",
            "--formula",
            package,
            dependency_checks=True,
        )
        return {
            **current,
            "preview": False,
            "status": "removed",
            "confirmation": confirm,
        }

    def _fail(self, message: str) -> None:
        """Raise the public failure type without exposing external command output."""
        from rss_aggregator.runtime import RuntimeFailure

        raise RuntimeFailure(message) from None

    def _command(self, *args: str, **options: Any) -> subprocess.CompletedProcess[str]:
        """Capture native output privately and sanitize failures before propagation."""
        from rss_aggregator.runtime import RuntimeFailure

        try:
            result = self.runtime._run(*args, check=False, **options)
        except (RuntimeFailure, OSError, ValueError):
            self._fail(
                "Package cleanup command unavailable; inspect native tools locally."
            )
        if result.returncode:
            self._fail("Package cleanup command failed; inspect native tools locally.")
        return result

    def _ledger(self, name: str) -> dict[str, Any]:
        """Read a required ownership ledger without changing it or leaking contents."""
        try:
            value = json.loads((self.runtime.state_dir / name).read_text())
        except (OSError, ValueError):
            self._fail(
                "Package ownership evidence is missing or unreadable; retain packages."
            )
        if not isinstance(value, dict):
            self._fail("Package ownership evidence is malformed; retain packages.")
        return value

    def _versions(self, value: Any) -> dict[str, str]:
        """Require an unambiguous formula-to-version ownership or inventory mapping."""
        if not isinstance(value, dict) or any(
            not isinstance(name, str)
            or not _FORMULA.fullmatch(name)
            or not isinstance(version, str)
            or not version
            or version != version.strip()
            or not version.isprintable()
            for name, version in value.items()
        ):
            self._fail("Package version evidence is malformed; retain packages.")
        return value

    def _executable(self, name: str) -> dict[str, Any] | None:
        """Resolve executable identity so changed native tools invalidate confirmation."""
        executable = shutil.which(name)
        if executable is None:
            return None
        try:
            path = Path(executable).resolve(strict=True)
            stat = path.stat()
        except OSError:
            self._fail("Package cleanup executable unavailable; retain packages.")
        return {
            "path": str(path),
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "mode": stat.st_mode,
        }

    def _inventory(self, podman: dict[str, Any] | None) -> dict[str, Any]:
        """Read all Podman machines and connections, never just the owned resources."""
        if podman is None:
            return {"available": False, "machines": [], "connections": []}
        inventory: dict[str, Any] = {"available": True}
        for kind, args in (
            ("machines", ("machine", "list", "--format", "json")),
            ("connections", ("system", "connection", "list", "--format", "json")),
        ):
            result = self._command(podman["path"], *args)
            try:
                records = json.loads(result.stdout)
            except ValueError:
                self._fail("Podman inventory is unreadable; retain packages.")
            if not isinstance(records, list) or any(
                not isinstance(record, dict) for record in records
            ):
                self._fail("Podman inventory is malformed; retain packages.")
            inventory[kind] = records
        return inventory

    def _plan(self, package: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Bind private ownership and fresh native inventory to a sanitized preview."""
        brew = self._executable("brew")
        if brew is None:
            self._fail("Homebrew is unavailable; package absence cannot be verified.")
        output = self._command(brew["path"], "list", "--formula", "--versions").stdout
        installed: dict[str, str] = {}
        for line in output.splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) != 2 or fields[0] in installed:
                self._fail("Homebrew formula inventory is malformed; retain packages.")
            installed[fields[0]] = fields[1]
        self._versions(installed)
        baseline = self._ledger("host-before.json")
        before = self._versions(baseline.get("packages"))
        additions = self._versions(self._ledger("host-additions.json"))
        version = installed.get(package)
        dependents = []
        if version is not None:
            result = self._command(
                brew["path"],
                "uses",
                "--installed",
                "--recursive",
                "--include-build",
                "--include-test",
                "--include-optional",
                package,
            )
            dependents = sorted(set(result.stdout.splitlines()))
        if any(
            not re.fullmatch(r"[a-z0-9][a-z0-9+_.@/-]*", name) for name in dependents
        ):
            self._fail("Homebrew dependents inventory is malformed; retain packages.")
        podman = self._executable("podman")
        inventory = self._inventory(podman)
        retain = []
        if package in before:
            retain.append("Package was present in the baseline.")
        if package not in additions:
            retain.append("Package is not recorded as a project addition.")
        elif version is not None and version != additions[package]:
            retain.append(
                "Installed version differs from the recorded project addition."
            )
        if dependents:
            retain.append("Installed formulae or casks depend on this package.")
        if inventory["machines"] or inventory["connections"]:
            retain.append("Podman machines or connections remain; retain all packages.")
        if not inventory["available"] and (
            package == "podman" or "podman" in installed
        ):
            retain.append(
                "Podman CLI is unavailable; machine and connection absence is unverified."
            )
        targets = {"package": package, "version": version}
        plan = {
            "format": "rss-aggregator-package-cleanup-v1",
            "root": str(self.runtime.root),
            "state_dir": str(self.runtime.state_dir),
            "brew": brew,
            "podman": podman,
            "podman_version": installed.get("podman"),
            "targets": targets,
            "baseline": baseline,
            "additions": additions,
            "dependents": dependents,
            "inventory": inventory,
        }
        report = {
            "preview": True,
            "status": "absent" if version is None else "preview",
            "targets": targets,
            "ownership": {
                "baseline": package in before,
                "addition": package in additions,
                "recorded_version": additions.get(package),
                "version_matches": version is not None
                and version == additions.get(package),
            },
            "dependents": dependents,
            "retain": retain,
        }
        return plan, report

    def _fingerprint(self, plan: dict[str, Any]) -> str:
        """Hash private complete plan evidence, publishing only the digest."""
        return hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
