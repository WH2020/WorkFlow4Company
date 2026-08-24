from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def test_no_remote_is_configured(self) -> None:
        result = subprocess.run(
            ["git", "remote"], cwd=ROOT, text=True, capture_output=True, check=True
        )
        self.assertEqual("", result.stdout.strip())

    def test_forbidden_runtime_content_is_not_tracked(self) -> None:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        )
        tracked = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
        blocked_parts = {
            ".pi",
            ".tmp",
            "tmp",
            "runtime",
            "node_modules",
            "dist",
            "target",
            "gen",
            "outputs",
            "backups",
        }
        blocked_suffixes = {
            ".db",
            ".sqlite",
            ".sqlite3",
            ".log",
            ".exe",
            ".msi",
            ".pem",
            ".key",
            ".pfx",
        }
        violations = [
            path
            for path in tracked
            if blocked_parts.intersection(Path(path).parts)
            or Path(path).suffix.lower() in blocked_suffixes
        ]
        self.assertEqual([], violations)

    def test_removed_requirements_are_not_runtime_dependencies(self) -> None:
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        serialized = json.dumps(package).lower()
        self.assertNotIn("juicesharp", serialized)
        self.assertNotIn("rpiv-todo", serialized)
        self.assertFalse((ROOT / "docs/reqguard").exists())
        self.assertNotIn("repository", package)

    def test_source_remote_is_not_reused(self) -> None:
        config = (ROOT / ".git/config").read_text(encoding="utf-8")
        self.assertNotIn("WorkFlow_Market", config)
        self.assertNotIn("github.com/WH2020", config)


if __name__ == "__main__":
    unittest.main()
