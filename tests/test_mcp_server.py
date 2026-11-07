"""MCP dispatch contracts; no subprocess, source bot, or browser is started."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.cvbot import CVBot, CVError
from scripts import mcp_server


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.bot = mock.Mock(spec=CVBot)

    def request(self, method, params=None):
        request = {"jsonrpc": "2.0", "id": 17, "method": method}
        if params is not None:
            request["params"] = params
        return mcp_server.handle_request(request, self.bot)

    def test_initialize_advertises_tools_without_touching_source(self):
        response = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "isolated-test", "version": "1"},
        })
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 17)
        self.assertIsInstance(response["result"]["protocolVersion"], str)
        self.assertIn("tools", response["result"]["capabilities"])
        self.assertEqual(self.bot.method_calls, [])

    def test_tool_discovery_exposes_preview_and_local_queue_only(self):
        response = self.request("tools/list")
        tools = response["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertTrue({
            "cv_connect_workspace", "cv_status", "cv_list_vacancies", "cv_get_vacancy",
            "cv_draft_letter", "cv_mark_vacancy", "cv_prepare_run", "cv_get_run",
        }.issubset(names))
        self.assertFalse({"cv_run_action", "cv_execute", "cv_apply", "cv_login"} & names)
        self.assertEqual(len(names), len(tools))
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertEqual(tool["inputSchema"]["type"], "object")
        self.assertEqual(self.bot.method_calls, [])

    def test_tools_call_returns_json_tool_content(self):
        self.bot.status.return_value = {"connected": True, "browser_started": False}
        response = self.request("tools/call", {"name": "cv_status", "arguments": {}})
        self.bot.status.assert_called_once_with()
        result = response["result"]
        self.assertFalse(result.get("isError", False))
        content = [item["text"] for item in result["content"] if item["type"] == "text"]
        self.assertEqual(json.loads("".join(content)), {"connected": True, "browser_started": False})

    def test_operational_failure_is_a_tool_error(self):
        self.bot.get_vacancy.side_effect = CVError("Вакансия 123 не найдена")
        response = self.request("tools/call", {
            "name": "cv_get_vacancy", "arguments": {"vacancy_id": 123},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertIn("123", response["result"]["content"][0]["text"])

    def test_unknown_method_is_jsonrpc_error(self):
        response = self.request("execute/arbitrary-command")
        self.assertEqual(response["error"]["code"], -32601)
        self.assertEqual(self.bot.method_calls, [])

    def test_unknown_tool_and_malformed_arguments_are_rejected(self):
        for params in (
            {"name": "cv_execute", "arguments": {"command": "whoami"}},
            {"name": "cv_status", "arguments": []},
            {"name": ["cv_status"], "arguments": {}},
        ):
            with self.subTest(params=params):
                response = self.request("tools/call", params)
                self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(self.bot.method_calls, [])

    def test_initialized_notification_has_no_response(self):
        response = mcp_server.handle_request({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }, self.bot)
        self.assertIsNone(response)
        self.assertEqual(self.bot.method_calls, [])


if __name__ == "__main__":
    unittest.main()
