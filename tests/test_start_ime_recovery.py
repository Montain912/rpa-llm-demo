"""Replay the Start-menu IME deadlock without a live desktop or LLM."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_agent_interaction_loop import make_agent, virtual_screenshot_path
import test_input_method as input_helpers
import rpa_agent
from task_policy import QueryOnlyPolicy


class StartIMERecoveryTests(unittest.TestCase):
    def test_direct_stage_probe_cannot_complete_after_independent_failure(self):
        frame = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=False)
        agent = make_agent([frame])
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent.task_plan = agent.task_policy.build_plan("查询智能问数和新SQL生成")
        agent.current_plan_idx = 0
        plan = agent.task_plan[0]
        agent._direct_stage_checks = True
        agent.history = [{"action": "click", "executed": True, "params": {"target": "智能体能力", "x": .1, "y": .2},
                          "subtask": {"content": plan["content"]}}]
        with patch("rpa_agent.chat_vision") as decision, \
             patch.object(agent, "_verify_visible_state", return_value={"passed": False, "reason": "only parent menu opened"}) as gate, \
             patch.object(agent, "saveScreenShot", side_effect=virtual_screenshot_path), patch("rpa_agent.time.sleep"):
            agent._decision_step("查询智能问数和新SQL生成", "", plan)
        decision.assert_not_called()
        gate.assert_called_once()
        self.assertFalse(agent.history[-1]["executed"])
        self.assertIsNone(agent._subtask_result)
        self.assertFalse(plan["completed"])
        self.assertEqual(agent.vnc.events, [])

    def test_direct_planning_does_not_skip_login_entry_evidence(self):
        frame = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=False)
        agent = make_agent([frame])
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent._direct_stage_checks = True
        agent.login_progress = "submitted"
        agent.history = [{"action": "skill_open_webpage", "executed": True, "params": {"stage": "submit"}}]
        with patch("rpa_agent.chat_vision") as decision, \
             patch.object(agent, "saveScreenShot", side_effect=virtual_screenshot_path), patch("rpa_agent.time.sleep"):
            result = agent._decision_step("查询智能问数和新SQL生成", "", None)
        decision.assert_not_called()
        self.assertEqual(result, "failed")
        self.assertEqual(agent.task_plan, [])
        self.assertEqual(agent.vnc.events, [])

    def test_relocated_pointer_still_cannot_bypass_independent_gate(self):
        frame = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=False)
        agent = make_agent([frame, frame])
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent.history = [{"action": "click", "params": {"target": "体验中心", "x": .1, "y": .1},
                          "executed": False, "pointer_target_verification": {
                              "passed": False, "suggested_x": .2, "suggested_y": .3}}]
        with patch("rpa_agent.chat_vision") as vision, \
             patch.object(agent, "saveScreenShot", side_effect=virtual_screenshot_path), \
             patch("rpa_agent.time.sleep"), \
             patch.object(agent, "_verify_query_pointer_target", return_value={"passed": False, "reason": "still misses"}) as gate:
            agent._decision_step("查询智能问数和新SQL生成", "", None)
        vision.assert_not_called()
        gate.assert_called_once()
        self.assertFalse(agent.history[-1]["executed"])
        self.assertEqual(agent.history[-1]["source"], "verified_pointer_relocation")
        self.assertEqual(agent.vnc.events, [])

    def test_entry_navigation_requires_visible_browser(self):
        frame = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=False)
        agent = make_agent([frame])
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent._start_at_login_entry = True
        with patch("rpa_agent.chat_vision", return_value=json.dumps({"action": "wait", "params": {"seconds": .5}})), \
             patch.object(agent, "_verify_visible_state", return_value={"passed": False, "reason": "desktop"}), \
             patch.object(agent, "saveScreenShot", side_effect=virtual_screenshot_path), patch("rpa_agent.time.sleep"):
            agent._decision_step("查询智能问数和新SQL生成", "", None)
        self.assertEqual(agent.history[-1]["action"], "wait")
        self.assertEqual(agent.vnc.events, [])

    def test_direct_url_verification_still_requires_independent_actual_text(self):
        frame = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=False)
        for actual, passed in (("http://example.com", True), ("http;//example.com", False)):
            agent = make_agent([frame])
            agent.pending_input = {"field_type": "url", "source_skill": "skill_open_url",
                                   "text": "http://example.com", "verified": False}
            with patch("rpa_agent.chat_vision", return_value=json.dumps({
                "observed_text": actual, "readable": True, "ime_visible": False,
            })) as vision, patch.object(agent, "saveScreenShot", side_effect=virtual_screenshot_path), \
                 patch("rpa_agent.time.sleep"), patch.object(agent, "_has_windows_ime_candidate_panel", return_value=False):
                agent._decision_step("open URL", "", None)
            self.assertEqual(vision.call_count, 1)
            self.assertEqual(agent.pending_input["verified"], passed)
            self.assertEqual(agent.history[-1]["source"], "direct_url_verification")
            self.assertEqual(agent.vnc.events, [])

    def test_repeated_loop_requests_replan_before_stopping(self):
        agent = self.make_agent()
        agent.pending_input = None
        detected = SimpleNamespace(detected=True, period=1, cycle=("click",), repetitions=2)
        agent.interaction_guard = SimpleNamespace(detect_loop=lambda *_: detected)
        frame = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=False)
        proposal = {"action": "click", "params": {"x": 0.1, "y": 0.2, "target": "体验中心"}}
        agent._loop_decision(proposal, frame)
        _, _, evidence = agent._loop_decision(proposal, frame)
        self.assertFalse(agent._abort_requested)
        self.assertTrue(agent._loop_replan_requested)
        self.assertTrue(evidence["replan_requested"])
        self.assertEqual(agent.task_plan, [])
        status, record = self.step(agent, frame, {"action": "wait", "params": {"seconds": 0}})
        self.assertEqual(record["source"], "loop_replan")
        self.assertFalse(agent._loop_replan_requested)

    def test_url_preflight_switches_before_first_character(self):
        agent = input_helpers.make_agent()
        readings = iter(["chinese", "english"])
        agent._read_focused_ime_mode = lambda: next(readings)
        with patch("vnc_client.time.sleep"), patch("builtins.print"):
            self.assertTrue(agent._input_once("http://example.com", "url", replace=True))
        events = agent.vnc._client.events
        self.assertEqual(events[:2], [("press", "esc"), ("press", "shift")])
        self.assertEqual(events.count(("press", "shift")), 1)
        self.assertEqual(events.count(("press", "h")), 1)
        self.assertEqual(agent.input_retry_count, 0)

    def test_url_preflight_leaves_english_unchanged(self):
        agent = input_helpers.make_agent()
        with patch("vnc_client.time.sleep"), patch("builtins.print"):
            self.assertTrue(agent._input_once("http://example.com", "url", replace=True))
        self.assertNotIn(("press", "shift"), agent.vnc._client.events)

    def test_url_preflight_unknown_or_failed_switch_never_types(self):
        for modes in (["unknown"], ["chinese", "chinese"]):
            with self.subTest(modes=modes):
                agent = input_helpers.make_agent()
                readings = iter(modes)
                agent._read_focused_ime_mode = lambda: next(readings)
                with patch("vnc_client.time.sleep"), patch("builtins.print"):
                    self.assertFalse(agent._input_once("http://example.com", "url", replace=True))
                self.assertNotIn(("press", "h"), agent.vnc._client.events)
                self.assertNotIn(("down", "ctrl"), agent.vnc._client.events)
                self.assertIsNone(agent.pending_input)
                self.assertIn("未清空或输入网址", agent._repeat_guidance)
                self.assertNotIn("部分文本", agent._repeat_guidance)

    def test_ime_reader_requires_visible_evidence_and_fresh_frame(self):
        agent = self.make_agent()
        frame = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=False)
        for response, expected in (({"mode": "english", "evidence": "任务栏英"}, "english"),
                                   ({"mode": "english"}, "unknown")):
            with patch.object(agent.vnc, "screenshot", return_value=frame) as capture, \
                 patch("rpa_agent.chat_vision", return_value=json.dumps(response)) as reader, \
                 patch("rpa_agent.time.sleep"):
                self.assertEqual(agent._read_focused_ime_mode(), expected)
                capture.assert_called_once()
                region = reader.call_args.args[1]
                self.assertEqual(region.size, (
                    (frame.width - int(frame.width * 0.65)) * 3,
                    (frame.height - int(frame.height * 0.88)) * 3,
                ))

    def test_url_reader_rejects_echoed_expected_value_and_repairs_via_ime(self):
        agent = self.make_agent()
        url = agent.task_policy.login_url
        agent.pending_input.update(text=url, field_type="url", source_skill="skill_open_url")
        frame = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=False)
        wrong = {"observed_text": "图片: //172.19.133.168:7010/web/views/login.h't'm'l",
                 "readable": True, "ime_visible": False}
        with patch("rpa_agent.chat_vision", return_value=json.dumps(wrong)) as reader:
            agent._verify_pending_input({"observed_text": url, "readable": True}, frame)
        self.assertNotIn(url, reader.call_args.args[0])
        self.assertFalse(agent.pending_input["verified"])
        self.assertEqual(agent.pending_input["cause"], "ime")
        status, record = self.step(agent, frame, {}, expect_model=False)
        self.assertTrue(record["executed"])
        self.assertEqual(record["action"], "retry_text_input")
        events = agent.vnc._client.events
        self.assertEqual(events.count(("press", "shift")), 1)
        self.assertEqual(events.count(("down", "shift")), url.count(":"))
        self.assertEqual(events.count(("up", "shift")), url.count(":"))
        self.assertNotIn(("press", ":"), events)
        self.assertNotIn(("press", "enter"), events)

    def test_url_reader_failure_cannot_accept_model_claim(self):
        agent = self.make_agent()
        agent.pending_input.update(text=agent.task_policy.login_url, field_type="url")
        frame = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=False)
        with patch("rpa_agent.chat_vision", side_effect=RuntimeError("reader unavailable")):
            agent._verify_pending_input({"observed_text": agent.task_policy.login_url,
                                         "readable": True}, frame)
        self.assertFalse(agent.pending_input["verified"])

    def test_taskbar_search_request_uses_browser_macro(self):
        agent = self.make_agent()
        agent.pending_input = None
        proposal = {"action": "click", "params": {
            "x": 0.414, "y": 0.975, "target": "搜索",
        }, "thought": "按候选坐标重新点击任务栏搜索按钮，准备输入应用名打开浏览器。"}
        frame = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=False)
        with patch.object(agent, "_verify_query_pointer_target") as pointer:
            status, record = self.step(agent, frame, proposal)
        pointer.assert_not_called()
        self.assertEqual(status, "continue")
        self.assertEqual(record["action"], "skill_open_app")
        self.assertEqual(record["requested_action"], proposal)
        self.assertTrue(record["executed"])
        self.assertEqual(agent.pending_input["text"], "edge")
        self.assertEqual(agent.pending_input["source_skill"], "skill_open_app")
        self.assertFalse(agent.pending_input["verified"])
        self.assertNotIn(("press", "enter"), agent.vnc._client.events)

    def test_business_search_and_pending_input_are_not_rewritten(self):
        agent = self.make_agent()
        proposal = {"action": "click", "params": {"target": "搜索"},
                    "thought": "点击任务栏搜索以打开浏览器"}
        self.assertIs(agent._normalize_browser_search_request(proposal), proposal)
        agent.pending_input = None
        agent.task_plan = [{"content": "业务搜索"}]
        self.assertIs(agent._normalize_browser_search_request(proposal), proposal)
        agent.task_plan = []
        business = {"action": "click", "params": {"target": "搜索"},
                    "thought": "点击业务页面搜索按钮"}
        self.assertIs(agent._normalize_browser_search_request(business), business)
        self.assertFalse(agent.task_policy.validate_runtime_action("click", business["params"])[0])

    def make_agent(self):
        agent = make_agent([])
        agent.vnc = input_helpers.make_vnc()
        agent.task_policy = QueryOnlyPolicy.for_task("查询智能问数和新SQL生成")
        agent.pending_input = {
            "text": "edge", "field_type": "app_search",
            "verified": False, "verification_attempted": False,
            "source_skill": "skill_open_app",
        }
        return agent

    def step(self, agent, frame, action, *, expect_model=True):
        with (
            patch.object(agent.vnc, "screenshot", return_value=frame),
            patch.object(agent, "saveScreenShot", side_effect=virtual_screenshot_path),
            patch.object(rpa_agent, "chat_vision", return_value=json.dumps(action)) as vision,
            patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"),
            patch("builtins.print"),
        ):
            status = agent._decision_step("查询智能问数和新SQL生成", "", None)
        self.assertEqual(vision.call_count, int(expect_model))
        return status, agent.history[-1]

    def test_candidate_repair_reverification_and_enter_form_one_complete_flow(self):
        candidate = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=True)
        committed = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=False)
        agent = self.make_agent()
        verify = {"action": "verify_text_input", "params": {
            "observed_text": "edge", "readable": True, "ime_visible": False,
        }, "thought": "验收通过"}

        status, record = self.step(agent, candidate, verify)
        self.assertEqual(status, "continue")
        self.assertFalse(record["input_verification"]["passed"])
        self.assertTrue(record["execution_feedback"])
        self.assertIsNone(getattr(agent, "_app_launch_recovery", None))

        status, record = self.step(agent, candidate, {}, expect_model=False)
        self.assertEqual(record["action"], "retry_text_input")
        self.assertEqual(record["source"], "local_ime_recovery")
        self.assertTrue(record["executed"])
        self.assertEqual(agent.input_retry_count, 1)
        self.assertFalse(agent.pending_input["verified"])
        events = agent.vnc._client.events
        self.assertIn(("press", "esc"), events)
        self.assertEqual(events.count(("press", "shift")), 1)
        self.assertNotIn(("press", "enter"), events)
        self.assertFalse(any(event[0].startswith("mouse") for event in events))

        # Same verify signature as the first frame: it must reach the field gate.
        status, record = self.step(agent, committed, verify)
        self.assertEqual(status, "continue")
        self.assertTrue(record["input_verification"]["passed"])
        self.assertTrue(agent.pending_input["verified"])
        status, record = self.step(agent, committed, {
            "action": "press", "params": {"key": "enter"},
        })
        self.assertTrue(record["executed"])
        self.assertEqual(events.count(("press", "enter")), 1)
        self.assertEqual(agent._app_launch_recovery["application"], "edge")

    def test_failed_repair_stops_without_second_toggle_or_submit(self):
        candidate = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=True)
        agent = self.make_agent()
        verify = {"action": "verify_text_input", "params": {
            "observed_text": "edge", "readable": True, "ime_visible": False,
        }}
        self.step(agent, candidate, verify)
        self.step(agent, candidate, {}, expect_model=False)
        status, _ = self.step(agent, candidate, verify)
        self.assertEqual(status, "failed")
        events = agent.vnc._client.events
        self.assertEqual(events.count(("press", "shift")), 1)
        self.assertNotIn(("press", "enter"), events)

    def test_pending_composition_blocks_pointer_before_visual_hit_check(self):
        candidate = input_helpers.ApplicationLaunchRecoveryTests._start_search_image(candidate_panel=True)
        agent = self.make_agent()
        # Missing controlled source must never authorize an automatic repair.
        agent.pending_input.update({
            "source_skill": None, "cause": "ime",
            "verification_attempted": True, "ime_evidence": "windows_candidate_panel",
        })
        with patch.object(agent, "_verify_query_pointer_target") as pointer:
            status, record = self.step(agent, candidate, {
                "action": "click", "params": {
                    "x": 0.343, "y": 0.288, "target": "Microsoft Edge 最佳匹配",
                },
            })
        pointer.assert_not_called()
        self.assertFalse(record["executed"])
        self.assertIn("尚未通过", record["execution_feedback"])
        self.assertEqual(agent.vnc._client.events, [])


if __name__ == "__main__":
    unittest.main()
