import math
import sys
import types
import unittest
from unittest.mock import patch


# 单独运行本文件时不需要安装或连接真实 vncdotool。
if "vncdotool" not in sys.modules:
    vncdotool_stub = types.ModuleType("vncdotool")
    vncdotool_stub.api = types.SimpleNamespace(connect=lambda *_args, **_kwargs: None)
    sys.modules["vncdotool"] = vncdotool_stub

from vnc_client import VNCClient


class FakeProtocol:
    def __init__(self, *, fail_move_call=None, fail_down_call=None):
        self.events = []
        self.fail_move_call = fail_move_call
        self.fail_down_call = fail_down_call
        self.move_calls = 0
        self.down_calls = 0

    def mouseMove(self, x, y):
        self.move_calls += 1
        self.events.append(("move", (x, y)))
        if self.move_calls == self.fail_move_call:
            raise RuntimeError("injected mouseMove failure")

    def mouseDown(self, button):
        self.down_calls += 1
        self.events.append(("down", button))
        if self.down_calls == self.fail_down_call:
            raise RuntimeError("injected mouseDown failure")

    def mouseUp(self, button):
        self.events.append(("up", button))


def make_vnc(protocol=None):
    vnc = VNCClient(key_interval=0)
    vnc._client = protocol or FakeProtocol()
    return vnc


