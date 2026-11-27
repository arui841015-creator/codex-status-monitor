from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from monitor_engine import (
    MonitorManager,
    codex_recency,
    codex_state,
    doubao_events,
    enabled_platforms,
    evicted_ids,
    make_detail,
    normalize_config,
    project_name,
    rank_items,
    read_workbuddy_sessions,
    status_topic,
    validate_device_id,
    workbuddy_recency,
    workbuddy_state,
)
from doubao_reader import (
    WebSocketError,
    _exe_from,
    cdp_targets,
    classify_text,
    conversation_title,
    debug_port_ready,
    doubao_exe_path,
    pick_chat_target,
    session_key,
)


class RecordingPublisher:
    device_id = "sdg-test-01"
    connected = True
    last_error = ""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def publish(self, payload: dict) -> bool:
        self.payloads.append(payload)
        return True


def make_workbuddy_db(folder: str, rows: list[tuple[str, str, int]]) -> Path:
    database = Path(folder) / "workbuddy.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, title TEXT, status TEXT, "
        "created_at INTEGER, updated_at INTEGER, last_activity_at INTEGER, deleted_at INTEGER)"
    )
    for task_id, status, stamp in rows:
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            (task_id, r"C:\work\demo", f"Task {task_id}", status, stamp, stamp, stamp, None),
        )
    connection.commit()
    connection.close()
    return database


