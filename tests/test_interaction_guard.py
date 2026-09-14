import math
import unittest

from PIL import Image, ImageDraw

from interaction_guard import (
    InteractionGuard,
    InteractionGuardError,
    canonical_action_signature,
    compare_screen_state,
    detect_action_loop,
    quantize_normalized_point,
    target_region_around_point,
    validate_normalized_coordinates,
)


def press(key):
    return {"action": "press", "params": {"key": key}}


class CoordinateGuardTests(unittest.TestCase):
    def test_finite_normalized_coordinates_include_endpoints(self):
        self.assertEqual(
            validate_normalized_coordinates({"x": 0, "y": 1.0}),
            (0.0, 1.0),
        )
        self.assertEqual(quantize_normalized_point(0, 1, grid_size=100), (0, 99))

    def test_nan_infinity_and_out_of_bounds_are_rejected(self):
        invalid_points = [
            (math.nan, 0.5),
            (0.5, math.inf),
            (10**400, 0.5),
            (-0.001, 0.5),
            (0.5, 1.001),
        ]
        for x, y in invalid_points:
            with self.subTest(x=x, y=y), self.assertRaises(InteractionGuardError):
                validate_normalized_coordinates({"x": x, "y": y})

    def test_numeric_strings_booleans_and_missing_pairs_are_rejected(self):
        for params in (
            {"x": "0.5", "y": 0.5},
            {"x": True, "y": 0.5},
            {"x": 0.5},
        ):
            with self.subTest(params=params), self.assertRaises(InteractionGuardError):
                validate_normalized_coordinates(params)


class ActionSignatureTests(unittest.TestCase):
    def test_coordinate_jitter_in_same_grid_cell_has_same_signature(self):
        first = {"action": "click", "params": {"x": 0.501, "y": 0.509}}
        second = {"action": "click", "params": {"x": 0.509, "y": 0.501}}
        third = {"action": "click", "params": {"x": 0.511, "y": 0.501}}
        self.assertEqual(
            canonical_action_signature(first),
            canonical_action_signature(second),
        )
        self.assertNotEqual(
            canonical_action_signature(first),
            canonical_action_signature(third),
        )

    def test_sensitive_values_never_appear_in_signature(self):
        action = {
            "action": "skill_open_webpage",
            "params": {
                "username": "zhouhao",
                "password": "123456",
                "text": "private search phrase",
                "url": "http://internal.example/path?token=very-secret",
                "field_type": "password",
            },
            "thought": "contains private search phrase and 123456",
        }
        signature = canonical_action_signature(action)
        rendered = repr(signature)
        for secret in (
            "zhouhao",
            "123456",
            "private search phrase",
            "internal.example",
            "very-secret",
        ):
            self.assertNotIn(secret, rendered)

        same = canonical_action_signature(action)
        changed = canonical_action_signature(
            {
                **action,
                "params": {**action["params"], "password": "654321"},
            }
        )
        self.assertEqual(signature, same)
        self.assertNotEqual(signature, changed)


class PeriodicLoopTests(unittest.TestCase):
    def test_period_one_is_detected(self):
        result = detect_action_loop([press("enter")], press("enter"))
        self.assertTrue(result.detected)
        self.assertEqual(result.period, 1)
        self.assertEqual(result.repetitions, 2)

    def test_abab_is_detected(self):
        result = detect_action_loop(
            [press("a"), press("b"), press("a")],
            press("b"),
        )
        self.assertTrue(result.detected)
        self.assertEqual(result.period, 2)
        self.assertEqual(result.repetitions, 2)

    def test_abcabc_is_detected(self):
        result = detect_action_loop(
            [press("a"), press("b"), press("c"), press("a"), press("b")],
            press("c"),
        )
        self.assertTrue(result.detected)
        self.assertEqual(result.period, 3)
        self.assertEqual(result.repetitions, 2)

    def test_non_periodic_actions_and_blocked_records_do_not_false_positive(self):
        guard = InteractionGuard()
        history = [
            {**press("a"), "executed": False},
            press("b"),
            press("a"),
        ]
        self.assertFalse(guard.detect_loop(history, press("b")).detected)


class ScreenDifferenceTests(unittest.TestCase):
    def test_small_control_change_is_visible_to_tile_and_target_roi(self):
        before = Image.new("RGB", (1000, 800), "white")
        after = before.copy()
        ImageDraw.Draw(after).rectangle((490, 390, 509, 409), fill=(0, 90, 220))
        target = target_region_around_point(
            0.5,
            0.5,
            before.size,
            radius_ratio=0.025,
        )

        result = compare_screen_state(before, after, target_region=target)

        self.assertFalse(result.full_changed)
        self.assertTrue(result.tile_changed)
        self.assertTrue(result.target_changed)
        self.assertTrue(result.changed)
        self.assertGreater(result.max_tile_score, result.full_score)
        self.assertGreater(result.target_score, result.full_score)
        self.assertIsNotNone(result.max_tile_region)

    def test_identical_images_have_no_change_at_any_scale(self):
        image = Image.new("RGB", (120, 80), "navy")
        result = compare_screen_state(
            image,
            image.copy(),
            target_region=(20, 20, 60, 60),
        )
        self.assertFalse(result.changed)
        self.assertEqual(result.full_score, 0.0)
        self.assertEqual(result.max_tile_score, 0.0)
        self.assertEqual(result.target_score, 0.0)

    def test_geometry_change_is_reported_without_resizing(self):
        result = compare_screen_state(
            Image.new("RGB", (100, 80), "white"),
            Image.new("RGB", (101, 80), "white"),
        )
        self.assertTrue(result.changed)
        self.assertTrue(result.geometry_changed)
        self.assertEqual(result.full_score, 1.0)


if __name__ == "__main__":
    unittest.main()
