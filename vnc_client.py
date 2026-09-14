"""
VNC 客户端封装 - 用于截图、鼠标点击、键盘输入等操作
基于 vncdotool
"""
from __future__ import annotations

import ctypes
import math
import os
import re
import time
from ctypes import wintypes

from PIL import Image
from vncdotool import api

from input_method import InputMethodController, UnicodeInputUnsupported


class VNCClient:
    """VNC 客户端，封装截图和键鼠操作"""

    KEYSTROKE_INTERVAL = 0.06
    POINTER_EVENT_INTERVAL = 0.02
    # Give browser-local scroll containers one UI turn to acquire hover after
    # the pointer is moved. Without this gap, the following wheel packets can
    # be dispatched to the previously hovered control even though RFB order is
    # correct (observed with Edge dropdown overlays).
    SCROLL_HOVER_SETTLE = 0.12
    SCROLL_EVENT_INTERVAL = 0.05
    MAX_POINTER_COORDINATE = 65535  # RFB PointerEvent 的 x/y 是 CARD16
    MAX_POINTER_DURATION_MS = 10_000
    MAX_POINTER_STEPS = 100
    MAX_SCROLL_TICKS = 20
    POINTER_BUTTONS = {
        "left": 1,
        "middle": 2,
        "right": 3,
    }
    # 部分 Windows VNC Server 不会把 ASCII keysym 自动还原为
    # Shift+基础键（已有截图中 ':' 被输入成了 ';'），因此显式发送组合键。
    SHIFTED_ASCII_KEYS = {
        "~": "`",
        "!": "1",
        "@": "2",
        "#": "3",
        "$": "4",
        "%": "5",
        "^": "6",
        "&": "7",
        "*": "8",
        "(": "9",
        ")": "0",
        "_": "-",
        "+": "=",
        "{": "[",
        "}": "]",
        "|": "\\",
        ":": ";",
        '"': "'",
        "<": ",",
        ">": ".",
        "?": "/",
    }

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5901,
        password: str = "123456",
        user: str = "",
        system: str = "win",
        local_sendinput: bool | None = None,
        key_interval: float | None = None,
    ):
        self.host = str(host or "localhost")
        self.port = int(port)
        self.password = str(password or "")
        self.user = str(user or "")
        self.system = self._normalize_system(system)
        if local_sendinput is None:
            local_sendinput = os.getenv("RPA_LOCAL_SENDINPUT", "0").strip().lower() in {
                "1", "true", "yes", "on",
            }
        self.local_sendinput = bool(local_sendinput)
        self.key_interval = (
            self.KEYSTROKE_INTERVAL if key_interval is None else float(key_interval)
        )
        if self.key_interval < 0:
            raise ValueError("key_interval 不能为负数")
        self._client = None
        # RFB 没有通用的“读取当前指针位置”接口；只追踪本实例已成功发送的位置。
        self._pointer_position: tuple[int, int] | None = None
        self.ime = InputMethodController(self)

    @staticmethod
    def _normalize_system(system: str) -> str:
        value = str(system or "win").lower().strip()
        value = {
            "windows": "win",
            "win32": "win",
            "ubuntu": "linux",
            "darwin": "mac",
            "macos": "mac",
        }.get(value, value)
        if value not in {"win", "linux", "mac"}:
            raise ValueError(f"不支持的操作系统: {system!r}")
        return value

    def connect(self):
        """连接 VNC 服务器"""
        if self.user:
            self._client = api.connect(f"{self.host}::{self.port}", password=self.password, username=self.user)
        else:
            self._client = api.connect(f"{self.host}::{self.port}", password=self.password)
        self._client.timeout = 10
        self._pointer_position = None

    def disconnect(self):
        """断开连接"""
        if self._client:
            self._client.disconnect()
            self._client = None
        self._pointer_position = None

    @classmethod
    def _validate_pointer_coordinate(cls, value, name: str) -> int:
        """校验 RFB 像素坐标；拒绝布尔值、非有限值和隐式截断。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} 必须是非负整数像素坐标")
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError(f"{name} 超出可表示范围") from exc
        if not math.isfinite(number):
            raise ValueError(f"{name} 必须是有限数值")
        if not number.is_integer():
            raise ValueError(f"{name} 必须是整数像素坐标，不能隐式截断")
        coordinate = int(number)
        if not 0 <= coordinate <= cls.MAX_POINTER_COORDINATE:
            raise ValueError(
                f"{name} 必须在 0~{cls.MAX_POINTER_COORDINATE} 范围内"
            )
        return coordinate

    @classmethod
    def _validate_pointer_point(cls, x, y, prefix: str = "") -> tuple[int, int]:
        label = f"{prefix}_" if prefix else ""
        return (
            cls._validate_pointer_coordinate(x, f"{label}x"),
            cls._validate_pointer_coordinate(y, f"{label}y"),
        )

    @classmethod
    def _validate_duration_ms(cls, duration_ms) -> float:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
            raise ValueError("duration_ms 必须是非负有限数值")
        try:
            value = float(duration_ms)
        except OverflowError as exc:
            raise ValueError("duration_ms 超出可表示范围") from exc
        if not math.isfinite(value) or not 0 <= value <= cls.MAX_POINTER_DURATION_MS:
            raise ValueError(
                f"duration_ms 必须在 0~{cls.MAX_POINTER_DURATION_MS} 范围内"
            )
        return value

    @classmethod
    def _validate_steps(cls, steps) -> int:
        if isinstance(steps, bool) or not isinstance(steps, int):
            raise ValueError("steps 必须是正整数")
        if not 1 <= steps <= cls.MAX_POINTER_STEPS:
            raise ValueError(f"steps 必须在 1~{cls.MAX_POINTER_STEPS} 范围内")
        return steps

    @classmethod
    def _validate_pointer_button(cls, button) -> int:
        if isinstance(button, str):
            value = cls.POINTER_BUTTONS.get(button.lower().strip())
        elif isinstance(button, int) and not isinstance(button, bool):
            value = button if button in cls.POINTER_BUTTONS.values() else None
        else:
            value = None
        if value is None:
            allowed = ", ".join(cls.POINTER_BUTTONS)
            raise ValueError(f"不支持的鼠标按钮 {button!r}；仅支持 {allowed}")
        return value

    def _send_pointer_move(self, x: int, y: int) -> None:
        """发送一次已校验的指针移动，并只在成功后更新本地位置。"""
        self._client.mouseMove(x, y)
        self._pointer_position = (x, y)

    def _move_between(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        duration_ms: float,
        steps: int,
    ) -> None:
        """从 start 向 end 发送固定步数的线性插值移动（包含终点）。"""
        delay = duration_ms / steps / 1000.0
        start_x, start_y = start
        end_x, end_y = end
        for index in range(1, steps + 1):
            ratio = index / steps
            x = round(start_x + (end_x - start_x) * ratio)
            y = round(start_y + (end_y - start_y) * ratio)
            self._send_pointer_move(x, y)
            if delay:
                time.sleep(delay)

    def screenshot(self) -> Image.Image:
        """截取当前屏幕，返回 PIL Image"""
        if not self._client:
            self.connect()
        # 首帧竞态防护：新连接后首帧可能尚未到达（纯黑），检测到则重试刷新
        img = None
        for _ in range(5):
            self._client.refreshScreen()
            img = self._client.screen.copy()
            # 纯黑判定：RGB 三通道 extrema 最大值均为 0
            if any(max_v > 0 for _, max_v in img.convert("RGB").getextrema()):
                return img
            time.sleep(0.3)
        return img

    def click(self, x: int, y: int, button: int | str = 1):
        """
        鼠标点击
        button: 1/left=左键, 2/middle=中键, 3/right=右键
        """
        point = self._validate_pointer_point(x, y)
        button_number = self._validate_pointer_button(button)
        if not self._client:
            self.connect()
        self._send_pointer_move(*point)
        time.sleep(0.05)
        try:
            self._client.mouseDown(button_number)
            time.sleep(0.05)
        finally:
            # mouseDown 可能在远端已生效后本地抛错，因此无条件尝试释放。
            self._client.mouseUp(button_number)
        time.sleep(0.1)

    def double_click(self, x: int, y: int):
        """鼠标双击"""
        self.click(x, y)
        time.sleep(0.1)
        self.click(x, y)

    def type_text(self, text: str):
        """经 VNC 发送 ASCII 键盘事件；先整体校验，避免只输入半段。"""
        if not self._client:
            self.connect()
        value = str(text).replace("\r\n", "\n").replace("\r", "\n")
        keys = []
        for char in value:
            if char == "\n":
                keys.append("enter")
            elif char == "\t":
                keys.append("tab")
            elif ord(char) < 32 or ord(char) == 127:
                raise ValueError(f"不支持的控制字符: U+{ord(char):04X}")
            elif not char.isascii():
                raise UnicodeInputUnsupported(
                    "VNC 逐键通道仅支持可靠的 ASCII 输入，非 ASCII 请使用受控 Unicode 通道"
                )
            elif char == ":":
                # This Windows VNC server maps ':' to the semicolon physical
                # key without adding Shift. Hold Shift across the base key;
                # URL input has already confirmed English via the IME module.
                keys.append(("shift", ";"))
            else:
                keys.append(char)
        for key in keys:
            if isinstance(key, tuple):
                self.key_combination(*key)
            else:
                self._client.keyPress(key)
            time.sleep(self.key_interval)

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        value = str(host or "").strip().lower().strip("[]")
        return value in {"localhost", "127.0.0.1", "::1"}

    def supports_local_unicode_input(self) -> bool:
        """仅显式启用且确认同桌面的回环 Windows 连接可使用 SendInput。"""
        return (
            self.local_sendinput
            and os.name == "nt"
            and self.system == "win"
            and self._is_loopback_host(self.host)
        )

    @staticmethod
    def _send_windows_keyboard_events(
        events_spec: list[tuple[int, int, int]],
    ) -> None:
        """按 ``(virtual_key, unicode_scan_code, flags)`` 发送 Windows 键盘事件。"""
        if os.name != "nt":
            raise RuntimeError("Windows 输入事件仅支持本机 Windows")
        if not events_spec:
            return

        class MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class HardwareInput(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class InputUnion(ctypes.Union):
            _fields_ = [
                ("mi", MouseInput),
                ("ki", KeyboardInput),
                ("hi", HardwareInput),
            ]

        class Input(ctypes.Structure):
            _anonymous_ = ("value",)
            _fields_ = [("type", wintypes.DWORD), ("value", InputUnion)]

        inputs = (Input * len(events_spec))()
        for index, (virtual_key, scan_code, flags) in enumerate(events_spec):
            inputs[index].type = 1  # INPUT_KEYBOARD
            inputs[index].ki = KeyboardInput(
                int(virtual_key), int(scan_code), int(flags), 0, 0
            )

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(Input),
            ctypes.c_int,
        ]
        user32.SendInput.restype = wintypes.UINT
        sent = user32.SendInput(len(inputs), inputs, ctypes.sizeof(Input))
        if sent != len(inputs):
            raise OSError(ctypes.get_last_error(), "Windows SendInput 失败")

    def local_key_combination(self, *keys: str) -> None:
        """用 SendInput 发送本机快捷键，和随后 Unicode 正文保持同一通道。"""
        if not self.supports_local_unicode_input():
            raise RuntimeError("Windows 本机快捷键仅支持显式启用的回环 VNC")
        virtual_keys = {
            "ctrl": 0x11,
            "control": 0x11,
            "shift": 0x10,
            "alt": 0x12,
            "win": 0x5B,
            "super": 0x5B,
            "escape": 0x1B,
            "esc": 0x1B,
            "enter": 0x0D,
            "return": 0x0D,
            "tab": 0x09,
            "backspace": 0x08,
            "bsp": 0x08,
            "delete": 0x2E,
            "space": 0x20,
        }
        resolved = []
        for key in keys:
            normalized = str(key).lower().strip()
            if normalized in virtual_keys:
                resolved.append(virtual_keys[normalized])
            elif len(normalized) == 1 and normalized.isascii() and normalized.isalnum():
                resolved.append(ord(normalized.upper()))
            else:
                raise ValueError(f"本机快捷键不支持: {key!r}")
        if not resolved:
            raise ValueError("本机快捷键不能为空")
        events = [(vk, 0, 0) for vk in resolved[:-1]]
        events.extend([
            (resolved[-1], 0, 0),
            (resolved[-1], 0, 0x0002),  # KEYEVENTF_KEYUP
        ])
        events.extend((vk, 0, 0x0002) for vk in reversed(resolved[:-1]))
        self._send_windows_keyboard_events(events)
        time.sleep(0.05)

    def type_unicode_text(self, text: str) -> None:
        """用 KEYEVENTF_UNICODE 输入 UTF-16 单元，不经过剪贴板。"""
        if not self.supports_local_unicode_input():
            raise RuntimeError("Windows Unicode 直接输入仅支持显式启用的回环 VNC")
        value = str(text).replace("\r\n", "\n").replace("\r", "\n")
        for char in value:
            if (ord(char) < 32 and char not in {"\n", "\t"}) or ord(char) == 127:
                raise ValueError(f"不支持的控制字符: U+{ord(char):04X}")
        for char in value:
            if char == "\n":
                self.local_key_combination("enter")
                continue
            if char == "\t":
                self.local_key_combination("tab")
                continue
            encoded = char.encode("utf-16-le")
            units = [
                int.from_bytes(encoded[index:index + 2], "little")
                for index in range(0, len(encoded), 2)
            ]
            for unit in units:
                self._send_windows_keyboard_events([
                    (0, unit, 0x0004),
                    (0, unit, 0x0004 | 0x0002),
                ])
                time.sleep(self.key_interval)

    # 键名别名映射：统一归一到 vncdotool KEYMAP 中的小写键名
    # 多字符未知键名会触发 ord() 报错，必须在此显式映射或拦截
    KEY_MAP = {
        'enter': 'enter',
        'return': 'enter',
        'escape': 'esc',
        'esc': 'esc',
        'tab': 'tab',
        'backspace': 'bsp',
        'bsp': 'bsp',
        'delete': 'delete',
        'del': 'delete',
        'insert': 'insert',
        'space': 'space',
        'left': 'left',
        'right': 'right',
        'up': 'up',
        'down': 'down',
        'home': 'home',
        'end': 'end',
        'pageup': 'pgup',
        'pgup': 'pgup',
        'pagedown': 'pgdn',
        'pgdn': 'pgdn',
        'ctrl': 'ctrl',
        'control': 'ctrl',
        'alt': 'alt',
        'shift': 'shift',
        'super': 'super',
        'win': 'super',
        'meta': 'super',
        'cmd': 'super',
        'command': 'super',
    }

    def _resolve_key(self, key: str) -> str:
        """将用户键名归一到 vncdotool KEYMAP 键名；未知多字符键名抛错避免 ord() 报错"""
        k = key.lower().strip()
        mapped = self.KEY_MAP.get(k)
        if mapped:
            return mapped
        if re.fullmatch(r"f(?:[1-9]|1[0-2])", k):
            return k
        # 单字符（含字母/数字/符号）直接交给 vncdotool 处理
        if len(k) == 1:
            return k
        raise ValueError(f"未知按键名: '{key}'（需为单字符或 KEY_MAP 中已定义的键名）")

    def press_key(self, key: str):
        """
        按下并释放按键，支持组合键
        - 单键: 'enter', 'escape', 'tab', 'backspace', 'space', 'up' 等
        - 组合键: 'ctrl+n', 'alt+f4', 'shift+tab'（用 '+' 分隔）
        """
        if not self._client:
            self.connect()
        # 含 '+' 视为组合键，交给 key_combination 处理
        if '+' in key:
            parts = [p.strip() for p in key.split('+') if p.strip()]
            if len(parts) < 2:
                raise ValueError(f"组合键格式非法: '{key}'")
            self.key_combination(*parts)
            return
        vnc_key = self._resolve_key(key)
        self._client.keyPress(vnc_key)

    def key_combination(self, *keys):
        """
        组合键，如 key_combination('ctrl', 'c')
        每个 key 都走 _resolve_key 别名归一，避免 ctrl+return 这类别名报错
        """
        if not self._client:
            self.connect()
        resolved = [self._resolve_key(k) for k in keys]
        if not resolved:
            raise ValueError("组合键不能为空")
        pressed = []
        try:
            for k in resolved[:-1]:
                self._client.keyDown(k)
                pressed.append(k)
                time.sleep(0.02)
            self._client.keyPress(resolved[-1])
            time.sleep(0.02)
        finally:
            # 中间失败也要释放修饰键，避免远端持续处于 Ctrl/Alt/Shift 按下状态。
            for k in reversed(pressed):
                self._client.keyUp(k)
                time.sleep(0.02)

    def maximize_window(self, system: str = "win"):
        """在有通用快捷键的平台最大化当前活动窗口。

        Windows 使用系统菜单 ``Alt+Space`` 后按 ``X``，已最大化时重复调用
        仍保持最大化；避免 ``Win+Up`` 在 Windows 11 上触发顶部分屏。
        Linux 使用 Super+Up。macOS 没有通用的“最大化但不进入全屏”
        快捷键，因此安全地跳过。
        """
        normalized = (system or "win").strip().lower()
        if normalized in {"mac", "macos", "darwin"}:
            return
        if not self._client:
            self.connect()
        if normalized in {"linux", "ubuntu"}:
            self.key_combination('super', 'up')
        else:
            # key_combination() 在异常路径也会释放 Alt，避免远端修饰键粘住。
            self.key_combination('alt', 'space')
            time.sleep(0.15)
            self.press_key('x')
        time.sleep(0.8)  # 等最大化动画完成

    def move_mouse(
        self,
        x: int,
        y: int,
        duration_ms: float = 0,
        steps: int = 1,
    ):
        """移动鼠标到指定像素位置。

        当本实例已知上一次指针位置时，``steps`` 大于 1 会在起点和终点间
        做线性插值。新连接无法读取远端现有指针位置，因此第一次调用只发送
        一次终点移动。
        """
        destination = self._validate_pointer_point(x, y)
        checked_duration = self._validate_duration_ms(duration_ms)
        checked_steps = self._validate_steps(steps)
        if not self._client:
            self.connect()
        start = self._pointer_position
        if start is None or start == destination:
            self._send_pointer_move(*destination)
            if checked_duration:
                time.sleep(checked_duration / 1000.0)
            return
        self._move_between(
            start,
            destination,
            duration_ms=checked_duration,
            steps=checked_steps,
        )

    def scroll(
        self,
        x: int,
        y: int,
        amount: int | None = None,
        *,
        direction: int | None = None,
    ):
        """
        在指定位置滚动鼠标滚轮。

        amount: 正数向上、负数向下，一次调用最多 20 格。
        direction: 兼容旧版关键字参数；不能与 amount 同时提供。
        省略 amount/direction 时保持旧行为，默认向上滚动一格。
        """
        point = self._validate_pointer_point(x, y)
        if amount is not None and direction is not None:
            raise ValueError("amount 和兼容参数 direction 不能同时提供")
        if amount is None:
            amount = 1 if direction is None else direction
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ValueError("scroll amount 必须是非零整数")
        if amount == 0:
            raise ValueError("scroll amount 不能为 0")
        if abs(amount) > self.MAX_SCROLL_TICKS:
            raise ValueError(
                f"scroll amount 绝对值不能超过 {self.MAX_SCROLL_TICKS}"
            )
        if not self._client:
            self.connect()
        self._send_pointer_move(*point)
        time.sleep(self.SCROLL_HOVER_SETTLE)
        button = 4 if amount > 0 else 5
        for _ in range(abs(amount)):
            try:
                self._client.mouseDown(button)
                time.sleep(self.SCROLL_EVENT_INTERVAL)
            finally:
                # 每一个 wheel tick 都必须成对释放，避免异常留下按下状态。
                self._client.mouseUp(button)
            time.sleep(self.SCROLL_EVENT_INTERVAL)

    def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: float = 400,
        steps: int = 8,
        button: str | int = "left",
    ):
        """按住鼠标按钮，从起点分段拖到终点，并保证异常时释放按钮。"""
        start = self._validate_pointer_point(start_x, start_y, "start")
        end = self._validate_pointer_point(end_x, end_y, "end")
        checked_duration = self._validate_duration_ms(duration_ms)
        checked_steps = self._validate_steps(steps)
        button_number = self._validate_pointer_button(button)
        if start == end:
            raise ValueError("drag 起点和终点不能相同")

        # 所有参数必须在 connect/鼠标事件等副作用之前完成校验。
        if not self._client:
            self.connect()
        self._send_pointer_move(*start)
        time.sleep(self.POINTER_EVENT_INTERVAL)
        try:
            self._client.mouseDown(button_number)
            time.sleep(self.POINTER_EVENT_INTERVAL)
            self._move_between(
                start,
                end,
                duration_ms=checked_duration,
                steps=checked_steps,
            )
        finally:
            # mouseDown 或任一中间 mouseMove 抛错时仍尝试释放，避免“粘住”按钮。
            self._client.mouseUp(button_number)
        time.sleep(self.POINTER_EVENT_INTERVAL)


if __name__ == "__main__":
    # 测试 VNC 连接
    vnc = VNCClient(host="localhost", port=5901, password="123456")
    try:
        vnc.connect()
        print("VNC 连接成功！")
        img = vnc.screenshot()
        print(f"截图尺寸: {img.size}")
        img.save("vnc_test.png")
        print("截图已保存为 vnc_test.png")
    except Exception as e:
        print(f"VNC 连接失败: {e}")
    finally:
        vnc.disconnect()
