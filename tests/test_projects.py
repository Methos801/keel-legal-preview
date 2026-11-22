"""Unit tests for per-project SourceLedger scoping and KeelRuntime lab bridge integration."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

from keel.daemon.artifacts import ArtifactStore

from keel.sources.ledger import SourceLedger, sanitize_project_id


class TestPerProjectSourceLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory(prefix="k_proj_ledger_")
        self.root = Path(self.td.name)
        self.state_dir = self.root / "state"
        self.store = ArtifactStore(self.state_dir / "artifacts")

    def tearDown(self) -> None:
        self.store.close()
        self.td.cleanup()

    def test_two_projects_start_at_s0001_and_isolate_identical_filenames(self) -> None:
        dir_a = self.root / "ws_a"
        dir_b = self.root / "ws_b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "README.md").write_text("Project Alpha unique content.", encoding="utf-8")
        (dir_b / "README.md").write_text("Project Beta different content.", encoding="utf-8")

        ledger_a = SourceLedger.for_project(self.state_dir, "proj-a")
        ledger_b = SourceLedger.for_project(self.state_dir, "proj-b")
        try:
            self.assertEqual(ledger_a.project_id, "proj-a")
            self.assertEqual(ledger_b.project_id, "proj-b")
            self.assertEqual(
                ledger_a.root.resolve(),
                (self.state_dir / "projects" / "proj-a" / "sources").resolve(),
            )
            self.assertEqual(
                ledger_b.root.resolve(),
                (self.state_dir / "projects" / "proj-b" / "sources").resolve(),
            )

            scan_a = ledger_a.scan_directory(dir_a)
            scan_b = ledger_b.scan_directory(dir_b)
            self.assertEqual(scan_a, {"scanned": 1, "new": 1, "updated": 0})
            self.assertEqual(scan_b, {"scanned": 1, "new": 1, "updated": 0})

            list_a = ledger_a.list_sources()
            list_b = ledger_b.list_sources()
            self.assertEqual([s.sid for s in list_a], ["S0001"])
            self.assertEqual([s.sid for s in list_b], ["S0001"])
            self.assertEqual(list_a[0].rel_path, "README.md")
            self.assertEqual(list_b[0].rel_path, "README.md")
            self.assertNotEqual(list_a[0].sha256, list_b[0].sha256)

            ext_a = ledger_a.extract_source("S0001", self.store, session="proj-a")
            ext_b = ledger_b.extract_source("S0001", self.store, session="proj-b")
            self.assertIn("Project Alpha unique content.", ext_a["text_preview"])
            self.assertIn("Project Beta different content.", ext_b["text_preview"])
            self.assertNotEqual(ext_a["handle"], ext_b["handle"])
        finally:
            ledger_a.close()
            ledger_b.close()

