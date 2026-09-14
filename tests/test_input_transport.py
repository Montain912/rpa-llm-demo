"""Input regression checks without a VNC server or model API."""
import unittest
from unittest.mock import MagicMock, patch

from input_method import InputMethodController, UnicodeInputUnsupported
from tools import open_application, open_url, open_webpage
from vnc_client import VNCClient


class InputTransportTests(unittest.TestCase):
    def setUp(self):
        self.vnc = VNCClient(system='win', key_interval=0, local_sendinput=False)
        self.vnc._client = MagicMock()
        self.ime = self.vnc.ime

    def test_url_colon_holds_shift_and_releases_it(self):
        with patch('vnc_client.time.sleep'):
            self.vnc.type_text('http://host:7010/')
        calls = self.vnc._client.method_calls
        down = [i for i, call in enumerate(calls) if call[0] == 'keyDown']
        self.assertEqual(len(down), 2)
        for i in down:
            self.assertEqual(calls[i].args, ('shift',))
            self.assertEqual(calls[i + 1].args, (';',))
            self.assertEqual(calls[i + 2].args, ('shift',))
            self.assertEqual(calls[i + 2][0], 'keyUp')

    def test_modifier_is_released_even_when_key_send_fails(self):
        self.vnc._client.keyPress.side_effect = OSError('transport')
        with patch('vnc_client.time.sleep'), self.assertRaises(OSError):
            self.vnc.type_text(':')
        self.vnc._client.keyUp.assert_called_once_with('shift')

    def test_unsupported_text_is_rejected_before_field_is_cleared(self):
        with self.assertRaises(UnicodeInputUnsupported):
            self.ime.type_value('中文', 'text', replace=True)
        self.assertEqual(self.vnc._client.method_calls, [])

    def test_unknown_ime_does_not_guess_a_language_toggle(self):
        with self.assertRaises(RuntimeError):
            self.ime.ensure_english(lambda: 'unknown')
        self.assertEqual(self.vnc._client.method_calls, [])

    def test_english_ime_needs_no_toggle(self):
        self.ime.ensure_english(lambda: 'english')
        self.assertEqual(self.vnc._client.method_calls, [])

    def test_chinese_ime_toggles_once_and_confirms(self):
        reader = MagicMock(side_effect=['chinese', 'english'])
        with patch.object(self.vnc, 'press_key') as press:
            self.ime.ensure_english(reader)
        self.assertEqual([c.args[0] for c in press.call_args_list], ['shift'])
        self.assertEqual(reader.call_count, 2)

    def test_url_skill_is_field_input_without_terminal_or_submit(self):
        steps = open_url('http://example.test:7010/', browser='firefox', system='linux')
        self.assertEqual(steps, [{'action': 'type', 'params': {
            'text': 'http://example.test:7010/', 'field_type': 'url'}}])
        self.assertEqual(open_application('edge')[0]['params']['key'], 'win+s')

    def test_login_macro_preserves_field_and_target_metadata(self):
        point = {'x': .5, 'y': .5}
        steps = open_webpage(needlogin=True, username='user', password='secret',
                             loginCoordinates=point, pdCoordinates=point, buttonCoordinates=point)
        self.assertEqual([s['params']['field_type'] for s in steps if s['action'] == 'type'],
                         ['username', 'password'])
        self.assertTrue(all(s['params']['target'] for s in steps if s['action'] == 'click'))


if __name__ == '__main__':
    unittest.main()
