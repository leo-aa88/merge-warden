#!/usr/bin/env python3
"""Unit tests for multi-provider Merge Warden helpers."""

from __future__ import annotations

import os
import unittest
from unittest import mock

import merge_warden as mw


class ProviderResolutionTests(unittest.TestCase):
    def test_aliases(self) -> None:
        self.assertEqual(mw.resolve_provider("grok"), "xai")
        self.assertEqual(mw.resolve_provider("ChatGPT"), "openai")
        self.assertEqual(mw.resolve_provider("claude"), "anthropic")
        self.assertEqual(mw.resolve_provider("gemini"), "google")
        self.assertEqual(mw.resolve_provider(""), "xai")

    def test_unknown_provider(self) -> None:
        with self.assertRaises(RuntimeError):
            mw.resolve_provider("cohere")

    def test_default_models(self) -> None:
        self.assertEqual(mw.resolve_model("xai", ""), "grok-4.6")
        self.assertEqual(mw.resolve_model("openai", "  "), "gpt-4.1")
        self.assertEqual(mw.resolve_model("anthropic", None or ""), "claude-sonnet-4-6")
        self.assertEqual(mw.resolve_model("google", ""), "gemini-2.5-pro")
        self.assertEqual(mw.resolve_model("openai", "gpt-4o"), "gpt-4o")

    def test_api_key_prefers_first_set_env(self) -> None:
        with mock.patch.dict(os.environ, {"GOOGLE_API_KEY": "", "GEMINI_API_KEY": "g"}, clear=False):
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ["GEMINI_API_KEY"] = "g"
            self.assertEqual(mw.resolve_api_key("google"), "g")
        with mock.patch.dict(
            os.environ,
            {"GOOGLE_API_KEY": "google", "GEMINI_API_KEY": "gemini"},
            clear=False,
        ):
            self.assertEqual(mw.resolve_api_key("google"), "google")


class PayloadTests(unittest.TestCase):
    def test_openai_compatible_payload(self) -> None:
        payload = mw.chat_completions_payload("sys", "user", "gpt-4.1")
        self.assertEqual(payload["model"], "gpt-4.1")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["messages"][0]["role"], "system")
        xai = mw.chat_completions_payload(
            "sys",
            "user",
            "grok-4.6",
            extra={"search_parameters": {"mode": "off"}},
        )
        self.assertEqual(xai["search_parameters"], {"mode": "off"})

    def test_anthropic_payload(self) -> None:
        payload = mw.anthropic_payload("sys", "user", "claude-sonnet-4-6")
        self.assertEqual(payload["system"], "sys")
        self.assertEqual(payload["max_tokens"], mw.ANTHROPIC_MAX_TOKENS)
        self.assertEqual(payload["messages"][0]["content"], "user")

    def test_gemini_payload_and_url(self) -> None:
        payload = mw.gemini_payload("sys", "user")
        self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], "sys")
        self.assertEqual(
            payload["generationConfig"]["responseMimeType"],
            "application/json",
        )
        self.assertIn("gemini-2.5-pro", mw.gemini_url("gemini-2.5-pro"))


class ResponseParsingTests(unittest.TestCase):
    def test_chat_completions_string_and_list(self) -> None:
        self.assertEqual(
            mw.content_from_chat_completions(
                {"choices": [{"message": {"content": "  {\"ok\": true}  "}}]},
                "OpenAI",
            ),
            '{"ok": true}',
        )
        self.assertEqual(
            mw.content_from_chat_completions(
                {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {"type": "text", "text": '{"event":"COMMENT"}'}
                                ]
                            }
                        }
                    ]
                },
                "OpenAI",
            ),
            '{"event":"COMMENT"}',
        )

    def test_anthropic_and_gemini(self) -> None:
        self.assertEqual(
            mw.content_from_anthropic(
                {"content": [{"type": "text", "text": '{"a":1}'}, {"type": "thinking"}]},
                "Anthropic",
            ),
            '{"a":1}',
        )
        self.assertEqual(
            mw.content_from_gemini(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"thought": True, "text": "scratch"},
                                    {"text": '{"event":"APPROVE"}'},
                                ]
                            }
                        }
                    ]
                },
                "Gemini",
            ),
            '{"event":"APPROVE"}',
        )

    def test_gemini_block_reason(self) -> None:
        with self.assertRaises(RuntimeError):
            mw.content_from_gemini(
                {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []},
                "Gemini",
            )


class CallModelRoutingTests(unittest.TestCase):
    def test_routes_each_provider(self) -> None:
        with mock.patch.object(mw, "http_post_json") as post:
            post.return_value = {
                "choices": [{"message": {"content": '{"event":"COMMENT"}'}}]
            }
            mw.call_model("xai", "sys", "user", "grok-4.6", "sk")
            url, payload, headers = post.call_args.args
            self.assertEqual(url, mw.XAI_URL)
            self.assertEqual(payload["search_parameters"], {"mode": "off"})
            self.assertEqual(payload["prompt_cache_key"], "merge-warden-v1")

            mw.call_model("openai", "sys", "user", "gpt-4o", "sk")
            url, payload, headers = post.call_args.args
            self.assertEqual(url, mw.OPENAI_URL)
            self.assertEqual(payload["model"], "gpt-4o")
            self.assertTrue(headers["Authorization"].startswith("Bearer "))

            post.return_value = {"content": [{"type": "text", "text": "{}"}]}
            mw.call_model("anthropic", "sys", "user", "claude-sonnet-4-6", "sk")
            url, payload, headers = post.call_args.args
            self.assertEqual(url, mw.ANTHROPIC_URL)
            self.assertEqual(headers["anthropic-version"], mw.ANTHROPIC_VERSION)
            self.assertEqual(headers["x-api-key"], "sk")

            post.return_value = {
                "candidates": [{"content": {"parts": [{"text": "{}"}]}}]
            }
            mw.call_model("google", "sys", "user", "gemini-2.5-pro", "sk")
            url, _payload, headers = post.call_args.args
            self.assertIn("gemini-2.5-pro", url)
            self.assertEqual(headers["x-goog-api-key"], "sk")


if __name__ == "__main__":
    unittest.main()
