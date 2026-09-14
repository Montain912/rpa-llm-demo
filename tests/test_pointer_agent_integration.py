import sys
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image


# These integration tests exercise the Agent -> VNC boundary without requiring
# a real vncdotool installation, remote desktop, or model endpoint.
if "vncdotool" not in sys.modules:
    vncdotool_stub = types.ModuleType("vncdotool")
    vncdotool_stub.api = types.SimpleNamespace(
        connect=lambda *_args, **_kwargs: None
    )
    sys.modules["vncdotool"] = vncdotool_stub

if "llm_client" not in sys.modules:
    llm_stub = types.ModuleType("llm_client")
    llm_stub.chat_vision = lambda *_args, **_kwargs: ""
    llm_stub.chat_text = lambda *_args, **_kwargs: ""
    llm_stub.token_tracker = types.SimpleNamespace(
        reset=lambda: None,
        save=lambda *_args, **_kwargs: None,
    )
    sys.modules["llm_client"] = llm_stub

if "knowledge_loader" not in sys.modules:
    knowledge_stub = types.ModuleType("knowledge_loader")
    knowledge_stub.get_summary = lambda *_args, **_kwargs: ""
    sys.modules["knowledge_loader"] = knowledge_stub

from rpa_agent import RPAgent
from task_policy import QueryOnlyPolicy
from vnc_client import VNCClient


class FakeProtocol:
    def __init__(self):
        self.events = []

    def mouseMove(self, x, y):
        self.events.append(("move", x, y))

    def mouseDown(self, button):
        self.events.append(("down", button))

    def mouseUp(self, button):
        self.events.append(("up", button))

    def keyDown(self, key):
        self.events.append(("key_down", key))

    def keyUp(self, key):
        self.events.append(("key_up", key))

    def keyPress(self, key):
        self.events.append(("key_press", key))


def make_vnc():
    vnc = VNCClient(system="win", key_interval=0)
    vnc._client = FakeProtocol()
    return vnc


def make_agent(*, task="", vnc=None):
    agent = RPAgent.__new__(RPAgent)
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
    agent.task = task
    agent.knowledge_summary = ""
    agent.task_policy = QueryOnlyPolicy.for_task(task)
    return agent


class PointerAgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        sleep_patcher = patch("vnc_client.time.sleep", return_value=None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    @staticmethod
    def screenshot():
        # (w - 1, h - 1) is exactly (100, 80), which makes expected pixel
        # coordinates readable while still checking the inclusive-edge mapping.
        return Image.new("RGB", (101, 81), "white")

    def test_move_converts_normalized_coordinates_and_forwards_motion_options(self):
        vnc = make_vnc()
        original_move = vnc.move_mouse
        vnc.move_mouse = Mock(wraps=original_move)
        agent = make_agent(vnc=vnc)

        continued = agent._execute_action(
            {
                "action": "move",
                "params": {
                    "x": 0.5,
                    "y": 0.25,
                    "duration_ms": 120,
                    "steps": 3,
                    "target": "toolbar",
                },
            },
            self.screenshot(),
        )

        self.assertTrue(continued)
        self.assertTrue(agent._last_action_executed)
        vnc.move_mouse.assert_called_once_with(
            50, 20, duration_ms=120, steps=3
        )
        self.assertEqual(vnc._client.events, [("move", 50, 20)])

    def test_query_scroll_stays_at_target_and_emits_each_requested_tick(self):
        vnc = make_vnc()
        agent = make_agent(task="查询智能体能力", vnc=vnc)
        agent.task_plan = agent.task_policy.build_plan(agent.task)
        agent.task_plan[0]["completed"] = True
        agent.task_plan[0]["passed"] = True
        agent.current_plan_idx = 1

        continued = agent._execute_action(
            {
                "action": "scroll",
                "params": {
                    "x": 0.2,
                    "y": 0.75,
                    "amount": -4,
                    "target": "测试类型下拉列表",
                },
            },
            self.screenshot(),
        )

        self.assertTrue(continued)
        self.assertEqual(
            vnc._client.events,
            [("move", 20, 60)]
            + [("down", 5), ("up", 5)] * 4,
        )

    def test_drag_converts_both_points_and_forwards_all_parameters(self):
        vnc = make_vnc()
        vnc.drag = Mock()
        agent = make_agent(vnc=vnc)

        continued = agent._execute_action(
            {
                "action": "drag",
                "params": {
                    "start_x": 0.1,
                    "start_y": 0.2,
                    "end_x": 0.9,
                    "end_y": 0.8,
                    "duration_ms": 750,
                    "steps": 9,
                    "button": "right",
                    "target": "desktop file",
                },
            },
            self.screenshot(),
        )

        self.assertTrue(continued)
        vnc.drag.assert_called_once_with(
            10,
            16,
            90,
            64,
            duration_ms=750,
            steps=9,
            button="right",
        )

    def test_query_policy_blocks_drag_before_any_vnc_event(self):
        vnc = make_vnc()
        vnc.drag = Mock()
        agent = make_agent(task="测试智能体能力查询", vnc=vnc)

        continued = agent._execute_action(
            {
                "action": "drag",
                "params": {
                    "start_x": 0.1,
                    "start_y": 0.2,
                    "end_x": 0.9,
                    "end_y": 0.8,
                    "target": "测试类型下拉列表",
                },
            },
            self.screenshot(),
        )

        self.assertFalse(continued)
        self.assertTrue(agent._abort_requested)
        self.assertFalse(agent._last_action_executed)
        vnc.drag.assert_not_called()
        self.assertEqual(vnc._client.events, [])

    def test_query_policy_blocks_targetless_scroll_before_any_vnc_event(self):
        vnc = make_vnc()
        agent = make_agent(task="测试智能体能力查询", vnc=vnc)

        continued = agent._execute_action(
            {
                "action": "scroll",
                "params": {"x": 0.2, "y": 0.75, "amount": -3},
            },
            self.screenshot(),
        )

        self.assertFalse(continued)
        self.assertTrue(agent._abort_requested)
        self.assertIn("params.target", agent.failure_reason)
        self.assertEqual(vnc._client.events, [])

    def test_click_and_double_click_never_implicitly_maximize(self):
        vnc = make_vnc()
        original_double_click = vnc.double_click
        vnc.double_click = Mock(wraps=original_double_click)
        vnc.maximize_window = Mock()
        agent = make_agent(vnc=vnc)
        screen = self.screenshot()

        click_continued = agent._execute_action(
            {
                "action": "click",
                "params": {"x": 0.5, "y": 1.0, "target": "taskbar item"},
            },
            screen,
        )
        double_continued = agent._execute_action(
            {
                "action": "double_click",
                "params": {"x": 0.4, "y": 0.4, "target": "desktop icon"},
            },
            screen,
        )

        self.assertTrue(click_continued)
        self.assertTrue(double_continued)
        vnc.double_click.assert_called_once_with(40, 32)
        vnc.maximize_window.assert_not_called()

    def test_maximize_is_an_explicit_forwarded_action(self):
        vnc = make_vnc()
        vnc.maximize_window = Mock()
        agent = make_agent(vnc=vnc)

        continued = agent._execute_action(
            {"action": "maximize_window", "params": {}},
            self.screenshot(),
        )

        self.assertTrue(continued)
        vnc.maximize_window.assert_called_once_with("win")
        self.assertEqual(vnc._client.events, [])


if __name__ == "__main__":
    unittest.main()