class PointerActionTests(unittest.TestCase):
    def setUp(self):
        sleep_patcher = patch("vnc_client.time.sleep", return_value=None)
        self.sleep_mock = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_move_mouse_tracks_position_and_interpolates(self):
        vnc = make_vnc()

        # 新连接不知道远端指针起点，第一次只能直接移动到终点。
        vnc.move_mouse(0, 0, duration_ms=50, steps=5)
        self.assertEqual(vnc._client.events, [("move", (0, 0))])

        vnc._client.events.clear()
        vnc.move_mouse(10, 6, duration_ms=60, steps=2)

        self.assertEqual(
            vnc._client.events,
            [("move", (5, 3)), ("move", (10, 6))],
        )
        self.assertEqual(vnc._pointer_position, (10, 6))

    def test_scroll_emits_bounded_multi_tick_sequence_at_target(self):
        vnc = make_vnc()

        vnc.scroll(10, 20, -3)

        self.assertEqual(
            vnc._client.events,
            [
                ("move", (10, 20)),
                ("down", 5), ("up", 5),
                ("down", 5), ("up", 5),
                ("down", 5), ("up", 5),
            ],
        )
        self.assertEqual(
            self.sleep_mock.call_args_list[0].args[0],
            VNCClient.SCROLL_HOVER_SETTLE,
        )

    def test_scroll_keeps_old_default_and_direction_keyword(self):
        default_vnc = make_vnc()
        default_vnc.scroll(1, 2)
        self.assertEqual(
            default_vnc._client.events,
            [("move", (1, 2)), ("down", 4), ("up", 4)],
        )

        legacy_vnc = make_vnc()
        legacy_vnc.scroll(3, 4, direction=-2)
        self.assertEqual(
            legacy_vnc._client.events,
            [
                ("move", (3, 4)),
                ("down", 5), ("up", 5),
                ("down", 5), ("up", 5),
            ],
        )

    def test_scroll_releases_wheel_button_when_a_tick_fails(self):
        protocol = FakeProtocol(fail_down_call=2)
        vnc = make_vnc(protocol)

        with self.assertRaisesRegex(RuntimeError, "mouseDown"):
            vnc.scroll(10, 20, -3)

        self.assertEqual(protocol.events[-2:], [("down", 5), ("up", 5)])

    def test_drag_interpolates_and_releases_button(self):
        vnc = make_vnc()

        vnc.drag(0, 0, 8, 4, duration_ms=0, steps=4)

        self.assertEqual(
            vnc._client.events,
            [
                ("move", (0, 0)),
                ("down", 1),
                ("move", (2, 1)),
                ("move", (4, 2)),
                ("move", (6, 3)),
                ("move", (8, 4)),
                ("up", 1),
            ],
        )
        self.assertEqual(vnc._pointer_position, (8, 4))

    def test_drag_releases_button_when_intermediate_move_fails(self):
        protocol = FakeProtocol(fail_move_call=3)
        vnc = make_vnc(protocol)

        with self.assertRaisesRegex(RuntimeError, "mouseMove"):
            vnc.drag(0, 0, 8, 4, duration_ms=0, steps=4)

        self.assertEqual(protocol.events[-1], ("up", 1))
        self.assertIn(("down", 1), protocol.events)

    def test_drag_releases_button_when_mouse_down_reports_failure(self):
        protocol = FakeProtocol(fail_down_call=1)
        vnc = make_vnc(protocol)

        with self.assertRaisesRegex(RuntimeError, "mouseDown"):
            vnc.drag(0, 0, 8, 4, duration_ms=0, steps=4)

        self.assertEqual(protocol.events[-2:], [("down", 1), ("up", 1)])

    def test_valid_coordinate_protocol_boundaries_are_preserved(self):
        vnc = make_vnc()

        vnc.move_mouse(0, VNCClient.MAX_POINTER_COORDINATE)

        self.assertEqual(
            vnc._client.events,
            [("move", (0, VNCClient.MAX_POINTER_COORDINATE))],
        )

    def test_invalid_parameters_fail_before_any_pointer_event(self):
        invalid_calls = [
            ("negative coordinate", lambda v: v.move_mouse(-1, 0)),
            ("fractional coordinate", lambda v: v.move_mouse(1.5, 0)),
            ("boolean coordinate", lambda v: v.move_mouse(True, 0)),
            ("NaN coordinate", lambda v: v.move_mouse(math.nan, 0)),
            ("infinite coordinate", lambda v: v.move_mouse(math.inf, 0)),
            ("huge coordinate", lambda v: v.move_mouse(10**400, 0)),
            (
                "protocol coordinate overflow",
                lambda v: v.move_mouse(VNCClient.MAX_POINTER_COORDINATE + 1, 0),
            ),
            ("negative duration", lambda v: v.move_mouse(1, 2, duration_ms=-1)),
            ("infinite duration", lambda v: v.move_mouse(1, 2, duration_ms=math.inf)),
            ("huge duration", lambda v: v.move_mouse(1, 2, duration_ms=10**400)),
            (
                "duration overflow",
                lambda v: v.move_mouse(
                    1, 2, duration_ms=VNCClient.MAX_POINTER_DURATION_MS + 1
                ),
            ),
            ("zero steps", lambda v: v.move_mouse(1, 2, steps=0)),
            ("boolean steps", lambda v: v.move_mouse(1, 2, steps=True)),
            ("too many steps", lambda v: v.move_mouse(1, 2, steps=101)),
            ("zero scroll", lambda v: v.scroll(1, 2, 0)),
            ("fractional scroll", lambda v: v.scroll(1, 2, 1.5)),
            ("boolean scroll", lambda v: v.scroll(1, 2, True)),
            ("NaN scroll point", lambda v: v.scroll(math.nan, 2, 1)),
            ("too much scroll", lambda v: v.scroll(1, 2, 21)),
            (
                "two scroll parameters",
                lambda v: v.scroll(1, 2, 1, direction=-1),
            ),
            ("same drag point", lambda v: v.drag(1, 2, 1, 2)),
            ("invalid drag end", lambda v: v.drag(1, 2, math.inf, 3)),
            ("invalid drag duration", lambda v: v.drag(1, 2, 3, 4, duration_ms=-1)),
            ("invalid drag steps", lambda v: v.drag(1, 2, 3, 4, steps=0)),
            ("invalid drag button", lambda v: v.drag(1, 2, 3, 4, button="side")),
        ]

        for label, invoke in invalid_calls:
            with self.subTest(label=label):
                protocol = FakeProtocol()
                vnc = make_vnc(protocol)
                with self.assertRaises(ValueError):
                    invoke(vnc)
                self.assertEqual(protocol.events, [])


if __name__ == "__main__":
    unittest.main()
