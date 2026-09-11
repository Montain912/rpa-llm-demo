import json
import os
import unittest
from unittest.mock import patch
from prompt_context import history_for_prompt, login_for_prompt, direct_url_verification, use_business_knowledge, pointer_relocation
from prompt_context import query_system_prompt, lean_query_enabled, direct_stage_check


class PromptContextTests(unittest.TestCase):
    def test_stage_checks_only_follow_executed_actions_in_same_stage(self):
        record = {"action": "click", "executed": True, "params": {"target": "type选项"}, "subtask": {"content": "select type"}}
        self.assertEqual(direct_stage_check([record], {"content": "select type", "expected_result": "type selected"}, "submitted")["action"], "subtask_done")
        self.assertIsNone(direct_stage_check([record], {"content": "another stage"}, "submitted"))
        self.assertIsNone(direct_stage_check([dict(record, executed=False)], {"content": "select type"}, "submitted"))
        submitted = {"action": "skill_open_webpage", "executed": True, "params": {"stage": "submit"}}
        self.assertEqual(direct_stage_check([submitted], None, "submitted")["action"], "planning")
        self.assertIsNone(direct_stage_check([submitted], None, "password_verified"))
        self.assertIsNone(direct_stage_check([{"action": "subtask_done", "executed": False}], {}, "submitted"))

    def test_parent_menu_does_not_trigger_stage_completion_probe(self):
        plan = {"content": "进入体验中心的智能体能力页面", "expected_result": "主内容区显示智能体能力及测试类型"}
        record = {"action": "click", "executed": True, "params": {"target": "体验中心"}, "subtask": {"content": plan["content"]}}
        self.assertIsNone(direct_stage_check([record], plan, "submitted"))
        record["params"]["target"] = "智能体能力菜单项"
        self.assertEqual(direct_stage_check([record], plan, "submitted")["action"], "subtask_done")
    def test_lean_query_prompt_is_opt_in_by_task_and_can_be_disabled(self):
        self.assertEqual(query_system_prompt("original", "windows", False), "original")
        with patch.dict(os.environ, {"RPA_LEAN_QUERY": "0"}):
            self.assertEqual(query_system_prompt("original", "windows", True), "original")
            self.assertFalse(lean_query_enabled(True))
        prompt = query_system_prompt("original", "linux:super", True)
        self.assertTrue(prompt.endswith("linux:super"))
        for guard in ("verify_text_input", "最多一次", "独立验收", "不重复提交", "禁止", "Win+R"):
            self.assertIn(guard, prompt)

    def test_lean_history_preserves_outcomes_and_latest_recovery_without_mutating_log(self):
        records = [{"step": 1, "action": "click", "params": {"target": "menu"}, "executed": False,
                    "thought": "old verbose speculation", "execution_feedback": "wrong row",
                    "pointer_target_verification": {"passed": False, "suggested_x": .3, "suggested_y": .4}},
                   {"step": 2, "action": "subtask_done", "executed": False, "thought": "latest reasoning",
                    "state_verification": {"passed": False, "state": "unchanged", "reason": "not selected", "evidence": ["unchecked"]}}]
        original = json.dumps(records)
        result = json.loads(history_for_prompt(records, lean=True))
        self.assertFalse(result[0]["executed"])
        self.assertEqual(result[0]["pointer_target_verification"]["suggested_x"], .3)
        self.assertEqual(result[0]["execution_feedback"], "wrong row")
        self.assertNotIn("thought", result[0])
        self.assertEqual(result[1]["thought"], "latest reasoning")
        self.assertFalse(result[1]["state_verification"]["passed"])
        self.assertEqual(json.dumps(records), original)
        with patch.dict(os.environ, {"RPA_COMPACT_HISTORY": "0"}):
            self.assertEqual(json.loads(history_for_prompt(records, lean=True)), records)

    def test_pointer_suggestion_is_bounded_and_keeps_original_target(self):
        record = {"action": "click", "params": {"target": "menu", "x": .1, "y": .1},
                  "executed": False, "subtask": {"content": "open menu"},
                  "pointer_target_verification": {"passed": False, "suggested_x": .2, "suggested_y": .3}}
        result = pointer_relocation([record], {"content": "open menu"})
        self.assertEqual(result["params"], {"target": "menu", "x": .2, "y": .3})
        self.assertEqual(record["params"]["x"], .1)
        self.assertIsNone(pointer_relocation([record], {"content": "different step"}))
        self.assertIsNone(pointer_relocation([dict(record, source="verified_pointer_relocation")], None))
        self.assertIsNone(pointer_relocation([dict(record, executed=True)], None))
        for invalid in (None, True, -1, 2, float("nan")):
            bad = dict(record, pointer_target_verification={"passed": False, "suggested_x": invalid, "suggested_y": .3})
            self.assertIsNone(pointer_relocation([bad], None))

    def test_preserves_failure_evidence_and_does_not_mutate_log(self):
        record = {"action": "click", "params": {"x": .4, "target": "menu"},
                  "executed": False, "duration": {"total": 4},
                  "screenshots": {"before": "large-path.png"},
                  "pointer_target_verification": {"passed": False, "suggested_y": .3,
                      "target_bbox": [.1, .2, .4, .5], "reason": "wrong row"}}
        original = json.dumps(record)
        compact = json.loads(history_for_prompt([record]))[0]
        self.assertFalse(compact["executed"])
        self.assertEqual(compact["pointer_target_verification"], record["pointer_target_verification"])
        self.assertEqual(compact["params"], record["params"])
        self.assertNotIn("screenshots", compact)
        self.assertEqual(json.dumps(record), original)

    def test_rollback_flag_and_history_window(self):
        history = [{"step": i, "duration": {"total": 1}} for i in range(8)]
        with patch.dict(os.environ, {"RPA_COMPACT_HISTORY": "0"}):
            self.assertEqual(json.loads(history_for_prompt(history)), history[-5:])
        self.assertEqual(len(json.loads(history_for_prompt(history))), 5)

    def test_empty_history(self):
        self.assertIn("第一步", history_for_prompt([]))

    def test_full_knowledge_returns_during_input_and_recovery(self):
        self.assertTrue(use_business_knowledge({}, None, ""))
        self.assertFalse(use_business_knowledge(None, None, ""))
        self.assertFalse(use_business_knowledge({}, {"verified": False}, ""))
        self.assertFalse(use_business_knowledge({}, None, "missed target"))
        with patch.dict(os.environ, {"RPA_PHASE_KNOWLEDGE": "0"}):
            self.assertFalse(use_business_knowledge({}, None, ""))

    def test_login_state_never_trusts_prefill(self):
        self.assertIn("stage='username'", login_for_prompt("idle", False, "/login"))
        self.assertIn("stage='password'", login_for_prompt("username_verified", True, "/login"))
        self.assertIn("stage='submit'", login_for_prompt("password_verified", True, "/login"))
        self.assertIn("不代表本次输入已经验收", login_for_prompt("idle", False, "/login"))

    def test_direct_url_reader_only_for_controlled_unverified_input(self):
        pending = {"field_type": "url", "source_skill": "skill_open_url", "text": "expected"}
        self.assertEqual(direct_url_verification(pending)["params"], {})
        for update in ({"verified": True}, {"verification_attempted": True},
                       {"source_skill": None}, {"field_type": "password"}):
            self.assertIsNone(direct_url_verification(dict(pending, **update)))
        with patch.dict(os.environ, {"RPA_DIRECT_URL_VERIFY": "0"}):
            self.assertIsNone(direct_url_verification(pending))
