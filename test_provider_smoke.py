#!/usr/bin/env python3
"""Optional live smoke tests against provider APIs.

These skip when the corresponding API key is unset so CI stays green without
secrets. When keys are present, they prove the default model and payload shape
are currently accepted.
"""

from __future__ import annotations

import os
import unittest

import merge_warden as mw

SMOKE_SYSTEM = (
    "You are a JSON generator. Reply with a JSON object only. "
    "Do not wrap it in markdown."
)
SMOKE_USER = 'Return {"event":"COMMENT","body":"ok","comments":[]}'


def _require(name: str) -> str:
    return (os.environ.get(name) or "").strip()


class LiveProviderSmokeTests(unittest.TestCase):
    def _assert_review_json(self, raw: str) -> None:
        data = mw.parse_review_json(raw)
        self.assertIsInstance(data, dict)
        self.assertTrue(data)

    @unittest.skipUnless(_require("XAI_API_KEY"), "XAI_API_KEY not set")
    def test_xai_grok_accepts_default_payload(self) -> None:
        raw = mw.call_model(
            "xai",
            SMOKE_SYSTEM,
            SMOKE_USER,
            mw.DEFAULT_MODELS["xai"],
            _require("XAI_API_KEY"),
        )
        self._assert_review_json(raw)

    @unittest.skipUnless(_require("OPENAI_API_KEY"), "OPENAI_API_KEY not set")
    def test_openai_accepts_default_payload(self) -> None:
        raw = mw.call_model(
            "openai",
            SMOKE_SYSTEM,
            SMOKE_USER,
            mw.DEFAULT_MODELS["openai"],
            _require("OPENAI_API_KEY"),
        )
        self._assert_review_json(raw)

    @unittest.skipUnless(_require("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY not set")
    def test_anthropic_accepts_default_payload(self) -> None:
        raw = mw.call_model(
            "anthropic",
            SMOKE_SYSTEM,
            SMOKE_USER,
            mw.DEFAULT_MODELS["anthropic"],
            _require("ANTHROPIC_API_KEY"),
        )
        self._assert_review_json(raw)

    @unittest.skipUnless(
        _require("GOOGLE_API_KEY") or _require("GEMINI_API_KEY"),
        "GOOGLE_API_KEY/GEMINI_API_KEY not set",
    )
    def test_gemini_accepts_default_payload(self) -> None:
        raw = mw.call_model(
            "google",
            SMOKE_SYSTEM,
            SMOKE_USER,
            mw.DEFAULT_MODELS["google"],
            mw.resolve_api_key("google"),
        )
        self._assert_review_json(raw)


if __name__ == "__main__":
    unittest.main()
