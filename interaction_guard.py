"""Deterministic guards for GUI interaction planning and visual progress.

The helpers in this module deliberately do not execute input.  They validate
normalized coordinates, build privacy-preserving behavioral signatures, detect
short periodic action loops, and measure pixel changes at three spatial
scales.  Pixel differences are evidence of visual change only; they never prove
that a business task succeeded.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from PIL import Image, ImageChops, ImageStat


ImageSource: TypeAlias = str | Path | Image.Image
ActionSignature: TypeAlias = tuple[object, ...]


class InteractionGuardError(ValueError):
    """A deterministic validation failure with a stable reason code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class LoopDetectionResult:
    """Description of a periodic suffix in an action-signature sequence."""

    detected: bool
    period: int | None = None
    repetitions: int = 0
    cycle: tuple[ActionSignature, ...] = ()


@dataclass(frozen=True, slots=True)
class ScreenDiffResult:
    """Full-screen, tiled, and target-region pixel-change evidence."""

    changed: bool
    geometry_changed: bool
    before_size: tuple[int, int]
    after_size: tuple[int, int]
    full_score: float
    full_threshold: float
    full_changed: bool
    max_tile_score: float
    tile_threshold: float
    tile_changed: bool
    max_tile_region: tuple[int, int, int, int] | None
    target_score: float | None
    target_threshold: float
    target_changed: bool
    target_region: tuple[int, int, int, int] | None
    reason: str


_COORDINATE_ACTIONS = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "move",
        "mouse_move",
        "move_mouse",
    }
)
_COORDINATE_PAIRS = (
    ("x", "y"),
    ("start_x", "start_y"),
    ("end_x", "end_y"),
    ("from_x", "from_y"),
    ("to_x", "to_y"),
)
_IGNORED_SIGNATURE_KEYS = frozenset(
    {
        "thought",
        "reason",
        "description",
        "expected_result",
        "result",
    }
)
_SAFE_ENUM_KEYS = frozenset(
    {
        "action",
        "browser",
        "button",
        "cause",
        "direction",
        "field_type",
        "key",
        "operation",
        "stage",
        "system",
    }
)
_SENSITIVE_KEY_PARTS = (
    "text",
    "password",
    "passwd",
    "secret",
    "token",
    "username",
    "account",
    "url",
    "query",
    "command",
    "application",
    "value",
)


def _strict_finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InteractionGuardError(
            "INVALID_COORDINATE_TYPE",
            f"{label} must be an int or float, got {type(value).__name__}",
        )
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise InteractionGuardError(
            "NON_FINITE_COORDINATE", f"{label} cannot be represented as a finite float"
        ) from exc
    if not math.isfinite(number):
        raise InteractionGuardError(
            "NON_FINITE_COORDINATE", f"{label} must be finite, got {value!r}"
        )
    return number


def validate_normalized_point(x: object, y: object) -> tuple[float, float]:
    """Return finite normalized coordinates or raise ``InteractionGuardError``.

    Numeric strings and booleans are intentionally rejected.  The caller must
    pass actual JSON numbers in the inclusive range ``[0, 1]``.
    """

    checked_x = _strict_finite_number(x, "x")
    checked_y = _strict_finite_number(y, "y")
    if not 0.0 <= checked_x <= 1.0 or not 0.0 <= checked_y <= 1.0:
        raise InteractionGuardError(
            "COORDINATE_OUT_OF_BOUNDS",
            f"x/y must be in [0, 1], got ({checked_x!r}, {checked_y!r})",
        )
    return checked_x, checked_y


def validate_normalized_coordinates(
    params: Mapping[str, Any],
    *,
    x_key: str = "x",
    y_key: str = "y",
) -> tuple[float, float]:
    """Validate a named normalized coordinate pair in an action mapping."""

    if not isinstance(params, Mapping):
        raise InteractionGuardError(
            "INVALID_PARAMS", f"params must be a mapping, got {type(params).__name__}"
        )
    if x_key not in params or y_key not in params:
        raise InteractionGuardError(
            "MISSING_COORDINATE", f"both {x_key!r} and {y_key!r} are required"
        )
    return validate_normalized_point(params[x_key], params[y_key])


def quantize_normalized_point(
    x: object,
    y: object,
    *,
    grid_size: int = 100,
) -> tuple[int, int]:
    """Map a normalized point into a stable square grid cell."""

    if isinstance(grid_size, bool) or type(grid_size) is not int or grid_size < 1:
        raise ValueError("grid_size must be a positive integer")
    checked_x, checked_y = validate_normalized_point(x, y)
    return (
        min(int(checked_x * grid_size), grid_size - 1),
        min(int(checked_y * grid_size), grid_size - 1),
    )


