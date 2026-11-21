from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm.backends import BACKEND_NAMES, build_backend, configured_backends
from tools.naming import normalize_backend


class TestBackendNaming(unittest.TestCase):
    def test_backend_names_are_public_names(self) -> None:
        self.assertEqual(BACKEND_NAMES, ("ollama", "groq", "openrouter", "cloudflare"))
        self.assertNotIn("kaggle", BACKEND_NAMES)

    def test_normalize_legacy_backend_aliases(self) -> None:
        self.assertEqual(normalize_backend("kaggle"), "ollama")
        self.assertEqual(normalize_backend("KAGGLE-OLLAMA"), "ollama")
        self.assertEqual(normalize_backend("groq"), "groq")
        self.assertEqual(normalize_backend(None), "unknown")

    def test_build_backend_normalizes_legacy_alias(self) -> None:
        with mock.patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://127.0.0.1:11435"}, clear=False):
            backend = build_backend("kaggle")
        self.assertEqual(backend.name, "ollama")
        self.assertEqual(backend.base_url, "http://127.0.0.1:11435")

    def test_build_backend_rejects_unknown_name(self) -> None:
        with self.assertRaises(ValueError):
            build_backend("mystery")

    def test_configured_backends_uses_ollama_name(self) -> None:
        env = {
            "OLLAMA_BASE_URL": "http://127.0.0.1:11435",
            "GROQ_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "CLOUDFLARE_ACCOUNT_ID": "",
            "CLOUDFLARE_AUTH_TOKEN": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            backends = configured_backends()
        self.assertEqual([backend.name for backend in backends], ["ollama"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
