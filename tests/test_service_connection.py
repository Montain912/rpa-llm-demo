"""Connection tests with mocked VNC/model dependencies; no desktop operations."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PortsTest(unittest.TestCase):
    def config(self, values=None):
        with patch.dict(os.environ, values or {}, clear=True):
            return load_module('tested_config', ROOT / 'service_config.py')

    def test_defaults_keep_three_distinct_ports(self):
        config = self.config()
        self.assertEqual((config.HTTP_PORT, config.WS_PORT, config.VNC_PORT), (5011, 6082, 5901))
        self.assertEqual(config.VNC_HOST, '127.0.0.1')

    def test_environment_overrides(self):
        config = self.config({'RPA_HTTP_PORT': '5200', 'RPA_WS_PORT': '6200',
                              'RPA_VNC_PORT': '5902', 'RPA_VNC_HOST': 'test-desktop'})
        self.assertEqual((config.HTTP_PORT, config.WS_PORT, config.VNC_PORT), (5200, 6200, 5902))
        self.assertEqual(config.VNC_HOST, 'test-desktop')

    def test_invalid_ports_rejected(self):
        for name in ('RPA_HTTP_PORT', 'RPA_WS_PORT', 'RPA_VNC_PORT'):
            for value in ('0', '-1', '65536', 'abc', '5011.5', ''):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.config({name: value})

    def test_http_and_proxy_cannot_share_port(self):
        with self.assertRaises(ValueError):
            self.config({'RPA_HTTP_PORT': '6200', 'RPA_WS_PORT': '6200'})


class ConnectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        replacements = {}
        for name, attribute in [('rpa_agent', 'RPAgent'), ('vnc_client', 'VNCClient'),
                                ('knowledge_loader', 'warmup_cache')]:
            stub = types.ModuleType(name)
            setattr(stub, attribute, MagicMock())
            replacements[name] = stub
        # Never import the real model client or warm up knowledge via external services.
        with patch.dict(sys.modules, replacements):
            cls.backend = load_module('tested_backend', ROOT / 'main.py')

    def setUp(self):
        self.backend._websockify = None
        self.backend.VNC_CONFIG = dict(host='127.0.0.1', port=5901,
                                       user='', password='test-only', system='win')
        self.backend.task_state.update(running=False, paused=False, history=[], result='', log_file='')
        self.temp = tempfile.TemporaryDirectory(prefix='rpa-api-test-')
        self.addCleanup(self.temp.cleanup)

    def output_path(self, directory, filename):
        target = Path(self.temp.name) / directory
        target.mkdir(exist_ok=True)
        return target / filename

    def test_api_reports_configured_proxy_and_target_ports(self):
        runner = MagicMock()
        runner.is_running.return_value = True
        with patch.object(self.backend, '_get_websockify', return_value=runner):
            response = TestClient(self.backend.app).get('/api/novnc-info')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['ws_port'], self.backend.WS_PORT)
        self.assertEqual(response.json()['target_port'], 5901)

    def test_dead_or_failed_proxy_is_restarted_on_next_request(self):
        runner = MagicMock()
        runner.start.side_effect = [RuntimeError('test bind failure'), None]
        with patch.object(self.backend, 'WebsockifyRunner', return_value=runner):
            with self.assertRaises(RuntimeError):
                self.backend._get_websockify()
            self.assertIs(self.backend._get_websockify(), runner)
        self.assertEqual(runner.start.call_count, 2)

    def test_proxy_start_failure_is_reported_in_json(self):
        with patch.object(self.backend, '_get_websockify', side_effect=RuntimeError('port occupied')):
            response = TestClient(self.backend.app).get('/api/novnc-info')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['running'])
        self.assertIn(str(self.backend.WS_PORT), response.json()['error'])

    def test_invalid_vnc_port_rejected_by_both_requests(self):
        for model in (self.backend.VNCConfigRequest, self.backend.ConnectionTestRequest):
            for port in (0, -1, 65536):
                with self.subTest(model=model.__name__, port=port), self.assertRaises(ValidationError):
                    model(port=port)

    def test_omitted_password_is_preserved_when_changing_port(self):
        with patch.object(self.backend, '_reset_screenshot_vnc'), patch.object(self.backend, '_restart_websockify'):
            self.backend.set_vnc_config(self.backend.VNCConfigRequest(port=5902))
        self.assertEqual(self.backend.VNC_CONFIG['port'], 5902)
        self.assertEqual(self.backend.VNC_CONFIG['password'], 'test-only')

    def test_task_api_passes_remote_system_and_completes_with_mock_agent(self):
        agent = MagicMock()
        agent.run_task.return_value = 'local mock complete'

        def start_inline_thread(*, target, args, daemon):
            return types.SimpleNamespace(start=lambda: target(*args))

        with patch.object(self.backend, 'RPAgent', return_value=agent) as constructor, \
             patch.object(self.backend.threading, 'Thread', side_effect=start_inline_thread), \
             patch.object(self.backend, 'runtime_path', side_effect=self.output_path):
            response = self.backend.start_task(self.backend.TaskRequest(task='local mock task'))
        self.assertTrue(response['success'])
        self.assertEqual(constructor.call_args.kwargs['system'], 'win')
        self.assertFalse(self.backend.task_state['running'])
        self.assertEqual(self.backend.task_state['result'], 'local mock complete')
        self.assertEqual(len(list(Path(self.temp.name).glob('logs/*.jsonl'))), 1)

    def test_initialization_failure_clears_task_running_state(self):
        self.backend.task_state['running'] = True
        with patch.object(self.backend, 'RPAgent', side_effect=ValueError('bad system')), \
             patch.object(self.backend, 'runtime_path', side_effect=self.output_path):
            self.backend.run_agent_task('local mock task')
        self.assertFalse(self.backend.task_state['running'])
        self.assertIn('bad system', self.backend.task_state['result'])

    def test_log_failure_does_not_leave_task_running(self):
        self.backend.task_state['running'] = True
        with patch.object(self.backend, 'runtime_path', side_effect=PermissionError('output denied')), \
             patch.object(self.backend, '_write_log', side_effect=PermissionError('output denied')):
            self.backend.run_agent_task('local mock task')
        self.assertFalse(self.backend.task_state['running'])
        self.assertIn('output denied', self.backend.task_state['result'])

    def test_thread_start_failure_returns_error_and_resets_state(self):
        with patch.object(self.backend.threading.Thread, 'start', side_effect=RuntimeError('thread failed')):
            result = self.backend.start_task(self.backend.TaskRequest(task='local mock task'))
        self.assertEqual(result.status_code, 500)
        self.assertFalse(self.backend.task_state['running'])

    def test_connection_test_always_disconnects_on_capture_failure(self):
        vnc = MagicMock()
        vnc.screenshot.side_effect = RuntimeError('capture failed')
        with patch.object(self.backend, 'VNCClient', return_value=vnc):
            result = self.backend.test_connection(self.backend.ConnectionTestRequest())
        self.assertEqual(result.status_code, 500)
        vnc.disconnect.assert_called_once()

    def test_frontend_and_static_paths_work_from_other_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.temp.name)
            self.assertIn('任务指令', self.backend.index())
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
