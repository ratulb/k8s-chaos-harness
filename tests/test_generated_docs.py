from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gen_state_diagrams


class TestGeneratedDocs(unittest.TestCase):
    def test_state_machine_reference_matches_generator(self) -> None:
        generated = gen_state_diagrams.build_document()
        committed = (ROOT / "docs" / "STATE_MACHINE.md").read_text(encoding="utf-8")
        self.assertEqual(committed, generated)

    def test_maintained_docs_do_not_reference_retired_docs(self) -> None:
        retired = (
            "DESIGN.md",
            "PHASES.md",
            "STATE_MACHINE_DESIGN.md",
            "STATE_MACHINE_PRIMER.md",
        )
        for name in ("README.md", "docs/ARCHITECTURE.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for document in retired:
                self.assertNotIn(document, text, f"{name} still references {document}")


if __name__ == "__main__":
    unittest.main()
