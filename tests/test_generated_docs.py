from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gen_state_diagrams

MAINTAINED_DOCS = (
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/call_flow.md",
    "docs/STATE_MACHINE.md",
)
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
REF_RE = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|sh|md|json)):(\d+(?:[,\-]\d+)*)`")


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

    def test_maintained_docs_links_fences_and_references(self) -> None:
        for name in MAINTAINED_DOCS:
            doc = ROOT / name
            text = doc.read_text(encoding="utf-8")
            self.assertEqual(text.count("```") % 2, 0, f"{name}: unbalanced code fences")
            for link in LINK_RE.findall(text):
                target = link.split("#", 1)[0]
                if not target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (doc.parent / target).resolve()
                self.assertTrue(resolved.exists(), f"{name}: broken link {link}")
            for match in REF_RE.finditer(text):
                path = ROOT / match.group(1)
                self.assertTrue(path.exists(), f"{name}: missing reference {match.group(1)}")
                line_count = len(path.read_text(encoding="utf-8").splitlines())
                for value in re.findall(r"\d+", match.group(2)):
                    self.assertLessEqual(
                        int(value),
                        line_count,
                        f"{name}: bad line reference {match.group(0)}",
                    )


if __name__ == "__main__":
    unittest.main()
