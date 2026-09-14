"""Fresh-checkout output tests; all model and VNC connections are mocked."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
import runtime_paths


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimePathsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        replacements = {}
        for name, attributes in {
            'llm_client': ['chat_vision', 'chat_text', 'token_tracker'],
            'vnc_client': ['VNCClient'],
            'knowledge_loader': ['get_summary'],
            'tools': ['open_application', 'open_webpage', 'open_url', 'planning'],
        }.items():
            stub = types.ModuleType(name)
            for attribute in attributes:
                setattr(stub, attribute, MagicMock())
            replacements[name] = stub
        with patch.dict(sys.modules, replacements):
            cls.agent_module = load_module('runtime_test_agent', 'rpa_agent.py')
        openai_stub = types.ModuleType('openai')
        openai_stub.OpenAI = MagicMock()
        with patch.dict(sys.modules, {'openai': openai_stub}):
            cls.model_module = load_module('runtime_test_model', 'llm_client.py')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rpa-output-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.patch_root = patch.object(runtime_paths, 'PROJECT_ROOT', self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.frame = Image.new('RGB', (16, 12), (50, 100, 150))

    def test_first_screenshot_creates_missing_directory(self):
        self.assertFalse(self.root.exists())
        agent = self.agent_module.RPAgent()
        agent.saveScreenShot(1, 543680, self.frame)
        target = self.root / 'screenshots' / 'sh_543680_1.png'
        with Image.open(target) as saved:
            self.assertEqual(saved.size, (16, 12))
            self.assertEqual(saved.getpixel((0, 0)), (50, 100, 150))

    def test_output_location_does_not_follow_current_directory(self):
        other = Path(self.temp.name) / 'other-working-directory'
        other.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(other)
            self.agent_module.RPAgent().saveScreenShot(2, 543680, self.frame)
        finally:
            os.chdir(previous)
        self.assertTrue((self.root / 'screenshots' / 'sh_543680_2.png').is_file())
        self.assertFalse((other / 'screenshots').exists())

    def test_existing_output_is_preserved_and_multiple_calls_succeed(self):
        first = runtime_paths.runtime_path('screenshots', 'existing.txt')
        first.write_text('keep', encoding='utf-8')
        agent = self.agent_module.RPAgent()
        agent.saveScreenShot(1, 123, self.frame)
        agent.saveScreenShot(2, 123, self.frame)
        self.assertEqual(first.read_text(encoding='utf-8'), 'keep')
        self.assertEqual(len(list(first.parent.glob('*.png'))), 2)

    def test_model_output_directory_exists_before_mocked_api_call(self):
        response = types.SimpleNamespace(
            usage=None, choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='测试输出'))],
        )
        client = MagicMock()

        def fake_completion(**kwargs):
            self.assertTrue(list((self.root / 'llm_output').glob('screenshot_*.png')))
            return response

        client.chat.completions.create.side_effect = fake_completion
        with patch.object(self.model_module, 'client', client), patch.object(self.model_module, 'token_tracker'):
            result = self.model_module.chat_vision('local test', self.frame)
        self.assertEqual(result, '测试输出')
        files = list((self.root / 'llm_output').glob('vision_response_*.txt'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(encoding='utf-8'), '测试输出')
        client.chat.completions.create.assert_called_once()

    def test_summary_path_is_created_in_project(self):
        target = runtime_paths.runtime_path('summary', 'token_usage_1.json')
        self.assertTrue(target.parent.is_dir())
        self.assertEqual(target, self.root / 'summary' / 'token_usage_1.json')

    def test_windows_and_linux_agent_prompts_do_not_share_system_state(self):
        win = self.agent_module.RPAgent(system='win')
        linux = self.agent_module.RPAgent(system='linux')
        self.assertEqual(win.system, 'windows')
        for template in (self.agent_module.SYSTEM_PROMPT, self.agent_module.SYSTEM_PROMPT_PLANNING):
            rendered = self.agent_module._prompt_for_system(template, win.system)
            self.assertTrue(rendered.endswith('WINDOWS'))
            self.assertNotIn('严禁使用 Win+R', rendered)
            self.assertTrue(self.agent_module._prompt_for_system(template, linux.system).endswith('LINUX'))
        with patch.object(self.agent_module, 'open_application', return_value=[]) as skill:
            win._expand_skill('skill_open_app', {'application': 'edge', 'system': 'linux'})
        self.assertEqual(skill.call_args.kwargs['system'], 'windows')

    def test_planning_failure_disconnects_before_any_keyboard_action(self):
        agent = self.agent_module.RPAgent(system='win')
        agent.vnc = MagicMock()
        with patch.object(self.agent_module, 'chat_text', side_effect=RuntimeError('mock 402')), \
             patch.object(self.agent_module, 'get_summary', return_value=''), \
             patch.object(self.agent_module, 'token_tracker'):
            with self.assertRaisesRegex(RuntimeError, 'mock 402'):
                agent.run_task('local mock task')
        agent.vnc.disconnect.assert_called_once()
        agent.vnc.press_key.assert_not_called()

    def test_pointer_events_do_not_implicitly_open_system_menu(self):
        for action, y in [('click', .975), ('double_click', .2)]:
            agent = self.agent_module.RPAgent(system='win')
            agent.vnc = MagicMock()
            agent.interaction = MagicMock()
            agent._execute_action({'action': action, 'params': {'x': .5, 'y': y, 'target': 'fixture'}}, self.frame)
            self.assertTrue(agent._last_action_executed)
            agent.interaction.pointer.assert_called_once()
            agent.vnc.maximize_window.assert_not_called()
            agent.interaction.ensure_browser_maximized.assert_not_called()

    def test_browser_launcher_click_and_double_click_restore_auto_maximize(self):
        for action in ('click', 'double_click'):
            agent = self.agent_module.RPAgent(system='win')
            agent.vnc = MagicMock()
            agent.interaction = MagicMock()
            with patch.object(self.agent_module.time, 'sleep'):
                agent._execute_action({'action': action, 'params': {
                    'x': .03, 'y': .2, 'target': 'Microsoft Edge 图标'}}, self.frame)
            self.assertTrue(agent._last_action_executed)
            agent.interaction.pointer.assert_called_once()
            agent.interaction.ensure_browser_maximized.assert_called_once()
            agent.vnc.maximize_window.assert_not_called()

    def test_explicit_browser_maximize_does_not_use_system_menu(self):
        agent = self.agent_module.RPAgent(system='win')
        agent.task = '打开浏览器'
        agent.vnc = MagicMock()
        agent.interaction = MagicMock()
        agent._execute_action({'action': 'maximize_window', 'params': {}}, self.frame)
        agent.interaction.ensure_browser_maximized.assert_called_once()
        agent.vnc.maximize_window.assert_not_called()

    def test_all_input_aliases_use_the_verified_ime_path(self):
        for action in ('type', 'input_text', 'skill_input_text', 'retry_text_input'):
            agent = self.agent_module.RPAgent(system='win')
            agent.vnc = MagicMock()
            agent.interaction = MagicMock()
            agent._execute_action({'action': action, 'params': {'text': 'abc', 'field_type': 'username'}}, self.frame)
            agent.interaction.input.assert_called_once_with('abc', 'username', replace=True)
            agent.vnc.type_text.assert_not_called()

    def test_cleanup_failures_do_not_hide_original_error(self):
        agent = self.agent_module.RPAgent(system='win')
        agent.vnc = MagicMock()
        agent.vnc.disconnect.side_effect = RuntimeError('cleanup failed')
        with patch.object(self.agent_module, 'chat_text', side_effect=RuntimeError('original failure')), \
             patch.object(self.agent_module, 'get_summary', return_value=''), \
             patch.object(self.agent_module, 'token_tracker') as tracker:
            tracker.save.side_effect = OSError('disk error')
            with self.assertRaisesRegex(RuntimeError, 'original failure'):
                agent.run_task('local mock task')

    def test_remote_system_controls_show_desktop_key(self):
        for system, key in [('win', 'win+d'), ('linux', 'super+d')]:
            agent = self.agent_module.RPAgent(system=system)
            agent.vnc = MagicMock()
            with patch.object(self.agent_module, 'chat_text', return_value='mock plan'), \
                 patch.object(self.agent_module, 'get_summary', return_value=''), \
                 patch.object(self.agent_module, 'token_tracker'), \
                 patch.object(self.agent_module.time, 'sleep'), \
                 patch.object(agent, '_decision_step', return_value='done'):
                agent.run_task('local mock task')
            agent.vnc.press_key.assert_called_once_with(key)

    def test_first_observation_runs_end_to_end_with_mock_models_and_vnc(self):
        agent = self.agent_module.RPAgent(system='win')
        agent.vnc = MagicMock()
        agent.vnc.screenshot.return_value = self.frame
        with patch.object(self.agent_module, 'chat_text', return_value='local test plan'), \
             patch.object(self.agent_module, 'chat_vision', side_effect=[
                 '{"action":"done","params":{},"thought":"local mock only"}',
                 '{"passed":true,"evidence":["mock terminal state"],"reason":"local fixture"}',
             ]) as vision, \
             patch.object(self.agent_module, 'get_summary', return_value=''), \
             patch.object(self.agent_module, 'token_tracker'), \
             patch.object(self.agent_module.time, 'sleep'):
            result = agent.run_task('local mock task')
        self.assertIn('任务完成', result)
        self.assertEqual(agent.step, 1)
        self.assertTrue((self.root / 'screenshots' / f'sh_{agent.rand}_1.png').is_file())
        self.assertIn('当前操作系统为：\nWINDOWS', vision.call_args_list[0].kwargs['system_prompt'])
        self.assertEqual(vision.call_count, 2)
        agent.vnc.disconnect.assert_called_once()

    def test_logout_ends_only_after_independent_full_task_verification(self):
        for passes in (True, False):
            agent = self.agent_module.RPAgent(system='win')
            agent.vnc = MagicMock()
            agent.vnc.screenshot.return_value = self.frame
            agent.rand = 10
            agent.planning = 'login, query, logout'
            agent.interaction = MagicMock()
            agent.interaction.evidence = []
            agent.interaction._one_shot_search_sent = True
            agent.interaction.control_role.return_value = 'logout'
            if not passes:
                agent.interaction.verify_completion.side_effect = self.agent_module.ObservationRequired('query evidence missing')
            with patch.object(self.agent_module, 'chat_vision', return_value=
                              '{"thought":"logout", "action":"click", "params":{"x":0.8,"y":0.2,"target":"退出登录"}}'), \
                 patch.object(self.agent_module.time, 'sleep'):
                status = agent._decision_step('查询后退出登录', '', None)
            self.assertEqual(status, 'done' if passes else 'continue')
            agent.interaction.verify_completion.assert_called_once()
            self.assertEqual(agent.history[-1]['completion_verified'], passes)


if __name__ == '__main__':
    unittest.main()
