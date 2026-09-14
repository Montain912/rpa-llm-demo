"""Keep a stable visual coordinate frame during a pointer correction.

Only crop geometry is reused, never a model verdict or target coordinates.
"""
from PIL import ImageChops


def stable_pointer_crop(screenshot, target, px, py, previous=None):
    frame = screenshot.convert("RGB")
    if previous and previous["target"] == target and previous["size"] == frame.size:
        left, top, right, bottom = previous["bounds"]
        if left <= px < right and top <= py < bottom:
            region = frame.crop(previous["bounds"])
            if ImageChops.difference(region, previous["image"]).getbbox() is None:
                return previous["bounds"], previous
    radius_x = max(140, round(frame.width * .16))
    radius_y = max(100, round(frame.height * .14))
    bounds = (max(0, px-radius_x), max(0, py-radius_y),
              min(frame.width, px+radius_x+1), min(frame.height, py+radius_y+1))
    return bounds, {"target": target, "size": frame.size, "bounds": bounds,
                    "image": frame.crop(bounds)}
