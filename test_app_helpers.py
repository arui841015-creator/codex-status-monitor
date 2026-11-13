"""Tests for launch-argument parsing and the Windows autostart helper."""
from __future__ import annotations

import unittest
from unittest import mock

import autostart
from monitor_app import parse_args


class ParseArgsTest(unittest.TestCase):
    def test_recognises_minimized_flags(self) -> None:
        self.assertTrue(parse_args(["--minimized"]))
        self.assertTrue(parse_args(["--MINIMIZED"]))
        self.assertTrue(parse_args(["ignored", "--tray"]))
        self.assertTrue(parse_args(["--startup"]))

    def test_normal_launch_stays_visible(self) -> None:
        self.assertFalse(parse_args([]))
        self.assertFalse(parse_args(["--verbose"]))


class LaunchCommandTest(unittest.TestCase):
    def test_command_is_quoted_and_requests_minimized(self) -> None:
        command = autostart.launch_command()
        self.assertIn(autostart.START_FLAG, command)
        self.assertTrue(command.startswith('"'), command)
        self.assertGreaterEqual(command.count('"'), 2, command)

    def test_source_mode_prefers_pythonw(self) -> None:
        # Running the test suite under python.exe must still yield a windowless
        # interpreter whenever pythonw.exe sits beside it.
        command = autostart.launch_command()
        if getattr(__import__("sys"), "frozen", False):
            self.skipTest("frozen build points at the exe instead")
        self.assertIn("pythonw", command.lower())


class SyncTest(unittest.TestCase):
    def test_repairs_stale_entry(self) -> None:
        with mock.patch.object(autostart, "current_command", return_value='"C:\\old\\x.exe" --minimized'):
            with mock.patch.object(autostart, "enable") as enable:
                self.assertTrue(autostart.sync())
        enable.assert_called_once()

    def test_noop_when_entry_absent(self) -> None:
        with mock.patch.object(autostart, "current_command", return_value=None):
            with mock.patch.object(autostart, "enable") as enable:
                self.assertFalse(autostart.sync())
        enable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
