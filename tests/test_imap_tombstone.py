#!/usr/bin/env python3
"""Unit tests for headers-only Apple-first curl selection. No network."""

from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import patch

import imap_tombstone as tombstone


class SelectCurlBinTests(unittest.TestCase):
    def test_env_curl_bin_wins(self):
        with patch.dict(os.environ, {"CURL_BIN": "/custom/curl"}, clear=False):
            self.assertEqual(tombstone.select_curl_bin(), "/custom/curl")

    def test_prefers_apple_when_present(self):
        env = {k: v for k, v in os.environ.items() if k != "CURL_BIN"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(tombstone.os.path, "isfile", return_value=True):
                with patch.object(tombstone.os, "access", return_value=True):
                    self.assertEqual(tombstone.select_curl_bin(), tombstone.APPLE_CURL)

    def test_falls_back_to_brew_when_apple_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "CURL_BIN"}

        def isfile(path):
            return path == tombstone.BREW_CURL

        with patch.dict(os.environ, env, clear=True):
            with patch.object(tombstone.os.path, "isfile", side_effect=isfile):
                with patch.object(tombstone.os, "access", return_value=True):
                    self.assertEqual(tombstone.select_curl_bin(), tombstone.BREW_CURL)

    def test_defaults_to_apple_when_neither_exists(self):
        env = {k: v for k, v in os.environ.items() if k != "CURL_BIN"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(tombstone.os.path, "isfile", return_value=False):
                self.assertEqual(tombstone.select_curl_bin(), tombstone.APPLE_CURL)

    def test_module_constants(self):
        self.assertEqual(tombstone.APPLE_CURL, "/usr/bin/curl")
        self.assertEqual(tombstone.BREW_CURL, "/opt/homebrew/opt/curl/bin/curl")

    def test_print_curl_cli(self):
        with patch.object(tombstone, "select_curl_bin", return_value="/usr/bin/curl"):
            with patch("sys.stdout") as stdout:
                rc = tombstone.main(["--print-curl"])
        self.assertEqual(rc, 0)
        stdout.write.assert_called_with("/usr/bin/curl\n")

    def test_reload_honors_env_for_module_curl_bin(self):
        with patch.dict(os.environ, {"CURL_BIN": "/from/env/curl"}, clear=False):
            reloaded = importlib.reload(tombstone)
            self.assertEqual(reloaded.CURL_BIN, "/from/env/curl")
        importlib.reload(tombstone)


class DocstringTests(unittest.TestCase):
    def test_module_doc_mentions_apple_default(self):
        doc = tombstone.__doc__ or ""
        self.assertIn("/usr/bin/curl", doc)
        self.assertIn("Headers-only", doc)
        self.assertIn("CURL_BIN", doc)


if __name__ == "__main__":
    unittest.main()
