import unittest
from PIL import Image
from pointer_context import stable_pointer_crop


class StablePointerCropTests(unittest.TestCase):
    def test_correction_on_unchanged_frame_keeps_origin(self):
        frame = Image.new("RGB", (1280, 800), "white")
        bounds, context = stable_pointer_crop(frame, "option", 824, 354)
        corrected, retained = stable_pointer_crop(frame.copy(), "option", 835, 364, context)
        self.assertEqual(corrected, bounds)
        self.assertIs(retained, context)

    def test_changed_pixels_reset_origin(self):
        frame = Image.new("RGB", (1280, 800), "white")
        bounds, context = stable_pointer_crop(frame, "option", 824, 354)
        frame.putpixel((830, 360), (0, 0, 0))
        new, retained = stable_pointer_crop(frame, "option", 835, 364, context)
        self.assertNotEqual(new, bounds)
        self.assertIsNot(retained, context)

    def test_different_target_or_size_or_point_outside_resets(self):
        frame = Image.new("RGB", (1280, 800), "white")
        _, context = stable_pointer_crop(frame, "option", 824, 354)
        for image, target, x, y in ((frame, "other", 835, 364),
                (Image.new("RGB", (1200, 800), "white"), "option", 835, 364),
                (frame, "option", 10, 10)):
            with self.subTest(target=target, size=image.size, x=x):
                _, retained = stable_pointer_crop(image, target, x, y, context)
                self.assertIsNot(retained, context)


if __name__ == "__main__":
    unittest.main()
