import sys
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


# 单元测试不建立真实 VNC/LLM 连接；在导入被测模块前提供最小依赖替身。
vncdotool_stub = types.ModuleType("vncdotool")
vncdotool_stub.api = types.SimpleNamespace(connect=lambda *_args, **_kwargs: None)
sys.modules["vncdotool"] = vncdotool_stub

llm_stub = types.ModuleType("llm_client")
llm_stub.chat_vision = lambda *_args, **_kwargs: ""
llm_stub.chat_text = lambda *_args, **_kwargs: ""
llm_stub.token_tracker = types.SimpleNamespace(
    reset=lambda: None,
    save=lambda *_args, **_kwargs: None,
)
sys.modules["llm_client"] = llm_stub

knowledge_stub = types.ModuleType("knowledge_loader")
knowledge_stub.get_summary = lambda *_args, **_kwargs: ""
sys.modules["knowledge_loader"] = knowledge_stub

from input_method import InputMethodConfig, InputMethodController, UnicodeInputUnsupported
from rpa_agent import RPAgent
from task_policy import AGENT_CAPABILITY_LOGIN_URL, QueryOnlyPolicy
from tools import open_application, open_url, open_webpage
from vnc_client import VNCClient


class FakeProtocol:
    def __init__(self):
        self.events = []
        self.screen = Image.new("RGB", (100, 80), "white")

    def keyDown(self, key):
        self.events.append(("down", key))

    def keyUp(self, key):
        self.events.append(("up", key))

    def keyPress(self, key):
        self.events.append(("press", key))

    def mouseMove(self, x, y):
        self.events.append(("move", (x, y)))

    def mouseDown(self, button):
        self.events.append(("mouse_down", button))

    def mouseUp(self, button):
        self.events.append(("mouse_up", button))


def make_vnc(
    *,
    host="remote.example",
    system="win",
    local_sendinput=False,
):
    vnc = VNCClient(
        host=host,
        system=system,
        local_sendinput=local_sendinput,
        key_interval=0,
    )
    vnc._client = FakeProtocol()
    return vnc


def make_agent(vnc=None):
    agent = RPAgent.__new__(RPAgent)
    # Most tests assume an English desktop; preflight tests override this reader.
    agent._read_focused_ime_mode = lambda: "english"
    agent.vnc = vnc or make_vnc()
    agent.system = agent.vnc.system
    agent.step = 1
    agent.pending_input = None
    agent.input_retry_count = 0
    agent.ime_operation_counts = {}
    agent.failure_reason = ""
    agent._repeat_guidance = ""
    agent._last_action_executed = False
    agent._abort_requested = False
    agent.login_progress = "idle"
    agent._active_login_input_stage = None
    agent._active_skill_action = None
    agent._last_focus_coordinates = None
    agent.task_plan = []
    agent.current_plan_idx = -1
    agent.task = ""
    agent.knowledge_summary = ""
    agent.task_policy = QueryOnlyPolicy.for_task("")
    return agent


