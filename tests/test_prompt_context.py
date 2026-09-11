import json
import os
import unittest
from unittest.mock import patch
from prompt_context import history_for_prompt, login_for_prompt, direct_url_verification, use_business_knowledge, pointer_relocation


class PromptContextTests(unittest.TestCase):
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
