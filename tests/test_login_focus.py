import unittest
from unittest.mock import patch
from PIL import Image
from test_input_method import make_agent


class LoginFocusTests(unittest.TestCase):
    def test_failed_focus_never_sends_replacement_keys(self):
        for field in ("username", "password"):
            agent = make_agent()
            with patch.object(agent, "_verify_visible_state", return_value={
                    "passed": False, "reason": "标题和按钮同时选中"}), \
                    patch.object(agent.vnc, "screenshot", return_value=Image.new("RGB", (100, 80))), \
                    patch.object(agent.vnc.ime, "type_value") as writer:
                self.assertFalse(agent._input_once("example", field))
            writer.assert_not_called()
            self.assertIsNone(agent.pending_input)
            self.assertEqual(agent.input_retry_count, 0)
            self.assertFalse(agent._abort_requested)

    def test_focus_error_is_not_permission_to_type(self):
        agent = make_agent()
        with patch.object(agent.vnc, "screenshot", side_effect=RuntimeError("capture failed")), \
                patch.object(agent.vnc.ime, "type_value") as writer:
            self.assertFalse(agent._input_once("example", "username"))
        writer.assert_not_called()

    def test_retry_focus_failure_preserves_budget_and_text(self):
        agent = make_agent()
        agent.pending_input = {"field_type": "username", "text": "example",
            "verified": False, "verification_attempted": True, "cause": "focus", "refocused": True}
        with patch.object(agent, "_verify_visible_state", return_value={"passed": False}), \
                patch.object(agent.vnc, "screenshot", return_value=Image.new("RGB", (100, 80))), \
                patch.object(agent.vnc.ime, "type_value") as writer:
            self.assertFalse(agent._retry_input("example", "username", "focus"))
        writer.assert_not_called()
        self.assertEqual(agent.input_retry_count, 0)
        self.assertEqual(agent.pending_input["text"], "example")
        self.assertFalse(agent.pending_input["refocused"])

    def test_real_verifier_signature_and_focus_evidence(self):
        agent = make_agent()
        with patch.object(agent.vnc, "screenshot", return_value=Image.new("RGB", (100, 80))), \
                patch("rpa_agent.chat_vision", return_value='{"passed":true,"evidence":["密码框内光标"],"reason":"focused"}') as reader:
            self.assertTrue(agent._confirm_login_input_focus("password"))
        self.assertIn("整页选区", reader.call_args.args[0])

    def test_macro_focus_failure_rolls_back_stage_without_text(self):
        agent = make_agent()
        with patch.object(agent, "_verify_visible_state", return_value={"passed": False}), \
                patch.object(agent.vnc, "screenshot", return_value=Image.new("RGB", (100, 80))), \
                patch("rpa_agent.time.sleep"), patch("vnc_client.time.sleep"), \
                patch.object(agent.vnc.ime, "type_value") as writer:
            self.assertFalse(agent._execute_action({"action": "skill_open_webpage", "params": {
                "needlogin": True, "stage": "username", "username": "example",
                "loginCoordinates": {"x": .7, "y": .4}}}, Image.new("RGB", (100, 80))))
        writer.assert_not_called()
        self.assertEqual(agent.login_progress, "idle")


if __name__ == "__main__":
    unittest.main()