def _redacted_value(value: Any) -> tuple[str, int, str]:
    """Return a stable opaque token without retaining the original value."""

    encoded = repr(value).encode("utf-8", errors="replace")
    digest = hashlib.blake2s(encoded, digest_size=12, person=b"rpasig").hexdigest()
    return "redacted", len(encoded), digest


def _key_is_sensitive(key: str) -> bool:
    lowered = key.casefold().replace("-", "_")
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _canonical_value(value: Any, key: str, grid_size: int) -> object:
    if _key_is_sensitive(key):
        return _redacted_value(value)
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InteractionGuardError(
                "NON_FINITE_PARAMETER", f"parameter {key!r} must be finite"
            )
        return value
    if isinstance(value, str):
        if key.casefold() in _SAFE_ENUM_KEYS:
            return value.casefold().strip()
        return _redacted_value(value)
    if isinstance(value, Mapping):
        return _canonical_mapping(value, grid_size)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return tuple(_canonical_value(item, key, grid_size) for item in value)
    return _redacted_value(value)


def _canonical_mapping(
    params: Mapping[str, Any], grid_size: int
) -> tuple[tuple[str, object], ...]:
    consumed: set[str] = set()
    items: list[tuple[str, object]] = []

    normalized_keys = {str(key): value for key, value in params.items()}
    for x_key, y_key in _COORDINATE_PAIRS:
        has_x = x_key in normalized_keys
        has_y = y_key in normalized_keys
        if has_x != has_y:
            raise InteractionGuardError(
                "MISSING_COORDINATE",
                f"coordinate pair {x_key!r}/{y_key!r} must be supplied together",
            )
        if has_x:
            cell = quantize_normalized_point(
                normalized_keys[x_key], normalized_keys[y_key], grid_size=grid_size
            )
            items.append((f"{x_key}/{y_key}_cell", cell))
            consumed.update((x_key, y_key))

    for key in sorted(normalized_keys):
        if key in consumed or key.casefold() in _IGNORED_SIGNATURE_KEYS:
            continue
        items.append((key, _canonical_value(normalized_keys[key], key, grid_size)))
    return tuple(items)


def canonical_action_signature(
    action: Mapping[str, Any],
    *,
    coordinate_grid: int = 100,
) -> ActionSignature:
    """Build a behavioral action signature safe to retain in loop history.

    Coordinates are represented by grid cells so insignificant visual jitter
    does not evade loop detection.  Free-form strings and known input/credential
    fields are represented only by length and an opaque digest.  Model prose is
    excluded because changing a thought must not evade a behavioral loop guard.
    """

    if (
        isinstance(coordinate_grid, bool)
        or type(coordinate_grid) is not int
        or coordinate_grid < 1
    ):
        raise ValueError("coordinate_grid must be a positive integer")
    if not isinstance(action, Mapping):
        raise InteractionGuardError(
            "INVALID_ACTION", f"action must be a mapping, got {type(action).__name__}"
        )
    name = action.get("action")
    if not isinstance(name, str) or not name.strip():
        raise InteractionGuardError("INVALID_ACTION", "action name must be non-empty")
    normalized_name = name.casefold().strip()
    params = action.get("params", {})
    if not isinstance(params, Mapping):
        raise InteractionGuardError(
            "INVALID_PARAMS", f"params must be a mapping, got {type(params).__name__}"
        )
    if normalized_name in _COORDINATE_ACTIONS:
        validate_normalized_coordinates(params)
    return normalized_name, _canonical_mapping(params, coordinate_grid)


def detect_signature_loop(
    signatures: Sequence[ActionSignature],
    *,
    max_period: int = 3,
    min_repetitions: int = 2,
) -> LoopDetectionResult:
    """Detect a repeated suffix with period 1, 2, or 3 by default."""

    if isinstance(max_period, bool) or type(max_period) is not int or not 1 <= max_period <= 3:
        raise ValueError("max_period must be an integer in [1, 3]")
    if (
        isinstance(min_repetitions, bool)
        or type(min_repetitions) is not int
        or min_repetitions < 2
    ):
        raise ValueError("min_repetitions must be an integer >= 2")

    sequence = tuple(signatures)
    for period in range(1, max_period + 1):
        required = period * min_repetitions
        if len(sequence) < required:
            continue
        cycle = sequence[-period:]
        repetitions = 1
        cursor = len(sequence) - 2 * period
        while cursor >= 0 and sequence[cursor : cursor + period] == cycle:
            repetitions += 1
            cursor -= period
        if repetitions >= min_repetitions:
            return LoopDetectionResult(
                detected=True,
                period=period,
                repetitions=repetitions,
                cycle=cycle,
            )
    return LoopDetectionResult(detected=False)