class InputMethodTests(unittest.TestCase):
    def test_system_ime_shortcuts_are_centralized(self):
        self.assertEqual(InputMethodConfig.for_system("windows").toggle_chinese_english, "shift")
        self.assertEqual(InputMethodConfig.for_system("linux").toggle_chinese_english, "super+space")
        self.assertEqual(InputMethodConfig.for_system("mac").toggle_chinese_english, "ctrl+space")

    def test_field_normalization_is_narrow_and_keeps_zero(self):
        normalize = InputMethodController.normalize_text
        self.assertEqual(normalize("１２３＠abc．com", "email"), "123@abc.com")
        self.assertEqual(normalize("ｈｔｔｐｓ：／／example。com", "url"), "https://example.com")
        self.assertEqual(normalize("example.com", "url"), "https://example.com")
        self.assertEqual(normalize("localhost:5000", "url"), "http://localhost:5000")
        self.assertEqual(normalize("127.0.0.1:7860", "url"), "http://127.0.0.1:7860")
        self.assertEqual(normalize(" １２ ３ ", "number"), "123")
        self.assertEqual(normalize(0, "number"), "0")
        self.assertEqual(normalize("echo  a\nb", "command"), "echo  a\nb")
        self.assertEqual(normalize("x = １", "code"), "x = １")
        self.assertEqual(normalize("１２３", "password"), "１２３")
        self.assertEqual(normalize("Ａlice", "username"), "Ａlice")
        with self.assertRaises(ValueError):
            normalize(None, "text")
        with self.assertRaises(ValueError):
            normalize("   ", "url")

    def test_ascii_vnc_typing_and_atomic_control_validation(self):
        vnc = make_vnc()
        with patch("vnc_client.time.sleep"):
            vnc.type_text("a\nb\tc")
        self.assertEqual(
            vnc._client.events,
            [
                ("press", "a"),
                ("press", "enter"),
                ("press", "b"),
                ("press", "tab"),
                ("press", "c"),
            ],
        )

        vnc._client.events.clear()
        with self.assertRaises(ValueError):
            vnc.type_text("a\x00b")
        self.assertEqual(vnc._client.events, [])

        with self.assertRaises(ValueError):
            vnc.ime.type_value("a\x00b", "text", replace=True)
        self.assertEqual(vnc._client.events, [])

    def test_colon_holds_shift_while_other_printable_keys_are_unchanged(self):
        vnc = make_vnc()
        with patch("vnc_client.time.sleep"):
            vnc.type_text("A:@_?")
        self.assertEqual(
            vnc._client.events,
            [
                ("press", "A"),
                ("down", "shift"), ("press", ";"), ("up", "shift"),
                ("press", "@"),
                ("press", "_"), ("press", "?"),
            ],
        )

    def test_windows_edge_url_uses_current_browser_address_bar(self):
        steps = open_url("http://127.0.0.1:7010/path", browser="edge")
        self.assertEqual(
            [step["action"] for step in steps],
            ["maximize_window", "press", "press", "wait", "input_text", "wait"],
        )
        self.assertEqual(steps[1]["params"]["key"], "escape")
        self.assertEqual(steps[2]["params"]["key"], "ctrl+l")
        self.assertEqual(
            steps[4]["params"]["text"],
            "http://127.0.0.1:7010/path",
        )
        self.assertEqual(steps[4]["params"]["field_type"], "url")
        self.assertNotIn("win+r", [
            step.get("params", {}).get("key") for step in steps
        ])

    def test_local_unicode_requires_all_explicit_gates(self):
        with patch("vnc_client.os.name", "nt"):
            self.assertFalse(make_vnc(host="127.0.0.1").supports_local_unicode_input())
            self.assertTrue(
                make_vnc(
                    host="127.0.0.1", local_sendinput=True
                ).supports_local_unicode_input()
            )
            self.assertFalse(
                make_vnc(
                    host="remote.example", local_sendinput=True
                ).supports_local_unicode_input()
            )
            self.assertFalse(
                make_vnc(
                    host="127.0.0.1", system="linux", local_sendinput=True
                ).supports_local_unicode_input()
            )

    def test_unicode_and_emoji_use_utf16_sendinput_only(self):
        vnc = make_vnc(host="127.0.0.1", local_sendinput=True)
        emitted = []
        with patch.object(vnc, "supports_local_unicode_input", return_value=True), \
                patch.object(vnc, "_send_windows_keyboard_events", side_effect=emitted.append), \
                patch("vnc_client.time.sleep"):
            value, mode = vnc.ime.type_value("中文😀", "text", replace=True)

        self.assertEqual((value, mode), ("中文😀", "chinese"))
        self.assertEqual(vnc._client.events, [])
        self.assertEqual(
            emitted[0],
            [(0x11, 0, 0), (0x41, 0, 0), (0x41, 0, 0x0002), (0x11, 0, 0x0002)],
        )
        unicode_events = emitted[1:]
        units = [event[0][1] for event in unicode_events]
        self.assertEqual(units[:2], [ord("中"), ord("文")])
        self.assertEqual(units[2:], [0xD83D, 0xDE00])
        for event in unicode_events:
            self.assertEqual(event, [(0, event[0][1], 0x0004), (0, event[0][1], 0x0006)])

    def test_remote_unicode_fails_before_any_field_mutation(self):
        vnc = make_vnc()
        with self.assertRaises(UnicodeInputUnsupported):
            vnc.ime.type_value(
                "中文",
                "text",
                replace=True,
                switch_language=True,
                cancel_composition=True,
            )
        self.assertEqual(vnc._client.events, [])

    def test_replace_empty_really_clears_without_submitting(self):
        vnc = make_vnc()
        with patch("vnc_client.time.sleep"):
            value, _ = vnc.ime.type_value("", "text", replace=True)
        self.assertEqual(value, "")
        self.assertEqual(
            vnc._client.events,
            [
                ("down", "ctrl"),
                ("press", "a"),
                ("up", "ctrl"),
                ("press", "bsp"),
            ],
        )
        self.assertNotIn(("press", "enter"), vnc._client.events)

    def test_ime_recovery_order_and_non_ime_retry(self):
        vnc = make_vnc()
        with patch("vnc_client.time.sleep"):
            vnc.ime.type_value(
                "abc", "text", replace=True,
                switch_language=True, cancel_composition=True,
            )
        presses = [event[1] for event in vnc._client.events if event[0] == "press"]
        self.assertEqual(presses, ["esc", "shift", "a", "a", "b", "c"])

        vnc._client.events.clear()
        with patch("vnc_client.time.sleep"):
            vnc.ime.type_value("abc", "text", replace=True)
        presses = [event[1] for event in vnc._client.events if event[0] == "press"]
        self.assertNotIn("esc", presses)
        self.assertNotIn("shift", presses)

    def test_macros_stop_after_input_for_visual_verification(self):
        self.assertEqual(
            [step["action"] for step in open_application("edge")],
            ["press", "wait", "input_text", "wait"],
        )
        self.assertEqual(
            [step["action"] for step in open_url("https://example.com")],
            ["maximize_window", "press", "press", "wait", "input_text", "wait"],
        )
        mac_url_steps = open_url("example.com", system="mac")
        self.assertEqual(mac_url_steps[2]["params"]["key"], "cmd+l")
        self.assertEqual(mac_url_steps[4]["params"]["field_type"], "url")
        self.assertEqual(open_url("example.com", system="mac", browser="chrome")[2]["params"]["key"], "cmd+l")
        username_steps = open_webpage(
            needlogin=True,
            username="admin",
            loginCoordinates={"x": 0.4, "y": 0.3},
            stage="username",
        )
        self.assertEqual(
            [step["action"] for step in username_steps],
            ["click", "wait", "input_text", "wait"],
        )
        self.assertEqual(username_steps[2]["params"]["field_type"], "username")


class AgentInputStateTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (100, 80), "white")

    def test_input_requires_visual_verification_before_click(self):
        agent = make_agent()
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"):
            self.assertTrue(agent._execute_action({
                "action": "input_text",
                "params": {"text": "admin", "field_type": "username"},
            }, self.image))
            self.assertEqual(
                agent.vnc._client.events[:3],
                [("down", "ctrl"), ("press", "a"), ("up", "ctrl")],
            )
            before = list(agent.vnc._client.events)
            self.assertTrue(agent._execute_action({
                "action": "click", "params": {"x": 0.5, "y": 0.5},
            }, self.image))
            self.assertEqual(before, agent.vnc._client.events)
            self.assertFalse(agent._last_action_executed)

            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {"observed_text": "admin", "readable": True},
            }, self.image))
            self.assertTrue(agent.pending_input["verified"])
            self.assertTrue(agent._execute_action({
                "action": "click", "params": {"x": 0.5, "y": 0.5},
            }, self.image))
            self.assertTrue(agent._last_action_executed)
            self.assertIsNone(agent.pending_input)

    def test_transport_errors_do_not_create_success_state(self):
        agent = make_agent()
        def fail_after_partial_input(*_args, **_kwargs):
            agent.vnc._client.events.append(("press", "a"))
            raise RuntimeError("boom")

        with patch.object(agent.vnc.ime, "type_value", side_effect=fail_after_partial_input), \
                patch("rpa_agent.time.sleep"):
            self.assertFalse(agent._execute_action({
                "action": "input_text",
                "params": {"text": "abc", "field_type": "text"},
            }, self.image))
        self.assertIsNone(agent.pending_input)
        self.assertEqual(agent.input_retry_count, 0)
        self.assertFalse(agent._last_action_executed)
        self.assertTrue(agent._abort_requested)
        self.assertIn("输入失败", agent.failure_reason)

        agent._abort_requested = False
        agent.pending_input = {
            "text": "abc",
            "field_type": "text",
            "verified": False,
            "verification_attempted": True,
            "cause": "partial",
        }
        original = dict(agent.pending_input)
        with patch.object(agent.vnc.ime, "type_value", side_effect=RuntimeError("retry boom")), \
                patch("rpa_agent.time.sleep"):
            self.assertFalse(agent._execute_action({
                "action": "retry_text_input",
                "params": {"text": "abc", "field_type": "text", "cause": "partial"},
            }, self.image))
        self.assertEqual(agent.pending_input, original)
        self.assertEqual(agent.input_retry_count, 0)
        self.assertTrue(agent._abort_requested)
        self.assertIn("未知状态", agent._repeat_guidance)

    def test_retry_and_ime_require_matching_visual_diagnosis(self):
        agent = make_agent()
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "input_text",
                "params": {"text": "abcdef", "field_type": "text"},
            }, self.image))
            before = list(agent.vnc._client.events)
            self.assertFalse(agent._execute_action({
                "action": "retry_text_input",
                "params": {"text": "abcdef", "field_type": "text", "cause": "partial"},
            }, self.image))
            self.assertEqual(before, agent.vnc._client.events)
            self.assertIn("尚未", agent.failure_reason)

            self.assertFalse(agent._execute_action({
                "action": "ime_operation",
                "params": {"operation": "toggle_language"},
            }, self.image))
            self.assertEqual(before, agent.vnc._client.events)

            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {
                    "observed_text": "abc",
                    "readable": True,
                    "cause": "partial",
                },
            }, self.image))
            self.assertFalse(agent._execute_action({
                "action": "retry_text_input",
                "params": {"text": "abcdef", "field_type": "text", "cause": "ime"},
            }, self.image))
            self.assertEqual(before, agent.vnc._client.events)
            self.assertIn("不一致", agent.failure_reason)

            self.assertTrue(agent._execute_action({
                "action": "retry_text_input",
                "params": {"text": "abcdef", "field_type": "text", "cause": "partial"},
            }, self.image))
        self.assertEqual(agent.input_retry_count, 1)

    def test_instance_system_prompt_matches_execution_system(self):
        linux_agent = RPAgent(system="linux")
        mac_agent = RPAgent(system="mac")
        self.assertEqual(linux_agent.system, "linux")
        self.assertIn("打开终端：Ctrl+Alt+T", linux_agent.system_prompt)
        self.assertIn("当前操作系统为：\nLINUX", linux_agent.system_prompt)
        self.assertEqual(mac_agent.system, "mac")
        self.assertIn("Cmd+Space", mac_agent.system_prompt)
        self.assertIn("当前操作系统为：\nMACOS", mac_agent.system_prompt)

    def test_invalid_empty_macro_is_not_marked_as_executed(self):
        agent = make_agent()
        with patch("builtins.print"):
            self.assertFalse(agent._execute_action({
                "action": "skill_open_app",
                "params": {"application": ""},
            }, self.image))
        self.assertFalse(agent._last_action_executed)
        self.assertIn("参数非法", agent.failure_reason)

    def test_legacy_type_also_requires_verification(self):
        agent = make_agent()
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "type", "params": {"text": "legacy"},
            }, self.image))
            self.assertIsNotNone(agent.pending_input)
            before = list(agent.vnc._client.events)
            self.assertTrue(agent._execute_action({
                "action": "press", "params": {"key": "enter"},
            }, self.image))
        self.assertEqual(before, agent.vnc._client.events)
        self.assertFalse(agent._last_action_executed)

    def test_failed_verification_after_retry_aborts_instead_of_deadlocking(self):
        agent = make_agent()
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "input_text",
                "params": {"text": "abcdef", "field_type": "text"},
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {"observed_text": "abc", "readable": True},
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "retry_text_input",
                "params": {"text": "abcdef", "field_type": "text", "cause": "partial"},
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {"observed_text": "abc", "readable": True},
            }, self.image))
        self.assertTrue(agent._abort_requested)
        self.assertIsNone(agent.pending_input)
        self.assertIn("唯一一次纠错后", agent.failure_reason)

    def test_single_character_misread_after_retry_requires_passive_recheck(self):
        agent = make_agent()
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "input_text",
                "params": {"text": "zhouhao", "field_type": "username"},
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {
                    "observed_text": "zhouhao",
                    "readable": True,
                    "ime_visible": True,
                },
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "retry_text_input",
                "params": {
                    "text": "zhouhao",
                    "field_type": "username",
                    "cause": "ime",
                },
            }, self.image))

            # A caret touching the final "o" can be transcribed as "d".  One
            # such observation is insufficient to overwrite again or abort.
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {
                    "observed_text": "zhouhad",
                    "readable": True,
                    "cause": "format",
                    "ime_visible": False,
                },
            }, self.image))
            self.assertFalse(agent._abort_requested)
            self.assertTrue(agent.pending_input["visual_recheck_required"])
            self.assertFalse(agent.pending_input["visual_recheck_waited"])

            event_count = len(agent.vnc._client.events)
            self.assertTrue(agent._execute_action({
                "action": "retry_text_input",
                "params": {
                    "text": "zhouhao",
                    "field_type": "username",
                    "cause": "format",
                },
            }, self.image))
            self.assertFalse(agent._last_action_executed)
            self.assertEqual(len(agent.vnc._client.events), event_count)

            self.assertTrue(agent._execute_action({
                "action": "abort_input",
                "params": {"reason": "single ambiguous frame"},
            }, self.image))
            self.assertFalse(agent._last_action_executed)
            self.assertFalse(agent._abort_requested)
            self.assertIsNotNone(agent.pending_input)

            self.assertTrue(agent._execute_action({
                "action": "wait", "params": {"seconds": 0.6},
            }, self.image))
            self.assertTrue(agent.pending_input["visual_recheck_waited"])
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {
                    "observed_text": "zhouhao",
                    "readable": True,
                    # The VLM may carry the prior diagnosis forward even after
                    # the candidate window is gone; current-frame evidence wins.
                    "cause": "ime",
                    "ime_visible": False,
                },
            }, self.image))

        self.assertFalse(agent._abort_requested)
        self.assertTrue(agent.pending_input["verified"])
        self.assertEqual(agent.login_progress, "idle")

    def test_confirmed_single_character_mismatch_after_retry_still_aborts(self):
        agent = make_agent()
        agent.pending_input = {
            "text": "zhouhao",
            "field_type": "username",
            "verified": False,
            "verification_attempted": False,
        }
        agent.input_retry_count = 1
        mismatch = {
            "action": "verify_text_input",
            "params": {
                "observed_text": "zhouhad",
                "readable": True,
                "cause": "format",
                "ime_visible": False,
            },
        }
        with patch("rpa_agent.time.sleep"), patch("builtins.print"):
            self.assertTrue(agent._execute_action(mismatch, self.image))
            self.assertFalse(agent._abort_requested)
            self.assertTrue(agent._execute_action({
                "action": "wait", "params": {"seconds": 0.6},
            }, self.image))
            self.assertTrue(agent._execute_action(mismatch, self.image))

        self.assertTrue(agent._abort_requested)
        self.assertIsNone(agent.pending_input)
        self.assertIn("唯一一次纠错后", agent.failure_reason)

    def test_initial_one_character_recheck_can_then_use_the_single_retry(self):
        agent = make_agent()
        agent.pending_input = {
            "text": "target",
            "field_type": "text",
            "verified": False,
            "verification_attempted": False,
        }
        mismatch = {
            "action": "verify_text_input",
            "params": {
                "observed_text": "targel",
                "readable": True,
                "cause": "format",
                "ime_visible": False,
            },
        }
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action(mismatch, self.image))
            self.assertTrue(agent._execute_action({
                "action": "wait", "params": {"seconds": 0.6},
            }, self.image))
            self.assertTrue(agent._execute_action(mismatch, self.image))
            self.assertFalse(agent._abort_requested)
            self.assertNotIn("visual_recheck_required", agent.pending_input)
            self.assertTrue(agent._execute_action({
                "action": "retry_text_input",
                "params": {
                    "text": "target",
                    "field_type": "text",
                    "cause": "format",
                },
            }, self.image))

        self.assertEqual(agent.input_retry_count, 1)
        self.assertFalse(agent._abort_requested)

    def test_unreadable_post_retry_frame_gets_one_passive_recheck(self):
        agent = make_agent()
        agent.pending_input = {
            "text": "exact-value",
            "field_type": "text",
            "verified": False,
            "verification_attempted": False,
        }
        agent.input_retry_count = 1
        unreadable = {
            "action": "verify_text_input",
            "params": {
                "observed_text": "",
                "readable": False,
                "cause": "unknown",
                "ime_visible": False,
            },
        }
        with patch("rpa_agent.time.sleep"), patch("builtins.print"):
            self.assertTrue(agent._execute_action(unreadable, self.image))
            self.assertFalse(agent._abort_requested)
            self.assertTrue(agent.pending_input["visual_recheck_required"])
            self.assertTrue(agent._execute_action({
                "action": "wait", "params": {"seconds": 0.6},
            }, self.image))
            self.assertTrue(agent._execute_action(unreadable, self.image))

        self.assertTrue(agent._abort_requested)
        self.assertIsNone(agent.pending_input)

    def test_focus_recovery_rejects_click_on_another_field(self):
        agent = make_agent()
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "click", "params": {"x": 0.4, "y": 0.3},
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "input_text",
                "params": {"text": "admin", "field_type": "username"},
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {"observed_text": "", "readable": True},
            }, self.image))
            before = list(agent.vnc._client.events)
            self.assertTrue(agent._execute_action({
                "action": "click", "params": {"x": 0.8, "y": 0.8},
            }, self.image))
            self.assertEqual(before, agent.vnc._client.events)
            self.assertFalse(agent._last_action_executed)
            self.assertTrue(agent._execute_action({
                "action": "click", "params": {"x": 0.41, "y": 0.31},
            }, self.image))
        self.assertTrue(agent.pending_input["refocused"])
        self.assertTrue(agent._last_action_executed)

    def test_controlled_login_retry_stays_inside_query_policy(self):
        agent = make_agent()
        agent.task = "查询智能问数和新SQL生成"
        agent.task_policy = QueryOnlyPolicy.for_task(agent.task)
        agent.login_progress = "username_pending"
        agent.pending_input = {
            "text": "zhouhao",
            "field_type": "username",
            "verified": False,
            "verification_attempted": True,
            "cause": "focus",
            "refocused": True,
            "focus_coordinates": (0.75, 0.215),
            "login_stage": "username",
        }

        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            continued = agent._execute_action({
                "action": "retry_text_input",
                "params": {
                    "text": "zhouhao",
                    "field_type": "username",
                    "cause": "focus",
                },
            }, self.image)

        self.assertTrue(continued)
        self.assertTrue(agent._last_action_executed)
        self.assertFalse(agent._abort_requested)
        self.assertEqual(agent.input_retry_count, 1)

    def test_repeated_ime_operation_is_stopped(self):
        agent = make_agent()
        first = {"action": "ime_operation", "params": {"operation": "toggle"}}
        alias = {"action": "ime_operation", "params": {"operation": "中英文切换"}}
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"):
            self.assertTrue(agent._execute_action(first, self.image))
            self.assertFalse(agent._execute_action(alias, self.image))
        shift_presses = [
            event for event in agent.vnc._client.events if event == ("press", "shift")
        ]
        self.assertEqual(len(shift_presses), 1)
        self.assertIn("停止重复", agent.failure_reason)

    def test_manual_ime_switch_is_reverified_and_not_toggled_again_on_retry(self):
        agent = make_agent()
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "input_text",
                "params": {"text": "abc", "field_type": "text"},
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {"observed_text": "", "readable": True, "cause": "ime"},
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "ime_operation",
                "params": {"operation": "toggle_language"},
            }, self.image))
            self.assertFalse(agent.pending_input["verification_attempted"])
            self.assertFalse(agent._execute_action({
                "action": "retry_text_input",
                "params": {"text": "abc", "field_type": "text", "cause": "ime"},
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {"observed_text": "", "readable": True, "cause": "ime"},
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "retry_text_input",
                "params": {"text": "abc", "field_type": "text", "cause": "ime"},
            }, self.image))
        shift_presses = [
            event for event in agent.vnc._client.events if event == ("press", "shift")
        ]
        self.assertEqual(len(shift_presses), 1)

    def test_skill_stops_before_submit_when_unicode_transport_fails(self):
        agent = make_agent()
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"):
            self.assertFalse(agent._execute_action({
                "action": "skill_open_app",
                "params": {"application": "记事本"},
            }, self.image))
        pressed = [event[1] for event in agent.vnc._client.events if event[0] == "press"]
        self.assertIn("super", pressed)
        self.assertNotIn("enter", pressed)
        self.assertFalse(agent._last_action_executed)

    def test_login_cannot_advance_or_submit_before_each_verification(self):
        agent = make_agent()
        username = {
            "action": "skill_open_webpage",
            "params": {
                "needlogin": True,
                "stage": "username",
                "username": "admin",
                "loginCoordinates": {"x": 0.4, "y": 0.3},
            },
        }
        password = {
            "action": "skill_open_webpage",
            "params": {
                "needlogin": True,
                "stage": "password",
                "password": "123456",
                "pdCoordinates": {"x": 0.4, "y": 0.4},
            },
        }
        submit = {
            "action": "skill_open_webpage",
            "params": {
                "needlogin": True,
                "stage": "submit",
                "buttonCoordinates": {"x": 0.4, "y": 0.5},
            },
        }
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertFalse(agent._execute_action(submit, self.image))
            self.assertFalse(agent._last_action_executed)
            self.assertTrue(agent._execute_action(username, self.image))
            before_password = list(agent.vnc._client.events)
            self.assertTrue(agent._execute_action(password, self.image))
            self.assertFalse(agent._last_action_executed)
            self.assertEqual(before_password, agent.vnc._client.events)

            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {"observed_text": "admin", "readable": True},
            }, self.image))
            self.assertTrue(agent._execute_action(submit, self.image))
            self.assertFalse(agent._last_action_executed)
            before_direct_click = list(agent.vnc._client.events)
            self.assertTrue(agent._execute_action({
                "action": "click", "params": {"x": 0.4, "y": 0.5},
            }, self.image))
            self.assertEqual(before_direct_click, agent.vnc._client.events)
            self.assertFalse(agent._last_action_executed)
            self.assertTrue(agent._execute_action(password, self.image))
            self.assertEqual(agent.pending_input["field_type"], "password")

            before_submit = list(agent.vnc._client.events)
            self.assertTrue(agent._execute_action(submit, self.image))
            self.assertFalse(agent._last_action_executed)
            self.assertEqual(before_submit, agent.vnc._client.events)

            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {"observed_text": "••••••", "readable": True},
            }, self.image))
            before_direct_submit = list(agent.vnc._client.events)
            self.assertTrue(agent._execute_action({
                "action": "click", "params": {"x": 0.4, "y": 0.5},
            }, self.image))
            self.assertEqual(before_direct_submit, agent.vnc._client.events)
            self.assertFalse(agent._last_action_executed)
            before_valid_submit = list(agent.vnc._client.events)
            self.assertTrue(agent._execute_action(submit, self.image))
            self.assertTrue(agent._last_action_executed)
            self.assertIsNone(agent.pending_input)
            self.assertEqual(agent.login_progress, "submitted")
            self.assertGreater(len(agent.vnc._client.events), len(before_valid_submit))
            self.assertIn(("mouse_down", 1), agent.vnc._client.events[len(before_valid_submit):])
            self.assertIn(("mouse_up", 1), agent.vnc._client.events[len(before_valid_submit):])

    def test_password_mask_is_not_accepted_while_ime_composition_is_visible(self):
        agent = make_agent()
        agent.login_progress = "password_pending"
        agent.pending_input = {
            "text": "123456",
            "field_type": "password",
            "verified": False,
            "verification_attempted": False,
            "login_stage": "password",
        }
        with patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {
                    "observed_text": "••••••",
                    "readable": True,
                    "ime_visible": True,
                },
            }, self.image))
        self.assertFalse(agent.pending_input["verified"])
        self.assertEqual(agent.pending_input["cause"], "ime")
        self.assertEqual(agent.login_progress, "password_pending")

    def test_query_scope_blocks_mutating_click_before_mouse_event(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("在智能体能力页面做查询测试")
        with patch("builtins.print"):
            continued = agent._execute_action({
                "action": "click",
                "params": {"x": 0.5, "y": 0.5, "target": "新增场景"},
            }, self.image)
        self.assertFalse(continued)
        self.assertTrue(agent._abort_requested)
        self.assertEqual(agent.vnc._client.events, [])
        self.assertIn("写操作", agent.failure_reason)

    def test_query_scope_only_accepts_designated_login_url(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        with patch("builtins.print"):
            self.assertFalse(agent._execute_action({
                "action": "skill_open_url",
                "params": {"url": "http://172.19.133.168:7010/other.html"},
            }, self.image))
        self.assertEqual(agent.vnc._client.events, [])

        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        steps = agent.task_policy.build_plan("")
        self.assertIn(AGENT_CAPABILITY_LOGIN_URL, agent.task_policy.prompt)
        self.assertTrue(steps)
        joined = " ".join(item["content"] for item in steps)
        for forbidden in ("新增", "编辑", "删除", "保存", "发布"):
            self.assertNotIn(forbidden, joined)

    def test_escape_from_address_suggestions_requires_url_reverification(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent.pending_input = {
            "text": AGENT_CAPABILITY_LOGIN_URL,
            "field_type": "url",
            "verified": True,
            "verification_attempted": True,
        }

        with patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "press", "params": {"key": "escape"},
            }, self.image))
        self.assertIsNotNone(agent.pending_input)
        self.assertFalse(agent.pending_input["verified"])
        self.assertIn("再次调用 verify_text_input", agent._repeat_guidance)
        self.assertEqual(agent.vnc._client.events, [("press", "esc")])

        with patch("builtins.print"), patch("rpa_agent.chat_vision", return_value=json.dumps({
            "observed_text": AGENT_CAPABILITY_LOGIN_URL, "readable": True, "ime_visible": False,
        })):
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {
                    "observed_text": AGENT_CAPABILITY_LOGIN_URL,
                    "readable": True,
                    "cause": "unknown",
                    "ime_visible": False,
                },
            }, self.image))
            self.assertTrue(agent._execute_action({
                "action": "press", "params": {"key": "enter"},
            }, self.image))
        self.assertIsNone(agent.pending_input)
        self.assertEqual(
            agent.vnc._client.events,
            [("press", "esc"), ("press", "enter")],
        )

    def test_query_scope_done_gate_requires_every_plan_to_pass(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("在智能体能力页面查询")
        agent.task_plan = agent.task_policy.build_plan("")
        with patch("builtins.print"):
            self.assertTrue(agent._execute_action({"action": "done", "params": {}}, self.image))
        self.assertFalse(agent._last_action_executed)
        for item in agent.task_plan:
            item["completed"] = True
            item["passed"] = True
        with patch("builtins.print"):
            self.assertFalse(agent._execute_action({"action": "done", "params": {}}, self.image))
        self.assertTrue(agent._last_action_executed)

    def test_query_search_is_phase_bound_and_counted_once(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task(
            "在体验中心的智能体能力页面查询智能问数和新SQL生成"
        )
        agent.task_plan = agent.task_policy.build_plan(agent.task)
        for item in agent.task_plan[:3]:
            item["completed"] = True
            item["passed"] = True
        agent.current_plan_idx = 3
        agent.login_progress = "submitted"
        search = {
            "action": "click",
            "params": {"x": 0.5, "y": 0.5, "target": "搜索按钮"},
        }
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action(search, self.image))
            self.assertEqual(agent._query_search_click_count, 1)
            before = list(agent.vnc._client.events)
            self.assertFalse(agent._execute_action(search, self.image))
        self.assertEqual(before, agent.vnc._client.events)
        self.assertTrue(agent._abort_requested)
        self.assertIn("搜索", agent.failure_reason)

    def test_query_search_subtask_requires_executed_after_frame_evidence(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task(
            "在体验中心的智能体能力页面查询智能问数和新SQL生成"
        )
        agent.task_plan = agent.task_policy.build_plan(agent.task)
        for item in agent.task_plan[:3]:
            item["completed"] = True
            item["passed"] = True
        agent.current_plan_idx = 3
        agent._query_search_click_count = 1
        action = {
            "action": "subtask_done",
            "params": {"passed": True, "result": "结果表格已加载"},
        }
        with patch.object(agent, "_verify_visible_state") as verifier, \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action(action, self.image))
        verifier.assert_not_called()
        self.assertFalse(agent._last_action_executed)
        self.assertIn("动作后截图", agent._repeat_guidance)

        observed_pixels = []
        with tempfile.TemporaryDirectory() as temp_dir:
            verification_path = Path(temp_dir) / "search_after.png"
            Image.new("RGB", self.image.size, (12, 34, 56)).save(verification_path)
            agent._query_last_search_evidence = {
                "step": 8,
                "plan_index": 3,
                "after_captured": True,
                "verification_frame": str(verification_path),
            }

            def verify_saved_search_frame(frame, **_kwargs):
                observed_pixels.append(frame.getpixel((0, 0)))
                return {
                    "passed": True,
                    "state": "results",
                    "evidence": ["结果表格可见"],
                    "reason": "",
                }

            with patch("rpa_agent.SCREENSHOT_DIR", Path(temp_dir)), \
                    patch.object(
                        agent,
                        "_verify_visible_state",
                        side_effect=verify_saved_search_frame,
                    ), patch("builtins.print"):
                self.assertTrue(agent._execute_action(action, self.image))
        self.assertTrue(agent._last_action_executed)
        self.assertTrue(agent._subtask_result["passed"])
        self.assertEqual(observed_pixels, [(12, 34, 56)])

    def test_query_planning_requires_verified_login_entry(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        with patch.object(agent, "_verify_visible_state") as verifier, \
                patch("builtins.print"):
            self.assertFalse(agent._execute_action(
                {"action": "planning", "params": {}}, self.image
            ))
        verifier.assert_not_called()
        self.assertTrue(agent._abort_requested)
        self.assertIn("指定 login.html", agent.failure_reason)

    def test_premature_safe_navigation_is_blocked_then_requests_planning(self):
        agent = make_agent()
        agent.task = "查询智能问数和新SQL生成"
        agent.task_policy = QueryOnlyPolicy.for_task(agent.task)
        before = list(agent.vnc._client.events)

        with patch("builtins.print"):
            continued = agent._execute_action({
                "action": "click",
                "params": {"x": 0.1, "y": 0.2, "target": "体验中心"},
            }, self.image)

        self.assertFalse(continued)
        self.assertFalse(agent._last_action_executed)
        self.assertFalse(agent._abort_requested)
        self.assertEqual(agent.vnc._client.events, before)
        self.assertIn("下一步必须调用 planning", agent._repeat_guidance)

    def test_prelogin_stale_test_type_popup_click_is_blocked_then_requests_escape(self):
        agent = make_agent()
        agent.task = "查询智能问数和新SQL生成"
        agent.task_policy = QueryOnlyPolicy.for_task(agent.task)
        before = list(agent.vnc._client.events)

        with patch("builtins.print"):
            continued = agent._execute_action({
                "action": "click",
                "params": {
                    "x": 0.7,
                    "y": 0.23,
                    "target": "请选择测试类型",
                },
            }, self.image)

        self.assertFalse(continued)
        self.assertFalse(agent._last_action_executed)
        self.assertFalse(agent._abort_requested)
        self.assertEqual(agent.vnc._client.events, before)
        self.assertIn("press(key=escape)", agent._repeat_guidance)
        self.assertIn("禁止再次点击下拉头部", agent._repeat_guidance)

    def test_macos_maximize_is_safe_noop(self):
        vnc = make_vnc(system="mac")
        with patch("vnc_client.time.sleep"):
            vnc.maximize_window("mac")
        self.assertEqual(vnc._client.events, [])

    def test_query_open_url_does_not_repeat_an_already_executed_maximize(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task(
            "查询智能问数和新SQL生成"
        )
        agent._query_maximize_count = 0
        with patch("vnc_client.time.sleep"):
            self.assertTrue(agent._execute_action({
                "action": "maximize_window",
                "params": {},
            }, self.image))
        events_after_first = list(agent.vnc._client.events)
        self.assertEqual(agent._query_maximize_count, 1)

        steps = agent._expand_skill("skill_open_url", {
            "url": AGENT_CAPABILITY_LOGIN_URL,
            "browser": "edge",
        })
        self.assertNotIn("maximize_window", [step["action"] for step in steps])

        with patch("vnc_client.time.sleep"):
            self.assertTrue(agent._execute_action({
                "action": "maximize_window",
                "params": {},
            }, self.image))
        self.assertEqual(agent.vnc._client.events, events_after_first)

    def test_windows_maximize_uses_idempotent_system_menu(self):
        vnc = make_vnc(system="win")
        with patch("vnc_client.time.sleep"):
            vnc.maximize_window("win")
            vnc.maximize_window("win")
        expected = [
            ("down", "alt"), ("press", "space"), ("up", "alt"),
            ("press", "x"),
        ]
        self.assertEqual(vnc._client.events, expected * 2)
        self.assertNotIn(("press", "up"), vnc._client.events)

    def test_windows_maximize_releases_alt_when_system_menu_fails(self):
        class FailingSystemMenuProtocol(FakeProtocol):
            def keyPress(self, key):
                super().keyPress(key)
                if key == "space":
                    raise RuntimeError("system menu failed")

        vnc = make_vnc(system="win")
        vnc._client = FailingSystemMenuProtocol()
        with patch("vnc_client.time.sleep"), self.assertRaises(RuntimeError):
            vnc.maximize_window("win")
        self.assertEqual(
            vnc._client.events,
            [("down", "alt"), ("press", "space"), ("up", "alt")],
        )


class ApplicationLaunchRecoveryTests(unittest.TestCase):
    @staticmethod
    def _start_search_image(*, candidate_panel: bool) -> Image.Image:
        image = Image.new("RGB", (1280, 800), (242, 242, 242))
        pixels = image.load()
        # Start search box. Its larger rectangle must not itself be treated as IME UI.
        for x in range(300, 980):
            pixels[x, 48] = (228, 228, 228)
            pixels[x, 96] = (228, 228, 228)
        for y in range(48, 97):
            pixels[300, y] = (228, 228, 228)
            pixels[979, y] = (228, 228, 228)
        for y in range(49, 96):
            for x in range(301, 979):
                pixels[x, y] = (255, 255, 255)

        if candidate_panel:
            for x in range(285, 868):
                pixels[x, 97] = (228, 228, 228)
                pixels[x, 132] = (228, 228, 228)
            for y in range(97, 133):
                pixels[285, y] = (228, 228, 228)
                pixels[867, y] = (228, 228, 228)
            for y in range(98, 132):
                for x in range(286, 867):
                    pixels[x, y] = (249, 249, 249)
        return image

    def test_local_ime_panel_overrides_false_vision_claim(self):
        candidate = self._start_search_image(candidate_panel=True)
        committed = self._start_search_image(candidate_panel=False)
        self.assertTrue(RPAgent._has_windows_ime_candidate_panel(candidate))
        self.assertFalse(RPAgent._has_windows_ime_candidate_panel(committed))

        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent.pending_input = {
            "text": "edge",
            "field_type": "app_search",
            "verified": False,
            "verification_attempted": False,
            "source_skill": "skill_open_app",
        }
        with patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {
                    "observed_text": "edge",
                    "readable": True,
                    "cause": "unknown",
                    "ime_visible": False,
                },
            }, candidate))
        self.assertFalse(agent.pending_input["verified"])
        self.assertEqual(agent.pending_input["cause"], "ime")
        self.assertEqual(
            agent.pending_input["ime_evidence"], "windows_candidate_panel"
        )

        events_before_blocked_enter = list(agent.vnc._client.events)
        with patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "press", "params": {"key": "enter"},
            }, candidate))
        self.assertFalse(agent._last_action_executed)
        self.assertFalse(agent._abort_requested)
        self.assertEqual(agent.vnc._client.events, events_before_blocked_enter)
        self.assertIn("尚未通过结构化验收", agent._repeat_guidance)

        with patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {
                    "observed_text": "edge",
                    "readable": True,
                    "cause": "unknown",
                    "ime_visible": False,
                },
            }, committed))
        self.assertFalse(agent.pending_input["verified"])
        self.assertEqual(agent.pending_input["cause"], "ime")

        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "retry_text_input",
                "params": {
                    "text": "edge", "field_type": "app_search", "cause": "ime",
                },
            }, committed))
            self.assertNotIn("ime_recovery_required", agent.pending_input)
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {
                    "observed_text": "edge", "readable": True,
                    "cause": "unknown", "ime_visible": False,
                },
            }, committed))
        self.assertTrue(agent.pending_input["verified"])

    def test_exact_edge_best_match_recovery_is_provenance_bound_and_one_shot(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task(
            "在体验中心的智能体能力页面查询智能问数和新SQL生成"
        )
        image = Image.new("RGB", (100, 80), "white")
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "skill_open_app",
                "params": {"application": "edge"},
            }, image))
            self.assertEqual(agent.pending_input["field_type"], "app_search")
            self.assertEqual(agent.pending_input["source_skill"], "skill_open_app")
            self.assertTrue(agent._execute_action({
                "action": "verify_text_input",
                "params": {
                    "observed_text": "edge", "readable": True,
                    "ime_visible": False,
                },
            }, image))
            self.assertTrue(agent.pending_input["verified"])
            self.assertTrue(agent._execute_action({
                "action": "press", "params": {"key": "enter"},
            }, image))
            self.assertEqual(agent._app_launch_recovery["application"], "edge")
            self.assertTrue(agent._execute_action({
                "action": "wait", "params": {"seconds": 1},
            }, image))
            self.assertEqual(agent._app_launch_recovery["wait_count"], 1)

            before = list(agent.vnc._client.events)
            click = {
                "action": "click",
                "params": {
                    "x": 0.345, "y": 0.283,
                    "target": "Microsoft Edge 最佳匹配",
                },
            }
            self.assertTrue(agent._execute_action(click, image))
            self.assertTrue(agent._last_action_executed)
            self.assertEqual(agent._app_launch_recovery["remaining_clicks"], 0)
            self.assertTrue(agent._app_launch_recovery["click_attempted"])
            self.assertEqual(
                agent.vnc._client.events[len(before):],
                [("move", (34, 22)), ("mouse_down", 1), ("mouse_up", 1)],
            )

            after_one_shot = list(agent.vnc._client.events)
            self.assertFalse(agent._execute_action(click, image))
            self.assertEqual(agent.vnc._client.events, after_one_shot)
            self.assertFalse(agent._abort_requested)

    def test_recovery_is_not_armed_without_skill_provenance(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent.pending_input = {
            "text": "edge",
            "field_type": "app_search",
            "verified": True,
            "verification_attempted": True,
        }
        image = Image.new("RGB", (100, 80), "white")
        with patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "press", "params": {"key": "enter"},
            }, image))
        self.assertIsNone(getattr(agent, "_app_launch_recovery", None))
        before = list(agent.vnc._client.events)
        with patch("builtins.print"):
            self.assertFalse(agent._execute_action({
                "action": "click",
                "params": {
                    "x": 0.345, "y": 0.283,
                    "target": "Microsoft Edge 最佳匹配",
                },
            }, image))
        self.assertEqual(agent.vnc._client.events, before)

    def test_unverified_browser_window_blocks_url_typing(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent._app_launch_recovery = {
            "application": "edge", "remaining_clicks": 1, "wait_count": 0,
        }
        image = Image.new("RGB", (100, 80), "white")
        before = list(agent.vnc._client.events)
        failed_check = {
            "passed": False, "state": "start_menu", "evidence": ["开始菜单仍显示"],
            "reason": "浏览器窗口不可见",
        }
        with patch.object(agent, "_verify_visible_state", return_value=failed_check), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "skill_open_url",
                "params": {"url": AGENT_CAPABILITY_LOGIN_URL, "browser": "edge"},
            }, image))
        self.assertFalse(agent._last_action_executed)
        self.assertEqual(agent.vnc._client.events, before)
        self.assertIsNotNone(agent._app_launch_recovery)
        self.assertIn("未知焦点", agent._repeat_guidance)

    def test_launch_recovery_rejects_wrong_browser_plan_and_expiry(self):
        image = Image.new("RGB", (100, 80), "white")
        cases = (
            ("Google Chrome 最佳匹配", [], 0),
            ("Microsoft Edge 最佳匹配", [{"content": "query"}], 0),
            ("Microsoft Edge 最佳匹配", [], 2),
        )
        for target, plans, waits in cases:
            with self.subTest(target=target, plans=bool(plans), waits=waits):
                agent = make_agent()
                agent.task_policy = QueryOnlyPolicy.for_task(
                    "查询智能问数和新SQL生成"
                )
                agent.task_plan = plans
                agent._app_launch_recovery = {
                    "application": "edge", "remaining_clicks": 1,
                    "wait_count": 0,
                }
                with patch("rpa_agent.time.sleep"), patch("builtins.print"):
                    for _ in range(waits):
                        self.assertTrue(agent._execute_action({
                            "action": "wait", "params": {"seconds": 1},
                        }, image))
                    before = list(agent.vnc._client.events)
                    self.assertFalse(agent._execute_action({
                        "action": "click",
                        "params": {"x": 0.3, "y": 0.2, "target": target},
                    }, image))
                self.assertEqual(agent.vnc._client.events, before)

    def test_other_successful_action_expires_recovery_click(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent._app_launch_recovery = {
            "application": "edge", "remaining_clicks": 1, "wait_count": 0,
        }
        image = Image.new("RGB", (100, 80), "white")
        before = list(agent.vnc._client.events)
        with patch("builtins.print"):
            self.assertFalse(agent._execute_action({
                "action": "press", "params": {"key": "escape"},
            }, image))
        self.assertEqual(agent._app_launch_recovery["remaining_clicks"], 0)
        self.assertEqual(agent.vnc._client.events, before)
        with patch("builtins.print"):
            self.assertFalse(agent._execute_action({
                "action": "click",
                "params": {
                    "x": 0.3, "y": 0.2,
                    "target": "Microsoft Edge 最佳匹配",
                },
            }, image))
        self.assertEqual(agent.vnc._client.events, before)

    def test_recovery_freezes_raw_url_input_at_unknown_focus(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent._app_launch_recovery = {
            "application": "edge", "remaining_clicks": 1, "wait_count": 0,
        }
        image = Image.new("RGB", (100, 80), "white")
        with patch("builtins.print"):
            self.assertFalse(agent._execute_action({
                "action": "input_text",
                "params": {
                    "text": AGENT_CAPABILITY_LOGIN_URL,
                    "field_type": "url",
                    "replace": True,
                },
            }, image))
        self.assertEqual(agent.vnc._client.events, [])
        self.assertEqual(agent._app_launch_recovery["remaining_clicks"], 0)
        self.assertIn("启动结果尚未验收", agent._repeat_guidance)

    def test_recovery_click_transport_error_is_terminal_and_consumed(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent._app_launch_recovery = {
            "application": "edge", "remaining_clicks": 1, "wait_count": 0,
        }
        image = Image.new("RGB", (100, 80), "white")
        with patch.object(agent.vnc, "click", side_effect=RuntimeError("partial click")), \
                patch("rpa_agent.time.sleep"), patch("builtins.print"):
            self.assertFalse(agent._execute_action({
                "action": "click",
                "params": {
                    "x": 0.345, "y": 0.283,
                    "target": "Microsoft Edge 最佳匹配",
                },
            }, image))
        self.assertTrue(agent._abort_requested)
        self.assertEqual(agent._app_launch_recovery["remaining_clicks"], 0)
        self.assertIn("partial click", agent.failure_reason)

    def test_edge_recovery_does_not_accept_chrome_window_evidence(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent._app_launch_recovery = {
            "application": "edge", "remaining_clicks": 0, "wait_count": 0,
        }
        image = Image.new("RGB", (100, 80), "white")
        chrome_check = {
            "passed": True,
            "state": "Google Chrome browser",
            "evidence": ["Google Chrome 标签栏与地址栏可见"],
            "reason": "浏览器可见",
        }
        with patch.object(agent, "_verify_visible_state", return_value=chrome_check) as check, \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "skill_open_url",
                "params": {"url": AGENT_CAPABILITY_LOGIN_URL, "browser": "edge"},
            }, image))
        self.assertFalse(agent._last_action_executed)
        self.assertEqual(agent.vnc._client.events, [])
        self.assertFalse(agent._last_state_verification["passed"])
        expected = check.call_args.kwargs["expected_result"]
        self.assertIn("Microsoft Edge", expected)
        self.assertNotIn("或 Google Chrome", expected)

    def test_repeated_browser_window_verification_failure_stops(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent._app_launch_recovery = {
            "application": "edge", "remaining_clicks": 0, "wait_count": 0,
        }
        image = Image.new("RGB", (100, 80), "white")
        failed = {
            "passed": False, "state": "start_menu", "evidence": ["开始菜单"],
            "reason": "Edge 浏览器窗口不可见",
        }
        action = {
            "action": "skill_open_url",
            "params": {"url": AGENT_CAPABILITY_LOGIN_URL, "browser": "edge"},
        }
        with patch.object(agent, "_verify_visible_state", return_value=failed), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action(action, image))
            self.assertFalse(agent._abort_requested)
            self.assertTrue(agent._execute_action(action, image))
        self.assertTrue(agent._abort_requested)
        self.assertEqual(agent.vnc._client.events, [])

    def test_recovery_can_restart_the_same_controlled_app_macro(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent._app_launch_recovery = {
            "application": "edge", "remaining_clicks": 0, "wait_count": 2,
        }
        image = Image.new("RGB", (100, 80), "white")
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "skill_open_app", "params": {"application": "edge"},
            }, image))
        self.assertIsNone(agent._app_launch_recovery)
        self.assertEqual(agent.pending_input["source_skill"], "skill_open_app")
        self.assertIn(("press", "super"), agent.vnc._client.events)

    def test_recovery_rejects_mismatched_open_url_browser_parameter(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent._app_launch_recovery = {
            "application": "edge", "remaining_clicks": 0, "wait_count": 0,
        }
        image = Image.new("RGB", (100, 80), "white")
        with patch.object(agent, "_verify_visible_state") as check, \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "skill_open_url",
                "params": {
                    "url": AGENT_CAPABILITY_LOGIN_URL, "browser": "chrome",
                },
            }, image))
        check.assert_not_called()
        self.assertFalse(agent._last_action_executed)
        self.assertEqual(agent.vnc._client.events, [])
        self.assertEqual(agent._app_launch_recovery["remaining_clicks"], 0)

    def test_inactive_query_does_not_arm_launch_recovery(self):
        agent = make_agent()
        agent.pending_input = {
            "text": "edge", "field_type": "app_search", "verified": True,
            "verification_attempted": True, "source_skill": "skill_open_app",
        }
        image = Image.new("RGB", (100, 80), "white")
        with patch("builtins.print"):
            self.assertTrue(agent._execute_action({
                "action": "press", "params": {"key": "enter"},
            }, image))
        self.assertIsNone(getattr(agent, "_app_launch_recovery", None))

    def test_query_desktop_edge_icon_is_allowed_only_as_verified_bootstrap(self):
        agent = make_agent()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        image = Image.new("RGB", (100, 80), "white")
        action = {
            "action": "click",
            "params": {
                "x": 0.031, "y": 0.177, "target": "Microsoft Edge 桌面图标",
            },
        }
        with patch("builtins.print"):
            self.assertTrue(agent._execute_action(action, image))
        self.assertFalse(agent._abort_requested)
        self.assertEqual(
            agent.vnc._client.events,
            [("move", (3, 13)), ("mouse_down", 1), ("mouse_up", 1)],
        )
        self.assertEqual(agent._app_launch_recovery["application"], "edge")
        self.assertEqual(
            agent._app_launch_recovery["source"], "verified_browser_icon"
        )

        action["action"] = "double_click"
        with patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch("builtins.print"):
            self.assertTrue(agent._execute_action(action, image))
        self.assertFalse(agent._abort_requested)
        self.assertEqual(agent._app_launch_recovery["application"], "edge")
        self.assertEqual(agent._app_launch_recovery["remaining_clicks"], 0)
        self.assertEqual(
            agent._app_launch_recovery["source"], "verified_browser_icon"
        )

        agent.task_plan = [{"content": "进入智能体能力", "completed": False}]
        events_before_denied = list(agent.vnc._client.events)
        with patch("builtins.print"):
            self.assertFalse(agent._execute_action(action, image))
        self.assertFalse(agent._abort_requested)
        self.assertIn("浏览器启动结果尚未验收", agent._repeat_guidance)
        self.assertEqual(agent.vnc._client.events, events_before_denied)


if __name__ == "__main__":
    unittest.main()
