import base64
import importlib.util
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return types.SimpleNamespace(
            usage=None,
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content="{}")
            )],
        )


class LLMClientImageTransportTests(unittest.TestCase):
    def _load_module(self):
        completions = _FakeCompletions()
        fake_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions)
        )
        openai_stub = types.ModuleType("openai")
        openai_stub.OpenAI = lambda **_kwargs: fake_client
        module_path = Path(__file__).resolve().parents[1] / "llm_client.py"
        spec = importlib.util.spec_from_file_location(
            "llm_client_transport_under_test", module_path
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"openai": openai_stub}):
            spec.loader.exec_module(module)
        return module, completions

    def test_png_mode_uses_lossless_payload_and_matching_mime_type(self):
        module, completions = self._load_module()
        source = Image.new("RGB", (3, 2), (17, 31, 47))
        with tempfile.TemporaryDirectory() as temp_dir:
            module.LLM_OUTPUT_DIR = Path(temp_dir)
            module.chat_vision("read exact text", source, image_format="PNG")

        data_url = completions.kwargs["messages"][1]["content"][1]["image_url"]["url"]
        prefix, encoded = data_url.split(",", 1)
        self.assertEqual(prefix, "data:image/png;base64")
        raw = base64.b64decode(encoded)
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
        decoded = Image.open(io.BytesIO(raw)).convert("RGB")
        self.assertEqual(list(decoded.getdata()), list(source.getdata()))
        record = module.token_tracker.records[-1]
        self.assertEqual(record["purpose"], "test_png_mode_uses_lossless_payload_and_matching_mime_type")
        self.assertEqual(record["image_size"], [3, 2])
        self.assertGreaterEqual(record["api_seconds"], 0)
        self.assertGreater(record["text_chars"], 0)


if __name__ == "__main__":
    unittest.main()