def detect_action_loop(
    history: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any] | None = None,
    *,
    coordinate_grid: int = 100,
    max_period: int = 3,
    min_repetitions: int = 2,
    include_unexecuted: bool = False,
) -> LoopDetectionResult:
    """Canonicalize action records and detect a short periodic loop.

    Records explicitly marked ``executed=False`` are ignored by default: an
    action blocked by policy did not mutate the GUI and must not prevent its
    first legitimate execution later.
    """

    signatures: list[ActionSignature] = []
    for index, item in enumerate(history):
        if not isinstance(item, Mapping):
            raise InteractionGuardError(
                "INVALID_ACTION_HISTORY",
                f"history item {index} must be a mapping, got {type(item).__name__}",
            )
        if include_unexecuted or item.get("executed") is not False:
            signatures.append(
                canonical_action_signature(item, coordinate_grid=coordinate_grid)
            )
    if candidate is not None:
        signatures.append(
            canonical_action_signature(candidate, coordinate_grid=coordinate_grid)
        )
    return detect_signature_loop(
        signatures,
        max_period=max_period,
        min_repetitions=min_repetitions,
    )


def _validate_threshold(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number in [0, 1]")
    threshold = float(value)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError(f"{label} must be finite and in [0, 1]")
    return threshold


def _load_rgb(source: ImageSource) -> Image.Image:
    if isinstance(source, Image.Image):
        return source.convert("RGB")
    path = Path(source)
    with Image.open(path) as opened:
        return opened.convert("RGB").copy()


def _normalized_mean_difference(delta: Image.Image) -> float:
    means = ImageStat.Stat(delta).mean
    return float(sum(means) / (len(means) * 255.0)) if means else 0.0


def _validate_region(
    region: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int]:
    if (
        not isinstance(region, tuple)
        or len(region) != 4
        or any(type(value) is not int for value in region)
    ):
        raise TypeError("target_region must be a four-integer tuple")
    x1, y1, x2, y2 = region
    if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1 or x2 > width or y2 > height:
        raise ValueError(
            f"target_region {region!r} is outside screenshot bounds {width}x{height}"
        )
    return region


def target_region_around_point(
    x: object,
    y: object,
    image_size: tuple[int, int],
    *,
    radius_ratio: float = 0.04,
    minimum_radius: int = 12,
) -> tuple[int, int, int, int]:
    """Create a clipped pixel ROI around a normalized interaction point."""

    checked_x, checked_y = validate_normalized_point(x, y)
    if (
        not isinstance(image_size, tuple)
        or len(image_size) != 2
        or any(type(value) is not int or value <= 0 for value in image_size)
    ):
        raise ValueError("image_size must be a pair of positive integers")
    ratio = _validate_threshold(radius_ratio, "radius_ratio")
    if isinstance(minimum_radius, bool) or type(minimum_radius) is not int or minimum_radius < 1:
        raise ValueError("minimum_radius must be a positive integer")

    width, height = image_size
    px = min(int(checked_x * width), width - 1)
    py = min(int(checked_y * height), height - 1)
    radius_x = max(minimum_radius, int(width * ratio))
    radius_y = max(minimum_radius, int(height * ratio))
    return (
        max(0, px - radius_x),
        max(0, py - radius_y),
        min(width, px + radius_x + 1),
        min(height, py + radius_y + 1),
    )


def compare_screen_state(
    before: ImageSource,
    after: ImageSource,
    *,
    full_threshold: float = 0.002,
    tile_threshold: float = 0.01,
    target_region: tuple[int, int, int, int] | None = None,
    target_threshold: float = 0.02,
    tile_columns: int = 8,
    tile_rows: int = 8,
) -> ScreenDiffResult:
    """Measure visual change at full-screen, tile, and optional target scales."""

    checked_full_threshold = _validate_threshold(full_threshold, "full_threshold")
    checked_tile_threshold = _validate_threshold(tile_threshold, "tile_threshold")
    checked_target_threshold = _validate_threshold(target_threshold, "target_threshold")
    if (
        isinstance(tile_columns, bool)
        or isinstance(tile_rows, bool)
        or type(tile_columns) is not int
        or type(tile_rows) is not int
        or not 1 <= tile_columns <= 64
        or not 1 <= tile_rows <= 64
    ):
        raise ValueError("tile_columns and tile_rows must be integers in [1, 64]")

    before_image = _load_rgb(before)
    after_image = _load_rgb(after)
    before_size = before_image.size
    after_size = after_image.size
    if before_size != after_size:
        return ScreenDiffResult(
            changed=True,
            geometry_changed=True,
            before_size=before_size,
            after_size=after_size,
            full_score=1.0,
            full_threshold=checked_full_threshold,
            full_changed=True,
            max_tile_score=1.0,
            tile_threshold=checked_tile_threshold,
            tile_changed=True,
            max_tile_region=None,
            target_score=1.0 if target_region is not None else None,
            target_threshold=checked_target_threshold,
            target_changed=target_region is not None,
            target_region=target_region,
            reason="screenshot geometry changed; images were not resized",
        )

    width, height = before_size
    checked_region = (
        _validate_region(target_region, width, height)
        if target_region is not None
        else None
    )
    delta = ImageChops.difference(before_image, after_image)
    full_score = _normalized_mean_difference(delta)
    full_changed = full_score > checked_full_threshold

    columns = min(tile_columns, width)
    rows = min(tile_rows, height)
    tile_width = math.ceil(width / columns)
    tile_height = math.ceil(height / rows)

    # Evaluate both the ordinary grid and a half-tile-shifted grid.  Without
    # overlap, a checkbox centered on a grid intersection is split across four
    # tiles and its signal can disappear from every tile average.
    x_starts = _overlapping_tile_starts(width, tile_width)
    y_starts = _overlapping_tile_starts(height, tile_height)
    max_tile_score = 0.0
    max_tile_region: tuple[int, int, int, int] | None = None
    for y1 in y_starts:
        y2 = min(height, y1 + tile_height)
        for x1 in x_starts:
            x2 = min(width, x1 + tile_width)
            region = (x1, y1, x2, y2)
            score = _normalized_mean_difference(delta.crop(region))
            if max_tile_region is None or score > max_tile_score:
                max_tile_score = score
                max_tile_region = region
    tile_changed = max_tile_score > checked_tile_threshold

    target_score = None
    target_changed = False
    if checked_region is not None:
        target_score = _normalized_mean_difference(delta.crop(checked_region))
        target_changed = target_score > checked_target_threshold

    changed = full_changed or tile_changed or target_changed
    signals = [
        label
        for label, active in (
            ("full", full_changed),
            ("tile", tile_changed),
            ("target", target_changed),
        )
        if active
    ]
    reason = (
        "visual change detected by " + ", ".join(signals)
        if signals
        else "no visual difference exceeded its configured threshold"
    )
    return ScreenDiffResult(
        changed=changed,
        geometry_changed=False,
        before_size=before_size,
        after_size=after_size,
        full_score=full_score,
        full_threshold=checked_full_threshold,
        full_changed=full_changed,
        max_tile_score=max_tile_score,
        tile_threshold=checked_tile_threshold,
        tile_changed=tile_changed,
        max_tile_region=max_tile_region,
        target_score=target_score,
        target_threshold=checked_target_threshold,
        target_changed=target_changed,
        target_region=checked_region,
        reason=reason,
    )


def _overlapping_tile_starts(length: int, tile_length: int) -> tuple[int, ...]:
    """Return full-size base and half-shifted tile starts without duplicates."""

    final_start = max(0, length - tile_length)
    starts = set(range(0, final_start + 1, tile_length))
    half = max(1, tile_length // 2)
    starts.update(range(half, final_start + 1, tile_length))
    starts.add(final_start)
    return tuple(sorted(starts))


class InteractionGuard:
    """Small configuration wrapper around the module's pure guard functions."""

    def __init__(
        self,
        *,
        coordinate_grid: int = 100,
        max_period: int = 3,
        min_repetitions: int = 2,
    ) -> None:
        if (
            isinstance(coordinate_grid, bool)
            or type(coordinate_grid) is not int
            or coordinate_grid < 1
        ):
            raise ValueError("coordinate_grid must be a positive integer")
        if isinstance(max_period, bool) or type(max_period) is not int or not 1 <= max_period <= 3:
            raise ValueError("max_period must be an integer in [1, 3]")
        if (
            isinstance(min_repetitions, bool)
            or type(min_repetitions) is not int
            or min_repetitions < 2
        ):
            raise ValueError("min_repetitions must be an integer >= 2")
        self.coordinate_grid = coordinate_grid
        self.max_period = max_period
        self.min_repetitions = min_repetitions

    def signature(self, action: Mapping[str, Any]) -> ActionSignature:
        return canonical_action_signature(action, coordinate_grid=self.coordinate_grid)

    def detect_loop(
        self,
        history: Sequence[Mapping[str, Any]],
        candidate: Mapping[str, Any] | None = None,
    ) -> LoopDetectionResult:
        return detect_action_loop(
            history,
            candidate,
            coordinate_grid=self.coordinate_grid,
            max_period=self.max_period,
            min_repetitions=self.min_repetitions,
        )


__all__ = [
    "ActionSignature",
    "ImageSource",
    "InteractionGuard",
    "InteractionGuardError",
    "LoopDetectionResult",
    "ScreenDiffResult",
    "canonical_action_signature",
    "compare_screen_state",
    "detect_action_loop",
    "detect_signature_loop",
    "quantize_normalized_point",
    "target_region_around_point",
    "validate_normalized_coordinates",
    "validate_normalized_point",
]
