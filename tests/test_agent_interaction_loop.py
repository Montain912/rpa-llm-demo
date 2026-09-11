import json
import sys
import types
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw


# Keep this integration test independent of optional runtime packages.  The
# real agent/VNC implementations are still imported; only their import-time
# third-party boundary is replaced when it is unavailable.
if "vncdotool" not in sys.modules:
    vncdotool_stub = types.ModuleType("vncdotool")
    vncdotool_stub.api = types.SimpleNamespace(connect=lambda *_args, **_kwargs: None)
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

import rpa_agent
from interaction_guard import InteractionGuard
from rpa_agent import RPAgent
from task_policy import QueryOnlyPolicy


def click(x=0.5, y=0.5):
    return {
        "action": "click",
        "params": {"x": x, "y": y},
        "thought": "click target",
    }


def changed_target_frame(base=None, *, x=0.5, y=0.5):
    image = (base or Image.new("RGB", (100, 100), "white")).copy()
    px = int(x * image.width)
    py = int(y * image.height)
    ImageDraw.Draw(image).rectangle((px - 7, py - 7, px + 7, py + 7), fill="black")
    return image


class FakeVNC:
    def __init__(self, frames):
        self.frames = [frame.copy() for frame in frames]
        self.events = []

    def screenshot(self):
        if not self.frames:
            raise AssertionError("unexpected screenshot request")
        return self.frames.pop(0).copy()

    def click(self, x, y, button=1):
        self.events.append(("click", x, y, button))


def make_agent(frames):
    agent = RPAgent.__new__(RPAgent)
    agent.vnc = FakeVNC(frames)
    agent.system = "windows"
    agent.system_prompt = "test system prompt"
    agent.step = 0
    agent.rand = 7
    agent.history = []
    agent.planning = ""
    agent.task = ""
    agent.task_plan = []
    agent.current_plan_idx = -1
    agent._subtask_result = None
    agent._prev_screenshot = None
    agent._repeat_guidance = ""
    agent.pending_input = None
    agent.input_retry_count = 0
    agent.ime_operation_counts = {}
    agent.failure_reason = ""
    agent._last_action_executed = False
    agent._abort_requested = False
    agent.login_progress = "idle"
    agent._active_login_input_stage = None
    agent._active_skill_action = None
    agent._last_focus_coordinates = None
    agent._last_skill_expand_error = ""
    agent.task_policy = QueryOnlyPolicy.for_task("")
    agent.interaction_guard = InteractionGuard(coordinate_grid=50)
    agent._executed_observations = []
    agent._loop_block_counts = {}
    agent._scroll_counts = {}
    agent._stale_block_count = 0
    agent._last_state_verification = None
    agent._planning_verification_failures = 0
    return agent


def virtual_screenshot_path(step, _rand, _image, phase="before"):
    return f"/virtual/step-{step}-{phase}.png"


