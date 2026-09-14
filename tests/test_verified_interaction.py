import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image
from verified_interaction import VerifiedInteraction, ObservationRequired


class VerifiedInteractionTests(unittest.TestCase):
    def test_ime_mode_is_mapped_from_glyph_not_a_contradictory_label(self):
        self.assertEqual(VerifiedInteraction.mode_from_glyph({'glyph': '英', 'mode': 'chinese', 'evidence': 'tray'}), 'english')
        self.assertEqual(VerifiedInteraction.mode_from_glyph({'glyph': '中', 'mode': 'english', 'evidence': 'tray'}), 'chinese')
        self.assertEqual(VerifiedInteraction.mode_from_glyph({'glyph': 'unknown', 'evidence': 'missing'}), 'unknown')

    def setUp(self):
        self.vnc = MagicMock()
        self.vnc.ime.normalize_text.side_effect = lambda text, field: text
        self.agent = SimpleNamespace(vnc=self.vnc, system='windows', step=1,
                                     task='login and query only', _to_pixels=lambda p, f: (10, 10))
        self.guard = VerifiedInteraction(self.agent, MagicMock())
        self.frame = Image.new('RGB', (120, 100), 'white')
        self.guard.capture = MagicMock(return_value=self.frame)

    def test_notepad_is_not_allowed_as_url_input_target(self):
        self.guard.inspect = MagicMock(return_value={'passed': False, 'reason': 'Notepad'})
        with self.assertRaises(ObservationRequired):
            self.guard.open_url('http://example.test/')
        self.vnc.press_key.assert_not_called()
        self.vnc.ime.type_value.assert_not_called()

    def test_url_input_does_not_open_a_window_system_menu(self):
        self.guard.input = MagicMock()
        self.guard.open_url('http://example.test/')
        self.guard.input.assert_called_once_with('http://example.test/', 'url', replace=True)
        self.vnc.maximize_window.assert_not_called()
        self.vnc.press_key.assert_not_called()

    def test_open_app_uses_os_search_and_verified_input(self):
        self.guard.input = MagicMock()
        self.guard.ensure_browser_maximized = MagicMock()
        with patch('verified_interaction.time.sleep'):
            self.guard.open_app('edge')
        self.assertEqual([c.args[0] for c in self.vnc.press_key.call_args_list], ['win+s', 'enter'])
        self.guard.input.assert_called_once_with('edge', 'app_search', replace=True)
        self.guard.ensure_browser_maximized.assert_called_once()

    def test_windowed_browser_is_maximized_once_and_rechecked(self):
        self.guard.inspect = MagicMock(side_effect=[
            {'browser_front': True, 'maximized': False, 'evidence': 'desktop around window'},
            {'browser_front': True, 'maximized': True, 'evidence': 'full work area and restore icon'},
        ])
        with patch('verified_interaction.time.sleep'):
            self.guard.ensure_browser_maximized()
        self.vnc.press_key.assert_called_once_with('win+up')
        self.vnc.maximize_window.assert_not_called()
        self.assertEqual(self.guard.inspect.call_count, 2)

    def test_already_maximized_browser_receives_no_window_shortcut(self):
        self.guard.inspect = MagicMock(return_value={
            'browser_front': True, 'maximized': True, 'evidence': 'restore icon and screen edges'})
        self.guard.ensure_browser_maximized()
        self.vnc.press_key.assert_not_called()
        self.vnc.maximize_window.assert_not_called()

    def test_redundant_planner_shortcut_also_checks_browser_geometry(self):
        self.agent.task = '打开 Edge 浏览器'
        self.guard.inspect = MagicMock(return_value={
            'browser_front': True, 'maximized': True, 'evidence': 'maximized browser'})
        self.guard.press('win+up')
        self.vnc.press_key.assert_not_called()
        self.assertEqual(self.guard.evidence[-1], {'kind': 'browser_maximize', 'changed': False, 'verified': True})

    def test_non_browser_maximize_shortcut_is_preserved(self):
        self.agent.task = '打开记事本'
        self.guard.press('win+up')
        self.vnc.press_key.assert_called_once_with('win+up')

    def test_completion_uses_post_maximize_frame_not_post_launch_frame(self):
        self.agent.history = [{'step': 1, 'action': 'double_click', 'params': {'target': 'Edge'}, 'executed': True,
                               'verification': [
                                   {'kind': 'pointer_after', 'screenshot': 'windowed.png'},
                                   {'kind': 'browser_window', 'screenshot': 'before.png'},
                                   {'kind': 'browser_window', 'screenshot': 'maximized.png'}]}]
        self.agent.task_plan = []
        self.guard.inspect = MagicMock(side_effect=[
            {'state': 'maximized browser', 'evidence': ['fills work area']},
            {'passed': True, 'evidence': ['maximized browser in latest frame']},
        ])
        opened = MagicMock()
        opened.__enter__.return_value.copy.return_value = self.frame
        with patch('verified_interaction.Image.open', return_value=opened) as reader:
            self.guard.verify_completion()
        reader.assert_called_once_with('maximized.png')

    def test_unknown_window_state_never_sends_shortcut(self):
        self.guard.inspect = MagicMock(return_value={
            'browser_front': True, 'maximized': None, 'evidence': 'borders unreadable'})
        with self.assertRaises(ObservationRequired):
            self.guard.ensure_browser_maximized()
        self.vnc.press_key.assert_not_called()

    def test_unsuccessful_maximize_blocks_url_input(self):
        self.guard.inspect = MagicMock(return_value={
            'browser_front': True, 'maximized': False, 'evidence': 'window is still half-screen'})
        with patch('verified_interaction.time.sleep'), self.assertRaises(ObservationRequired):
            self.guard.input('http://example.test/', 'url')
        self.vnc.press_key.assert_called_once_with('win+up')
        self.vnc.ime.type_value.assert_not_called()

    def test_direct_url_input_checks_window_before_focusing_address_bar(self):
        self.guard.inspect = MagicMock(side_effect=[
            {'browser_front': True, 'maximized': False, 'evidence': 'windowed'},
            {'browser_front': True, 'maximized': True, 'evidence': 'maximized'},
            {'focused': True, 'observed_text': 'http://example.test/', 'ime_visible': False, 'evidence': 'selected URL'},
        ])
        with patch('verified_interaction.time.sleep'):
            self.guard.input('http://example.test/', 'url')
        self.assertEqual([c.args[0] for c in self.vnc.press_key.call_args_list], ['win+up', 'ctrl+l'])

    def test_non_browser_app_does_not_trigger_browser_maximization(self):
        self.guard.input = MagicMock()
        self.guard.ensure_browser_maximized = MagicMock()
        with patch('verified_interaction.time.sleep'):
            self.guard.open_app('notepad')
        self.guard.ensure_browser_maximized.assert_not_called()

    def test_browser_launch_target_detection_excludes_browser_fields(self):
        for target in ('Microsoft Edge 图标', 'Edge 最佳匹配', '任务栏浏览器图标', 'Microsoft Edge'):
            self.assertTrue(self.guard.is_browser_launch_target(target), target)
        for target in ('用户名输入框', '任务栏搜索框', 'Edge 地址栏', '浏览器新建标签页按钮'):
            self.assertFalse(self.guard.is_browser_launch_target(target), target)

    def test_missing_focus_does_not_select_all_or_type(self):
        self.guard.inspect = MagicMock(return_value={'focused': False, 'evidence': 'whole page selected'})
        with self.assertRaises(ObservationRequired):
            self.guard.input('user', 'username')
        self.vnc.ime.type_value.assert_not_called()
        self.vnc.press_key.assert_not_called()

    def test_failed_ime_check_does_not_type(self):
        self.guard.inspect = MagicMock(return_value={'focused': True, 'evidence': 'caret'})
        self.vnc.ime.ensure_english.side_effect = RuntimeError('unknown mode')
        with self.assertRaises(RuntimeError):
            self.guard.input('user', 'username')
        self.vnc.ime.type_value.assert_not_called()

    def test_failed_input_cannot_be_submitted(self):
        self.guard.inspect = MagicMock(side_effect=[
            {'focused': True, 'evidence': 'caret'},
            {'observed_text': 'wrong', 'readable': False, 'ime_visible': False, 'evidence': ''},
        ])
        with patch('verified_interaction.time.sleep'), self.assertRaises(ObservationRequired):
            self.guard.input('user', 'username')
        with self.assertRaises(ObservationRequired):
            self.guard.press('enter')
        self.vnc.press_key.assert_not_called()

    def test_verified_input_can_be_submitted(self):
        self.guard.inspect = MagicMock(side_effect=[
            {'focused': True, 'evidence': 'caret'},
            {'observed_text': 'user', 'readable': True, 'ime_visible': False, 'evidence': 'read user'},
        ])
        with patch('verified_interaction.time.sleep'):
            self.guard.input('user', 'username')
        self.guard.press('enter')
        self.vnc.press_key.assert_called_once_with('enter')

    def test_password_is_verified_by_mask_length(self):
        self.guard.inspect = MagicMock(side_effect=[
            {'focused': True, 'evidence': 'caret'},
            {'mask_length': 6, 'readable': True, 'ime_visible': False, 'evidence': 'six dots'},
        ])
        with patch('verified_interaction.time.sleep'):
            self.guard.input('123456', 'password')
        self.assertTrue(self.guard.pending['verified'])

    def test_disallowed_pointer_never_sends_mouse_event(self):
        self.guard.inspect = MagicMock(return_value={
            'target_visible': True, 'inside': True, 'allowed': False,
            'bbox': [.1, .1, .3, .3], 'evidence': 'delete record',
        })
        with self.assertRaises(ObservationRequired):
            self.guard.pointer('click', {'x': .2, 'y': .2, 'target': 'delete'}, self.frame)
        self.vnc.click.assert_not_called()

    def test_pointer_correction_is_reverified_before_clicking(self):
        self.guard.inspect = MagicMock(side_effect=[
            {'target_visible': True, 'inside': False, 'allowed': True,
             'bbox': [.2, .2, .4, .4], 'evidence': 'button'},
            {'target_visible': True, 'inside': True, 'allowed': True,
             'bbox': [.2, .2, .4, .4], 'evidence': 'center'},
        ])
        with patch('verified_interaction.time.sleep'):
            self.guard.pointer('click', {'x': .1, 'y': .1, 'target': 'search'}, self.frame)
        self.assertEqual(self.guard.inspect.call_count, 2)
        self.vnc.click.assert_called_once()

    def test_local_zoom_rejects_adjacent_menu_item(self):
        self.guard.inspect = MagicMock(side_effect=[
            {'target_visible': True, 'inside': True, 'bbox': [.1, .1, .2, .2], 'evidence': 'menu'},
            {'hit': False, 'observed_label': 'different adjacent item', 'evidence': 'local crop mismatch'},
        ])
        with self.assertRaises(ObservationRequired):
            self.guard.pointer('click', {'x': .15, 'y': .15, 'target': 'target menu'}, self.frame)
        self.vnc.click.assert_not_called()

    def test_local_hit_flag_cannot_override_a_contradictory_label(self):
        self.guard.inspect = MagicMock(side_effect=[
            {'target_visible': True, 'inside': True, 'bbox': [.1, .1, .2, .2], 'evidence': 'icon'},
            {'hit': True, 'observed_label': '搜索按钮', 'evidence': 'magnifying glass'},
        ])
        with self.assertRaises(ObservationRequired):
            self.guard.pointer('click', {'x': .15, 'y': .15, 'target': '退出登录'}, self.frame)
        self.vnc.click.assert_not_called()

    def test_faint_icon_can_use_positive_local_evidence_and_geometry(self):
        self.guard.inspect = MagicMock(side_effect=[
            {'target_visible': False, 'inside': False, 'bbox': [0, 0, 0, 0], 'evidence': 'faint'},
            {'hit': True, 'observed_label': '退出登录门框图标', 'evidence': 'visible arrow',
             'bbox': [.1, .1, .2, .2]},
        ])
        with patch('verified_interaction.time.sleep'):
            self.guard.pointer('click', {'x': .15, 'y': .15, 'target': '退出登录'}, self.frame)
        self.vnc.click.assert_called_once()

    def test_local_geometry_rechecks_a_claimed_hit_outside_the_box(self):
        self.guard.inspect = MagicMock(side_effect=[
            {'target_visible': True, 'inside': True, 'bbox': [.1, .1, .2, .2], 'evidence': 'field'},
            {'hit': True, 'observed_label': '用户名', 'evidence': 'field', 'bbox': [.2, .2, .3, .3]},
            {'hit': True, 'observed_label': '用户名', 'evidence': 'center', 'bbox': [.2, .2, .3, .3]},
        ])
        params = {'x': .15, 'y': .15, 'target': '用户名输入框'}
        with patch('verified_interaction.time.sleep'):
            self.guard.pointer('click', params, self.frame)
        self.assertAlmostEqual(params['x'], .25)
        self.assertAlmostEqual(params['y'], .25)
        self.assertEqual(self.guard.inspect.call_count, 3)
        self.vnc.click.assert_called_once()

    def test_search_once_request_blocks_second_query_click(self):
        self.agent.task = '搜索一次，然后退出登录'
        self.guard._one_shot_search_sent = True
        self.guard.inspect = MagicMock()
        with self.assertRaises(ObservationRequired):
            self.guard.pointer('click', {'x': .15, 'y': .15, 'target': '搜索按钮'}, self.frame)
        self.vnc.click.assert_not_called()
        self.guard.inspect.assert_not_called()

    def test_unverified_login_input_blocks_mouse_submit_too(self):
        self.guard.pending = {'verified': False, 'field': 'password'}
        with self.assertRaises(ObservationRequired):
            self.guard.pointer('click', {'x': .15, 'y': .15, 'target': '登录按钮'}, self.frame)
        self.vnc.click.assert_not_called()

    def test_os_search_is_not_subject_to_a_business_search_whitelist(self):
        self.guard.inspect = MagicMock(return_value={
            'target_visible': True, 'inside': True, 'allowed': False,
            'bbox': [.1, .1, .8, .3], 'evidence': 'Windows search field',
        })
        with patch('verified_interaction.time.sleep'):
            self.guard.pointer('click', {'x': .2, 'y': .2, 'target': 'Windows 搜索框'}, self.frame)
        self.vnc.click.assert_called_once()

    def test_completion_combines_historical_query_and_final_logout(self):
        self.agent.history = [{'step': 3, 'action': 'click', 'params': {'target': 'search'}, 'executed': True,
                               'verification': [{'kind': 'pointer_after', 'screenshot': 'fake.png'}]}]
        self.agent.task_plan = []
        self.guard.inspect = MagicMock(side_effect=[
            {'state': 'query results, both filters selected', 'evidence': ['visible labels and count']},
            {'passed': True, 'evidence': ['final login page after logout']},
        ])
        opened = MagicMock()
        opened.__enter__.return_value.copy.return_value = self.frame
        with patch('verified_interaction.Image.open', return_value=opened):
            self.guard.verify_completion()
        prompt = self.guard.inspect.call_args_list[-1].args[1]
        self.assertIn('query results, both filters selected', prompt)
        self.assertIn('登录页正是正确的最终状态', prompt)