class MonitorEngineTests(unittest.TestCase):
    def _manager(self) -> MonitorManager:
        manager = MonitorManager({"identity": {"device_id": "sdg-test-01"}})
        manager.publisher = RecordingPublisher()  # skip any real broker
        return manager

    # ---- identity / display contract ----

    def test_identity_and_display_contract(self) -> None:
        self.assertEqual(status_topic("sdg-test-01"), "sdgoods/codex/v1/devices/sdg-test-01/status")
        with self.assertRaises(ValueError):
            validate_device_id("default")
        with self.assertRaises(ValueError):
            validate_device_id("REPLACE_ME")
        detail = make_detail("Workbuddy", "项目A", "整理任务", "worked", 0, 0, display_project="WB-REAL123")
        self.assertEqual(detail["display_message"], "WORKBUDDY | WB-REAL123 | COMPLETED")
        self.assertEqual(detail["project_raw"], "项目A")
        self.assertEqual(detail["task_name_raw"], "整理任务")

    def test_state_mapping(self) -> None:
        self.assertEqual(workbuddy_state("running"), "working")
        self.assertEqual(workbuddy_state("completed"), "worked")
        self.assertIsNone(workbuddy_state("archived"))
        self.assertEqual(codex_state({"status": "completed"}), "worked")
        self.assertEqual(codex_state({"status": "interrupted"}), "canceled")
        self.assertEqual(codex_state({"status": {"type": "notLoaded"}, "items": [{"type": "commandExecution"}]}), "working")
        self.assertEqual(codex_state({"status": "active", "items": [{"type": "commandExecution"}]}), "working")

    # ---- config ----

    def test_v1_channels_config_is_migrated(self) -> None:
        legacy = {
            "identity": {"device_id": "sdg-abc-01"},
            "mqtt": {"host": "10.0.0.9", "port": 8883},
            "channels": [
                {"id": "codex-main", "type": "codex", "enabled": True, "interval": 2.0},
                {"id": "workbuddy-main", "type": "workbuddy", "enabled": False, "database": "D:/wb.db"},
            ],
        }
        config = normalize_config(legacy)
        self.assertEqual(config["identity"]["device_id"], "sdg-abc-01")
        self.assertEqual(config["mqtt"]["host"], "10.0.0.9")
        self.assertTrue(config["watch"]["codex"]["enabled"])
        self.assertFalse(config["watch"]["workbuddy"]["enabled"])
        self.assertEqual(config["watch"]["codex"]["interval"], 2.0)
        self.assertEqual(enabled_platforms(config), ["codex"])

    def test_placeholder_device_id_is_dropped(self) -> None:
        self.assertEqual(normalize_config({"identity": {"device_id": "REPLACE_ME"}})["identity"]["device_id"], "")
        self.assertEqual(normalize_config(None)["watch"]["codex"]["max_tasks"], 1)
        self.assertEqual(normalize_config(None)["watch"]["workbuddy"]["max_tasks"], 1)

    def test_max_tasks_is_clamped(self) -> None:
        config = normalize_config({"watch": {"codex": {"max_tasks": 99}, "workbuddy": {"max_tasks": 0}}})
        self.assertEqual(config["watch"]["codex"]["max_tasks"], 10)
        self.assertEqual(config["watch"]["workbuddy"]["max_tasks"], 1)

    # ---- rolling window ----

    def test_window_keeps_newest_and_evicts_oldest(self) -> None:
        rows = [
            {"id": "old", "updated_at": 100},
            {"id": "mid", "updated_at": 200},
            {"id": "new", "updated_at": 300},
            {"id": "newest", "updated_at": 400},
        ]
        window = rank_items(rows, 3, workbuddy_recency)
        self.assertEqual([row["id"] for row in window], ["newest", "new", "mid"])
        self.assertEqual(evicted_ids(["old", "mid", "new"], ["newest", "new", "mid"]), ["old"])
        self.assertEqual(evicted_ids(["mid", "new"], ["newest", "new", "mid"]), [])

    def test_codex_window_prefers_recency_at(self) -> None:
        threads = [
            {"thread_id": "a", "recencyAt": 10, "updatedAt": 999},
            {"thread_id": "b", "recencyAt": 500, "updatedAt": 1},
        ]
        self.assertEqual(codex_recency(threads[0]), 999)
        self.assertEqual([t["thread_id"] for t in rank_items(threads, 1, codex_recency)], ["a"])

    # ---- regression: Workbuddy used to publish nothing for finished tasks ----

    def test_first_scan_publishes_finished_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = make_workbuddy_db(folder, [
                ("s1", "completed", 1000),
                ("s2", "completed", 2000),
                ("s3", "error", 3000),
                ("s4", "working", 4000),
                ("s5", "terminated", 5000),
            ])
            manager = self._manager()
            signatures: dict[str, tuple[str, int]] = {}
            dropped, published = manager._workbuddy_pass(database, 3, signatures)
            self.assertEqual(dropped, [])
            self.assertEqual(published, 3)
            ids = [payload["task_id"] for payload in manager.publisher.payloads]
            self.assertEqual(ids, ["s5", "s4", "s3"])
            completed = {payload["task_id"]: payload["detail"]["status"] for payload in manager.publisher.payloads}
            self.assertEqual(completed["s3"], "failed")
            self.assertEqual(completed["s5"], "canceled")

            # A second identical scan must not spam the broker.
            _, published_again = manager._workbuddy_pass(database, 3, signatures)
            self.assertEqual(published_again, 0)

    def test_new_task_enters_window_and_evicts_oldest(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = make_workbuddy_db(folder, [
                ("s1", "completed", 1000),
                ("s2", "completed", 2000),
                ("s3", "completed", 3000),
            ])
            manager = self._manager()
            signatures: dict[str, tuple[str, int]] = {}
            manager._workbuddy_pass(database, 3, signatures)

            connection = sqlite3.connect(database)
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                ("s4", r"C:\work\demo", "Task s4", "working", 9000, 9000, 9000, None),
            )
            connection.commit()
            connection.close()

            dropped, published = manager._workbuddy_pass(database, 3, signatures)
            self.assertEqual(dropped, ["s1"])
            self.assertEqual(published, 1)
            self.assertNotIn("workbuddy:s1", manager._states)
            self.assertIn("workbuddy:s4", manager._states)

    def test_workbuddy_reader_is_read_only_and_uses_real_ids(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = make_workbuddy_db(folder, [("real-session", "running", 2)])
            rows = read_workbuddy_sessions(database)
            self.assertEqual(rows[0]["id"], "real-session")
            self.assertEqual(project_name(rows[0]["cwd"], rows[0]["title"]), "demo")

    # ---- Doubao channel ----

    def test_doubao_state_words(self) -> None:
        self.assertEqual(classify_text("ButtonControl\t停止生成"), "working")
        self.assertEqual(classify_text("正在生成中"), "working")
        self.assertEqual(classify_text("深度思考中"), "thinking")
        self.assertEqual(classify_text("正在阅读 3 篇资料"), "thinking")
        self.assertEqual(classify_text("历史对话\n最近对话"), "idle")
        self.assertEqual(classify_text(""), "idle")

    def test_doubao_identity_is_ascii_only(self) -> None:
        self.assertEqual(conversation_title("季度报告整理 - 豆包"), "季度报告整理")
        self.assertEqual(conversation_title("Weekly report - Doubao"), "Weekly report")
        self.assertEqual(conversation_title("豆包"), "豆包")
        # A Chinese title cannot ride in the single-line ASCII message, so the
        # id becomes a stable hash while the readable text stays in detail.*_raw.
        hashed = session_key("", "季度报告整理 - 豆包")
        self.assertTrue(hashed.isascii())
        self.assertEqual(hashed, session_key("", "季度报告整理 - 豆包"))
        self.assertEqual(session_key("https://www.doubao.com/chat/7412?x=1", "忽略"), "7412")
        self.assertEqual(session_key("", ""), "main")

    def test_doubao_target_prefers_the_chat_page(self) -> None:
        targets = [
            {"type": "page", "url": "https://www.doubao.com/", "webSocketDebuggerUrl": "ws://a"},
            {"type": "page", "url": "https://www.doubao.com/chat/9", "webSocketDebuggerUrl": "ws://b"},
            {"type": "service_worker", "url": "https://www.doubao.com/sw.js", "webSocketDebuggerUrl": "ws://c"},
        ]
        self.assertEqual(pick_chat_target(targets)["webSocketDebuggerUrl"], "ws://b")
        self.assertIsNone(pick_chat_target([{"type": "page", "url": "x"}]))

    def test_doubao_config_is_off_by_default(self) -> None:
        config = normalize_config({"version": 2, "identity": {"device_id": "sdg-abc-01"}})
        self.assertFalse(config["watch"]["doubao"]["enabled"])
        self.assertEqual(config["watch"]["doubao"]["port"], 19226)
        self.assertNotIn("max_tasks", config["watch"]["doubao"])
        self.assertEqual(enabled_platforms(config), ["codex", "workbuddy"])
        clamped = normalize_config({"watch": {"doubao": {"enabled": True, "port": 99999}}})
        self.assertEqual(clamped["watch"]["doubao"]["port"], 19226)
        self.assertTrue(clamped["watch"]["doubao"]["enabled"])
        self.assertEqual(enabled_platforms(clamped), ["codex", "workbuddy", "doubao"])

    def test_doubao_state_machine_reports_each_edge_once(self) -> None:
        self.assertEqual(doubao_events(None, None), (None, []))
        working = {"state": "working", "session": "7412", "title": "季度报告整理", "source": "cdp"}
        previous, events = doubao_events(None, working)
        self.assertEqual([state for state, _ in events], ["working"])
        self.assertEqual(events[0][1]["session"], "7412")
        # Same poll twice must not republish.
        previous, events = doubao_events(previous, working)
        self.assertEqual(events, [])
        idle = {"state": "idle", "session": "7412", "title": "季度报告整理", "source": "cdp"}
        previous, events = doubao_events(previous, idle)
        self.assertEqual([state for state, _ in events], ["worked"])
        self.assertIsNone(previous)
        # Losing the source entirely also closes the conversation.
        previous, events = doubao_events({"state": "thinking", "session": "a", "title": "A"}, None)
        self.assertEqual([state for state, _ in events], ["worked"])
        self.assertIsNone(previous)

    def test_doubao_payload_stays_single_line_ascii(self) -> None:
        manager = self._manager()
        manager._publish("Doubao", session_key("", "季度报告整理 - 豆包"), "季度报告整理", "季度报告整理", "working", {})
        payload = manager.publisher.payloads[0]
        self.assertTrue(payload["message"].isascii())
        self.assertNotIn("\n", payload["message"])
        self.assertEqual(payload["detail"]["project_raw"], "季度报告整理")
        self.assertEqual(payload["detail"]["status_label"], "WORKING")

    def test_doubao_exe_is_found_off_the_system_drive(self) -> None:
        """A bare folder, an exe and a quoted DisplayIcon all resolve to the exe."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nested = root / "Doubao" / "app"
            nested.mkdir(parents=True)
            exe = nested / "Doubao.exe"
            exe.write_bytes(b"MZ")
            self.assertEqual(_exe_from(root), exe)
            self.assertEqual(_exe_from(f'"{root}",0'), exe)
            self.assertEqual(_exe_from(exe), exe)
            self.assertIsNone(_exe_from(root / "missing"))
            self.assertIsNone(_exe_from(""))

    def test_configured_doubao_exe_wins_and_is_kept_in_config(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "Doubao.exe"
            exe.write_bytes(b"MZ")
            self.assertEqual(doubao_exe_path(exe), str(exe))
        # A wrong path must not stop the discovery fallbacks.
        self.assertNotEqual(doubao_exe_path(r"Z:\nowhere\Doubao.exe"), r"Z:\nowhere\Doubao.exe")
        config = normalize_config({"watch": {"doubao": {"executable": f" {exe} "}}})
        self.assertEqual(config["watch"]["doubao"]["executable"], str(exe))
        self.assertEqual(normalize_config({})["watch"]["doubao"]["executable"], "")

    def test_debug_port_probe_fails_fast(self) -> None:
        started = time.time()
        with self.assertRaises(WebSocketError) as caught:
            cdp_targets(1, timeout=0.2)  # port 1 is never a DevTools endpoint
        self.assertLess(time.time() - started, 1.5)
        self.assertIn("未开放", str(caught.exception))
        self.assertFalse(debug_port_ready(1, timeout=0.2))


if __name__ == "__main__":
    unittest.main()