class AgentInteractionLoopTests(unittest.TestCase):
    def run_step(self, agent, action, *, plan=None):
        records = []
        with (
            patch.object(rpa_agent, "chat_vision", return_value=json.dumps(action)),
            patch.object(rpa_agent.time, "sleep", return_value=None),
            patch.object(
                agent,
                "saveScreenShot",
                side_effect=virtual_screenshot_path,
            ),
        ):
            status = agent._decision_step(
                "test task",
                "",
                plan,
                progress_callback=records.append,
            )
        self.assertEqual(len(records), 1)
        return status, records[0]

    def test_freshness_target_change_blocks_all_pointer_events(self):
        observed = Image.new("RGB", (100, 100), "white")
        stale = changed_target_frame(observed)
        agent = make_agent([observed, stale])

        status, record = self.run_step(agent, click())

        self.assertEqual(status, "continue")
        self.assertEqual(agent.vnc.events, [])
        self.assertFalse(record["executed"])
        self.assertTrue(record["freshness"]["target_changed"])
        self.assertIsNone(record["screenshots"]["after"])
        self.assertIn("坐标已过期", agent._repeat_guidance)

    def test_repeated_same_click_is_blocked_even_after_visual_change(self):
        prior = Image.new("RGB", (100, 100), "white")
        current = changed_target_frame(prior)
        agent = make_agent([current, current])
        agent.history = [{**click(), "executed": True}]
        agent._executed_observations = [prior]

        status, record = self.run_step(agent, click())

        self.assertEqual(status, "continue")
        self.assertEqual(agent.vnc.events, [])
        self.assertFalse(record["executed"])
        self.assertEqual(record["loop_guard"]["period"], 1)
        self.assertTrue(record["loop_guard"]["visual_changed"])

    def test_abab_cycle_without_visual_progress_is_blocked(self):
        frame = Image.new("RGB", (100, 100), "white")
        action_a = click(0.4, 0.5)
        action_b = click(0.6, 0.5)
        agent = make_agent([frame, frame])
        agent.history = [
            {**action_a, "executed": True},
            {**action_b, "executed": True},
            {**action_a, "executed": True},
        ]
        agent._executed_observations = [frame.copy(), frame.copy(), frame.copy()]

        status, record = self.run_step(agent, action_b)

        self.assertEqual(status, "continue")
        self.assertEqual(agent.vnc.events, [])
        self.assertFalse(record["executed"])
        self.assertEqual(record["loop_guard"]["period"], 2)
        self.assertFalse(record["loop_guard"]["visual_changed"])

    def test_executed_click_logs_before_pre_action_after_and_verification(self):
        before = Image.new("RGB", (100, 100), "white")
        after = changed_target_frame(before)
        agent = make_agent([before, before, after])

        status, record = self.run_step(agent, click())

        self.assertEqual(status, "continue")
        self.assertEqual(agent.vnc.events, [("click", 49, 49, 1)])
        self.assertTrue(record["executed"])
        self.assertEqual(
            record["screenshots"],
            {
                "before": "/virtual/step-1-before.png",
                "pre_action": "/virtual/step-1-pre_action.png",
                "after": "/virtual/step-1-after.png",
            },
        )
        verification = record["verification"]
        self.assertEqual(verification["status"], "visual_change")
        self.assertTrue(verification["changed"])
        self.assertTrue(verification["target_changed"])
        self.assertIsNotNone(verification["target_region"])
        self.assertIn("full_score", verification)
        self.assertIn("max_tile_score", verification)

    def test_subtask_done_false_returns_failed(self):
        frame = Image.new("RGB", (100, 100), "white")
        plan = {
            "content": "inspect one row",
            "expected_result": "visible evidence captured",
            "completed": False,
        }
        agent = make_agent([frame])
        agent.task_plan = [plan]
        agent.current_plan_idx = 0
        action = {
            "action": "subtask_done",
            "params": {"result": "evidence is ambiguous", "passed": False},
        }

        status, record = self.run_step(agent, action, plan=plan)

        self.assertEqual(status, "failed")
        self.assertTrue(record["executed"])
        self.assertFalse(agent._subtask_result["passed"])
        self.assertTrue(agent._abort_requested)
        self.assertIn("验收未通过", agent.failure_reason)

    def test_done_is_not_accepted_until_every_subtask_passes(self):
        frame = Image.new("RGB", (100, 100), "white")
        plan = {
            "content": "inspect one row",
            "expected_result": "visible evidence captured",
            "completed": False,
        }
        agent = make_agent([frame])
        agent.task_plan = [plan]
        agent.current_plan_idx = 0

        status, record = self.run_step(
            agent,
            {"action": "done", "params": {}, "thought": "finished"},
            plan=plan,
        )

        self.assertEqual(status, "continue")
        self.assertFalse(record["executed"])
        self.assertIn("已阻止提前完成", agent._repeat_guidance)

    def test_pending_exact_input_uses_lossless_deterministic_vision(self):
        frame = Image.new("RGB", (100, 100), "white")
        agent = make_agent([frame])
        agent.pending_input = {
            "text": "zhouhao",
            "field_type": "username",
            "verified": False,
            "verification_attempted": False,
        }
        action = {
            "action": "verify_text_input",
            "params": {
                "observed_text": "zhouhao",
                "readable": True,
                "cause": "unknown",
                "ime_visible": False,
            },
        }
        records = []
        with (
            patch.object(
                rpa_agent,
                "chat_vision",
                return_value=json.dumps(action),
            ) as vision,
            patch.object(agent, "saveScreenShot", side_effect=virtual_screenshot_path),
        ):
            status = agent._decision_step(
                "test task",
                "",
                None,
                progress_callback=records.append,
            )

        self.assertEqual(status, "continue")
        self.assertTrue(agent.pending_input["verified"])
        self.assertEqual(vision.call_args.kwargs["image_format"], "PNG")
        self.assertEqual(vision.call_args.kwargs["temperature"], 0.0)
        decision_prompt = vision.call_args.args[0]
        self.assertIn("只读取登录卡片中带人形/用户图标", decision_prompt)
        self.assertIn("请输入登录账号", decision_prompt)
        self.assertIn("不得把下方密码行的圆点", decision_prompt)

    def test_query_pointer_target_verifier_blocks_before_mouse_event(self):
        frame = Image.new("RGB", (100, 100), "white")
        agent = make_agent([frame, frame])
        agent.task = "查询智能问数和新SQL生成"
        agent.task_policy = QueryOnlyPolicy.for_task(agent.task)
        agent.task_plan = agent.task_policy.build_plan(agent.task)
        agent.current_plan_idx = 0
        plan = agent.task_plan[0]
        action = {
            "action": "click",
            "params": {"x": 0.5, "y": 0.5, "target": "体验中心"},
            "thought": "进入体验中心",
        }
        verifier_result = {
            "passed": False,
            "target_visible": True,
            "marker_inside_target": False,
            "suggested_x": 0.42,
            "suggested_y": 0.27,
            "evidence": ["红色十字位于相邻菜单"],
            "reason": "落点不在体验中心菜单内部",
        }
        records = []
        with (
            patch.object(
                rpa_agent,
                "chat_vision",
                side_effect=[json.dumps(action), json.dumps(verifier_result)],
            ),
            patch.object(rpa_agent.time, "sleep", return_value=None),
            patch.object(agent, "saveScreenShot", side_effect=virtual_screenshot_path),
        ):
            status = agent._decision_step(
                agent.task,
                "",
                plan,
                progress_callback=records.append,
            )

        self.assertEqual(status, "continue")
        self.assertEqual(agent.vnc.events, [])
        self.assertFalse(records[0]["executed"])
        self.assertFalse(records[0]["pointer_target_verification"]["passed"])
        self.assertIn("未发送任何鼠标事件", agent._repeat_guidance)
        self.assertIn("x=0.4200, y=0.2700", agent._repeat_guidance)
        self.assertIn("红色十字二次验收", agent._repeat_guidance)

    def test_verified_relocated_login_field_can_refocus(self):
        frame = Image.new("RGB", (100, 80), "white")
        agent = make_agent([frame, frame, frame])
        agent.task = "查询智能问数和新SQL生成"
        agent.task_policy = QueryOnlyPolicy.for_task(agent.task)
        agent.pending_input = {
            "text": "zhouhao",
            "field_type": "username",
            "verified": False,
            "verification_attempted": True,
            "cause": "focus",
            "focus_coordinates": (0.75, 0.26),
            "login_stage": "username",
        }
        action = {
            "action": "click",
            "params": {"x": 0.75, "y": 0.215, "target": "用户名输入框"},
            "thought": "重新聚焦用户名输入框",
        }
        verifier_result = {
            "passed": True,
            "target_visible": True,
            "marker_inside_target": True,
            "evidence": ["红色十字位于用户名输入框内部"],
            "reason": "落点与目标一致",
        }
        records = []
        with (
            patch.object(
                rpa_agent,
                "chat_vision",
                side_effect=[json.dumps(action), json.dumps(verifier_result)],
            ),
            patch.object(rpa_agent.time, "sleep", return_value=None),
            patch.object(agent, "saveScreenShot", side_effect=virtual_screenshot_path),
        ):
            status = agent._decision_step(
                agent.task, "", None, progress_callback=records.append
            )

        self.assertEqual(status, "continue")
        self.assertTrue(records[0]["executed"])
        self.assertEqual(agent.vnc.events, [("click", 74, 16, 1)])
        self.assertTrue(agent.pending_input["refocused"])
        self.assertEqual(agent.pending_input["focus_coordinates"], (0.75, 0.215))

    def test_query_scroll_verifier_gets_test_type_popup_anchors(self):
        frame = Image.new("RGB", (100, 80), "white")
        agent = make_agent([])
        agent.task = "查询智能问数和新SQL生成"
        agent.task_policy = QueryOnlyPolicy.for_task(agent.task)
        agent.task_plan = agent.task_policy.build_plan(agent.task)
        agent.current_plan_idx = 1
        action = {
            "action": "scroll",
            "params": {
                "x": 0.7,
                "y": 0.35,
                "amount": -3,
                "target": "测试类型下拉弹层",
            },
        }
        verifier_result = {
            "passed": True,
            "target_visible": True,
            "marker_inside_target": True,
            "evidence": ["红色十字在测试类型弹层内部"],
            "reason": "可以安全滚动",
        }
        captured = {}

        def fake_vision(prompt, *_args, **_kwargs):
            captured["prompt"] = prompt
            return json.dumps(verifier_result)

        with patch.object(rpa_agent, "chat_vision", side_effect=fake_vision):
            result = agent._verify_query_pointer_target(frame, action)

        self.assertTrue(result["passed"])
        self.assertIn("文本结构化增强", captured["prompt"])
        self.assertIn("这些是测试类型而不是渠道", captured["prompt"])
        self.assertIn("不得因目标项暂不可见", captured["prompt"])

    def test_logout_pointer_verifier_knows_the_direct_header_icon(self):
        frame = Image.new("RGB", (1280, 800), "white")
        agent = make_agent([])
        action = {
            "action": "click",
            "params": {"x": 0.977, "y": 0.131, "target": "退出登录图标"},
        }
        full_result = {
            "passed": False,
            "target_visible": False,
            "marker_inside_target": False,
            "target_bbox": None,
            "evidence": ["全图未读取到文字标签"],
            "reason": "没有退出登录文字",
        }
        local_result = {
            "passed": True,
            "target_visible": True,
            "marker_inside_target": True,
            "target_bbox": [0.80, 0.40, 0.98, 0.65],
            "suggested_x": None,
            "suggested_y": None,
            "evidence": ["红点位于周昊右侧白色门框/向右箭头按钮内"],
            "reason": "无文字的直接退出按钮命中",
        }
        calls = []

        def fake_vision(prompt, image, **_kwargs):
            calls.append((prompt, image.size))
            return json.dumps(full_result if len(calls) == 1 else local_result)

        with patch.object(rpa_agent, "chat_vision", side_effect=fake_vision):
            result = agent._verify_query_pointer_target(frame, action)

        self.assertTrue(result["passed"])
        self.assertTrue(result["local_zoom_confirmed"])
        self.assertIn("不需要先展开用户菜单", calls[0][0])
        self.assertIn("无文字的直接退出登录按钮", calls[1][0])

    def test_direct_logout_icon_is_verified_locally_without_vision_export(self):
        frame = Image.new("RGB", (1280, 800), "white")
        draw = ImageDraw.Draw(frame)
        draw.rectangle((4, 80, 1275, 127), fill=(90, 197, 66))
        icon = (172, 226, 161)
        draw.rectangle((1243, 96, 1253, 110), outline=icon, width=2)
        draw.line((1249, 103, 1258, 103), fill=icon, width=2)
        draw.line((1254, 99, 1258, 103, 1254, 107), fill=icon, width=2)
        agent = make_agent([])

        with patch.object(
            rpa_agent,
            "chat_vision",
            side_effect=AssertionError("direct logout detection must stay local"),
        ):
            inside = agent._verify_query_pointer_target(frame, {
                "action": "click",
                "params": {"x": 0.977, "y": 0.131, "target": "退出登录图标"},
            })
            outside = agent._verify_query_pointer_target(frame, {
                "action": "click",
                "params": {"x": 0.90, "y": 0.131, "target": "退出登录图标"},
            })

        self.assertTrue(inside["passed"])
        self.assertIn("本地确定性", inside["reason"])
        self.assertFalse(outside["passed"])
        self.assertIsNotNone(outside["suggested_x"])
        self.assertGreater(outside["suggested_x"], 0.95)

    def test_pointer_verifier_recomputes_inside_from_normalized_bbox(self):
        frame = Image.new("RGB", (1280, 800), "white")
        agent = make_agent([])
        action = {
            "action": "click",
            "params": {"x": 0.688, "y": 0.229, "target": "请选择测试类型"},
        }
        verifier_result = {
            "passed": False,
            "target_visible": True,
            "marker_inside_target": False,
            "target_bbox": [0.64, 0.21, 0.78, 0.26],
            "suggested_x": 0.688,
            "suggested_y": 0.229,
            "evidence": ["请选择测试类型下拉框清楚可见"],
            "reason": "误认为十字靠近箭头所以未命中",
        }
        captured = {}

        def fake_vision(prompt, *_args, **kwargs):
            captured["system_prompt"] = kwargs["system_prompt"]
            return json.dumps(verifier_result)

        with patch.object(rpa_agent, "chat_vision", side_effect=fake_vision):
            result = agent._verify_query_pointer_target(frame, action)

        self.assertTrue(result["passed"])
        self.assertTrue(result["marker_inside_target"])
        self.assertEqual(result["target_bbox"], [0.64, 0.21, 0.78, 0.26])
        self.assertIsNone(result["suggested_x"])
        self.assertIn("代码确定性判定命中", result["reason"])
        self.assertIn("target_bbox", captured["system_prompt"])
        self.assertIn("包括右侧下拉箭头区域", captured["system_prompt"])

    def test_pointer_verifier_retries_small_sidebar_target_with_zoomed_crop(self):
        frame = Image.new("RGB", (1280, 800), "white")
        agent = make_agent([])
        action = {
            "action": "click",
            "params": {"x": 0.045, "y": 0.222, "target": "体验中心"},
        }
        full_result = {
            "passed": False,
            "target_visible": False,
            "marker_inside_target": False,
            "target_bbox": None,
            "evidence": ["全图小字无法确认"],
            "reason": "目标不可见",
        }
        local_result = {
            "passed": True,
            "target_visible": True,
            "marker_inside_target": True,
            "target_bbox": [0.05, 0.42, 0.75, 0.58],
            "suggested_x": None,
            "suggested_y": None,
            "evidence": ["放大图中可读到渠道管理、体验中心、系统配置三行"],
            "reason": "十字位于中间的体验中心菜单行",
        }
        calls = []

        def fake_vision(prompt, image, **_kwargs):
            calls.append((prompt, image.size))
            return json.dumps(full_result if len(calls) == 1 else local_result)

        with patch.object(rpa_agent, "chat_vision", side_effect=fake_vision):
            result = agent._verify_query_pointer_target(frame, action)

        self.assertTrue(result["passed"])
        self.assertEqual(len(calls), 2)
        self.assertIn("渠道管理 → 体验中心 → 系统配置", calls[0][0])
        self.assertIn("局部放大图", calls[1][0])
        self.assertGreater(calls[1][1][0], 500)
        self.assertIsNotNone(result["target_bbox"])
        self.assertTrue(result["local_zoom_confirmed"])
        self.assertIn("局部放大复核", result["reason"])

    def test_option_local_zoom_semantic_hit_cannot_override_conflicting_geometry(self):
        frame = Image.new("RGB", (1280, 800), "white")
        agent = make_agent([])
        action = {
            "action": "click",
            "params": {"x": 0.6388, "y": 0.4969, "target": "智能问数选项"},
        }
        full_result = {
            "passed": False,
            "target_visible": True,
            "marker_inside_target": False,
            "target_bbox": [0.61, 0.43, 0.78, 0.47],
            "suggested_x": 0.66,
            "suggested_y": 0.52,
            "evidence": ["全图误把红点识别成相邻的智能扩写行"],
            "reason": "全图小字判定未命中",
        }
        local_result = {
            "passed": True,
            "target_visible": True,
            "marker_inside_target": True,
            # Reproduce a model returning coordinates from the wrong frame.
            "target_bbox": [0.60, 0.47, 0.78, 0.52],
            "suggested_x": 0.65,
            "suggested_y": 0.50,
            "evidence": ["放大图中红色十字明确位于智能问数文字所在行"],
            "reason": "局部文字清晰，命中智能问数而非相邻选项",
        }

        with patch.object(
            rpa_agent,
            "chat_vision",
            side_effect=[json.dumps(full_result), json.dumps(local_result)],
        ):
            result = agent._verify_query_pointer_target(frame, action)

        self.assertFalse(result["passed"])
        self.assertFalse(result["marker_inside_target"])
        self.assertFalse(result["local_zoom_confirmed"])
        self.assertIsNotNone(result["target_bbox"])
        self.assertIsNotNone(result["suggested_x"])
        self.assertIsNotNone(result["suggested_y"])
        self.assertIn("局部放大复核", result["reason"])

    def test_local_verifier_missing_geometry_cannot_claim_success(self):
        agent = make_agent([])
        result = {"passed": True, "target_visible": True,
                  "marker_inside_target": True, "target_bbox": None,
                  "evidence": ["声称红点命中"], "suggested_x": None, "suggested_y": None}
        with patch.object(rpa_agent, "chat_vision", return_value=json.dumps(result)):
            checked = agent._verify_query_pointer_target(Image.new("RGB", (1280, 800)), {
                "action": "click", "params": {"x": .65, "y": .46, "target": "智能问数"}})
        self.assertFalse(checked["passed"])
        self.assertFalse(checked["local_zoom_confirmed"])

    def test_blind_local_pixel_box_is_mapped_without_proposed_marker(self):
        agent = make_agent([])
        full = {"passed": False, "target_visible": False, "evidence": []}
        calls = []
        def locate(prompt, image, **kwargs):
            calls.append((prompt, image, kwargs))
            if len(calls) == 1:
                return json.dumps(full)
            width, height = image.size
            # Screenshot is entirely white: the local locator must see no marker.
            self.assertEqual(image.getextrema(), ((255, 255),) * 3)
            self.assertNotIn("红色十字中心", prompt)
            return json.dumps({"target_visible": True,
                "bbox_pixels": [width*.4, height*.4, width*.6, height*.6],
                "evidence": ["独立定位目标文字行"], "reason": "文字清晰"})
        with patch.object(rpa_agent, "chat_vision", side_effect=locate):
            checked = agent._verify_query_pointer_target(Image.new("RGB", (1280, 800), "white"), {
                "action": "click", "params": {"x": .65, "y": .46, "target": "智能问数"}})
        self.assertTrue(checked["passed"])
        self.assertTrue(checked["geometry_inside_target"])
        self.assertTrue(checked["local_zoom_confirmed"])

    def test_blind_local_pixel_box_outside_image_is_rejected(self):
        agent = make_agent([])
        response = {"target_visible": True, "bbox_pixels": [0, 0, 9999, 9999],
                    "evidence": ["无效尺寸"], "passed": True, "marker_inside_target": True}
        with patch.object(rpa_agent, "chat_vision", return_value=json.dumps(response)):
            checked = agent._verify_query_pointer_target(Image.new("RGB", (1280, 800)), {
                "action": "click", "params": {"x": .65, "y": .46, "target": "智能问数"}})
        self.assertFalse(checked["passed"])

    def test_pointer_verifier_uses_bbox_center_when_marker_is_outside(self):
        frame = Image.new("RGB", (100, 80), "white")
        agent = make_agent([])
        action = {
            "action": "click",
            "params": {"x": 0.5, "y": 0.5, "target": "请选择测试类型"},
        }
        verifier_result = {
            "passed": True,
            "target_visible": True,
            "marker_inside_target": True,
            "target_bbox": [0.1, 0.1, 0.3, 0.3],
            "suggested_x": 0.9,
            "suggested_y": 0.9,
            "evidence": ["目标下拉框清楚可见"],
            "reason": "错误声称已命中",
        }

        with patch.object(
            rpa_agent,
            "chat_vision",
            return_value=json.dumps(verifier_result),
        ):
            result = agent._verify_query_pointer_target(frame, action)

        self.assertFalse(result["passed"])
        self.assertFalse(result["marker_inside_target"])
        self.assertAlmostEqual(result["suggested_x"], 0.2)
        self.assertAlmostEqual(result["suggested_y"], 0.2)
        self.assertIn("代码确定性判定未命中", result["reason"])

    def test_option_bbox_cannot_override_semantic_adjacent_row_rejection(self):
        frame = Image.new("RGB", (1280, 800), "white")
        agent = make_agent([])
        action = {
            "action": "click",
            "params": {"x": 0.656, "y": 0.489, "target": "智能问数选项"},
        }
        verifier_result = {
            "passed": False,
            "target_visible": True,
            "marker_inside_target": False,
            "target_bbox": [0.617, 0.465, 0.775, 0.512],
            "suggested_x": 0.69,
            "suggested_y": 0.53,
            "evidence": ["十字实际落在相邻的新SQL生成选项行"],
            "reason": "智能问数在十字下方，当前命中相邻行",
        }
        zoom_result = {
            "passed": False,
            "target_visible": True,
            "marker_inside_target": False,
            "target_bbox": [0.05, 0.58, 0.95, 0.75],
            "suggested_x": 0.5,
            "suggested_y": 0.665,
            "evidence": ["放大图确认十字在新SQL生成行，智能问数位于下方"],
            "reason": "相邻行，不通过",
        }

        with patch.object(
            rpa_agent,
            "chat_vision",
            side_effect=[json.dumps(verifier_result), json.dumps(zoom_result)],
        ):
            result = agent._verify_query_pointer_target(frame, action)

        self.assertFalse(result["passed"])
        self.assertFalse(result["geometry_inside_target"])
        self.assertFalse(result["marker_inside_target"])
        self.assertAlmostEqual(result["suggested_x"], 0.656, places=2)
        self.assertGreater(result["suggested_y"], 0.52)
        self.assertIn("局部放大复核", result["reason"])

    def test_pointer_verifier_rejects_non_tight_bbox_and_keeps_evidence_gate(self):
        frame = Image.new("RGB", (100, 80), "white")
        agent = make_agent([])
        action = {
            "action": "click",
            "params": {"x": 0.5, "y": 0.5, "target": "请选择测试类型"},
        }
        verifier_result = {
            "passed": True,
            "target_visible": True,
            "marker_inside_target": True,
            "target_bbox": [0.0, 0.0, 1.0, 1.0],
            "evidence": [],
            "reason": "没有具体可见证据",
        }

        with patch.object(
            rpa_agent,
            "chat_vision",
            return_value=json.dumps(verifier_result),
        ):
            result = agent._verify_query_pointer_target(frame, action)

        self.assertFalse(result["passed"])
        self.assertIsNone(result["target_bbox"])
        self.assertTrue(result["marker_inside_target"])


if __name__ == "__main__":
    unittest.main()
