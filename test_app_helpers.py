"""Tests for launch-argument parsing, autostart entries and platform paths."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import autostart
import monitor_app
import monitor_engine
from monitor_app import parse_args


class ParseArgsTest(unittest.TestCase):
    def test_recognises_minimized_flags(self) -> None:
        self.assertTrue(parse_args(["--minimized"]))
        self.assertTrue(parse_args(["--MINIMIZED"]))
        self.assertTrue(parse_args(["ignored", "--tray"]))
        self.assertTrue(parse_args(["--startup"]))
        self.assertTrue(parse_args(["-m"]))

    def test_normal_launch_stays_visible(self) -> None:
        self.assertFalse(parse_args([]))
        self.assertFalse(parse_args(["--verbose"]))


class LaunchCommandTest(unittest.TestCase):
    def test_command_carries_the_minimized_flag(self) -> None:
        command = autostart.launch_command()
        self.assertIn(autostart.START_FLAG, command)

    def test_windows_command_is_quoted(self) -> None:
        with mock.patch.object(autostart, "IS_WINDOWS", True):
            command = autostart.launch_command()
        self.assertTrue(command.startswith('"'), command)
        self.assertGreaterEqual(command.count('"'), 2, command)

    def test_source_mode_prefers_pythonw(self) -> None:
        # Running the suite under python.exe must still yield a windowless
        # interpreter whenever pythonw.exe sits beside it.
        if sys.platform != "win32" or autostart._is_frozen():
            self.skipTest("only meaningful for a Windows source run")
        self.assertIn("pythonw", autostart.launch_command().lower())

    def test_frozen_build_points_at_the_executable(self) -> None:
        with mock.patch.object(autostart, "_is_frozen", return_value=True):
            argv = autostart.launch_argv()
        self.assertEqual(argv[0], str(Path(sys.executable).resolve()))
        self.assertEqual(argv[-1], autostart.START_FLAG)


class SyncTest(unittest.TestCase):
    def test_repairs_stale_entry_in_a_frozen_build(self) -> None:
        with mock.patch.object(autostart, "_is_frozen", return_value=True), mock.patch.object(
            autostart, "current_command", return_value='"C:\\old\\x.exe" --minimized'
        ), mock.patch.object(autostart, "_up_to_date", return_value=False), mock.patch.object(
            autostart, "enable"
        ) as enable:
            self.assertTrue(autostart.sync())
        enable.assert_called_once()

    def test_never_rewrites_from_a_source_run(self) -> None:
        # A source run would write a venv interpreter into an installed app's
        # entry, which breaks autostart for the user who logs in later.
        with mock.patch.object(autostart, "_is_frozen", return_value=False), mock.patch.object(
            autostart, "current_command", return_value='"C:\\app\\x.exe" --minimized'
        ), mock.patch.object(autostart, "enable") as enable:
            self.assertTrue(autostart.sync())
        enable.assert_not_called()

    def test_noop_when_entry_absent(self) -> None:
        with mock.patch.object(autostart, "current_command", return_value=None):
            with mock.patch.object(autostart, "enable") as enable:
                self.assertFalse(autostart.sync())
        enable.assert_not_called()


class PlatformPathTest(unittest.TestCase):
    def test_default_database_resolves_under_home(self) -> None:
        resolved = monitor_engine.expand_path(monitor_engine.DEFAULT_DATABASE)
        self.assertEqual(resolved, (Path.home() / ".workbuddy" / "workbuddy.db").resolve())

    def test_legacy_windows_database_path_still_resolves(self) -> None:
        legacy = "%USERPROFILE%\\.workbuddy\\workbuddy.db"
        resolved = monitor_engine.expand_path(legacy)
        self.assertEqual(resolved.name, "workbuddy.db")
        self.assertEqual(resolved.parent.name, ".workbuddy")

    def test_macos_config_lives_outside_the_bundle(self) -> None:
        with mock.patch.object(monitor_app.sys, "platform", "darwin"):
            path = monitor_app._default_config_path()
        self.assertIn("Application Support", str(path))
        self.assertNotIn(".app", str(path))

    def test_windows_config_stays_portable(self) -> None:
        with mock.patch.object(monitor_app.sys, "platform", "win32"):
            path = monitor_app._default_config_path()
        self.assertEqual(path, monitor_app.ROOT / "monitor_config.json")

    def test_project_name_reads_windows_paths_on_any_host(self) -> None:
        # A session created on Windows keeps a backslash path. POSIX would treat
        # the whole string as one filename, so it has to be split explicitly.
        self.assertEqual(monitor_engine.project_name(r"C:\work\demo"), "demo")

    def test_project_name_reads_posix_paths(self) -> None:
        self.assertEqual(monitor_engine.project_name("/Users/me/projects/demo"), "demo")

    def test_project_name_falls_back_to_title(self) -> None:
        self.assertEqual(monitor_engine.project_name("", "会话标题"), "会话标题")


if __name__ == "__main__":
    unittest.main()
