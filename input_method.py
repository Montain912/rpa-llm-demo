"""字段感知的输入法控制与无剪贴板文本输入。"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any


class UnicodeInputUnsupported(RuntimeError):
    """当前输入通道无法可靠发送非 ASCII 文本。"""


@dataclass(frozen=True)
class InputMethodConfig:
    """集中保存常用输入法快捷键，避免业务技能散落系统细节。"""

    toggle_chinese_english: str = "shift"
    cycle_keyboard_layout: str = "win+space"
    cancel_composition: str = "esc"
    commit_composition: str = "space"

    @classmethod
    def for_system(cls, system: str) -> "InputMethodConfig":
        normalized = str(system or "win").lower().strip()
        if normalized in {"mac", "macos", "darwin"}:
            return cls(
                toggle_chinese_english="ctrl+space",
                cycle_keyboard_layout="ctrl+space",
            )
        if normalized in {"linux", "ubuntu"}:
            return cls(
                toggle_chinese_english="super+space",
                cycle_keyboard_layout="super+space",
            )
        return cls()


class InputMethodController:
    """规范化字段值，并为输入、替换和输入法恢复选择一致的键盘通道。"""

    ASCII_FIELDS = {"url", "email", "number"}
    NORMALIZED_FIELDS = {"url", "email", "number"}
    OPERATION_ALIASES = {
        "toggle_language": "toggle_language",
        "toggle": "toggle_language",
        "中英文切换": "toggle_language",
        "cycle_layout": "cycle_layout",
        "switch_layout": "cycle_layout",
        "切换输入法": "cycle_layout",
        "cancel": "cancel",
        "cancel_composition": "cancel",
        "取消候选": "cancel",
        "commit": "commit",
        "commit_composition": "commit",
        "提交候选": "commit",
        "select_candidate": "select_candidate",
        "选择候选": "select_candidate",
    }

    def __init__(
        self,
        vnc_client: Any,
        config: InputMethodConfig | None = None,
    ) -> None:
        self.vnc = vnc_client
        self.config = config or InputMethodConfig.for_system(
            getattr(vnc_client, "system", "win")
        )

    @staticmethod
    def expected_mode(text: str, field_type: str = "auto") -> str:
        """返回期望语言模式；它不是对远端当前输入法状态的检测。"""
        field = str(field_type or "auto").lower().strip()
        value = str(text)
        if field in InputMethodController.ASCII_FIELDS:
            return "english"
        has_cjk = bool(re.search(r"[\u3400-\u9fff]", value))
        has_ascii_word = bool(re.search(r"[A-Za-z0-9]", value))
        if has_cjk and has_ascii_word:
            return "mixed"
        if has_cjk:
            return "chinese"
        return "english"

    @staticmethod
    def normalize_text(text: Any, field_type: str = "auto") -> str:
        """只规范明确的 ASCII 数据字段；密码、命令、代码和普通文本保持原值。"""
        if text is None:
            raise ValueError("输入文本不能为 None")
        value = str(text)
        field = str(field_type or "auto").lower().strip()
        if field in InputMethodController.NORMALIZED_FIELDS:
            value = unicodedata.normalize("NFKC", value)
            if field == "url":
                value = value.strip().strip("\"'").replace("。", ".")
            value = re.sub(r"\s+", "", value)
        if field == "url":
            if not value:
                raise ValueError("URL 不能为空")
            has_scheme = bool(
                re.match(r"^[a-z][a-z0-9+.-]*://", value, flags=re.IGNORECASE)
                or re.match(
                    r"^(?:about|edge|chrome|file|data):",
                    value,
                    flags=re.IGNORECASE,
                )
            )
            if value.lower().startswith("www."):
                value = "https://" + value
            elif not has_scheme:
                local_target = re.match(
                    r"^(?:localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\])"
                    r"(?::|/|$)",
                    value,
                    flags=re.IGNORECASE,
                )
                value = ("http://" if local_target else "https://") + value
        return value

    def toggle_language(self) -> None:
        self.vnc.press_key(self.config.toggle_chinese_english)

    def ensure_english(self, read_mode) -> None:
        """Prepare the focused field without typing a probe or guessing Shift."""
        mode = read_mode()
        if mode == "english":
            return
        if mode != "chinese":
            raise RuntimeError("无法确认当前输入法中英文状态，尚未输入字段内容")
        self.cancel()
        self.toggle_language()
        if read_mode() != "english":
            raise RuntimeError("切换后未确认英文输入状态，尚未输入字段内容")

    def cycle_layout(self) -> None:
        self.vnc.press_key(self.config.cycle_keyboard_layout)

    def cancel(self) -> None:
        self.vnc.press_key(self.config.cancel_composition)

    def commit(self) -> None:
        self.vnc.press_key(self.config.commit_composition)

    def select_candidate(self, index: int) -> None:
        candidate = int(index)
        if not 1 <= candidate <= 9:
            raise ValueError("输入法候选序号必须是 1~9")
        self.vnc.press_key(str(candidate))

    @classmethod
    def normalize_operation(cls, operation: str) -> str:
        op = str(operation or "").lower().strip()
        canonical = cls.OPERATION_ALIASES.get(op)
        if not canonical:
            raise ValueError(f"未知输入法操作: {operation!r}")
        return canonical

    def perform(self, operation: str, candidate: int | None = None) -> str:
        """按稳定操作名执行输入法动作。"""
        op = self.normalize_operation(operation)
        if op == "toggle_language":
            self.toggle_language()
        elif op == "cycle_layout":
            self.cycle_layout()
        elif op == "cancel":
            self.cancel()
        elif op == "commit":
            self.commit()
        elif op == "select_candidate":
            if candidate is None:
                raise ValueError("select_candidate 缺少 candidate 参数")
            self.select_candidate(candidate)
        return op

    @staticmethod
    def _contains_non_ascii(value: str) -> bool:
        return any(ord(char) > 0x7F for char in value)

    def type_value(
        self,
        text: Any,
        field_type: str = "auto",
        *,
        replace: bool = False,
        switch_language: bool = False,
        cancel_composition: bool = False,
    ) -> tuple[str, str]:
        """
        输入字段值并返回 ``(实际输入值, 期望模式)``。

        ASCII 始终沿用 VNC 键盘通道。非 ASCII 只在部署方明确启用同桌面
        Windows SendInput 时发送；不使用仅支持 Latin-1 的传统 VNC 剪贴板。
        """
        value = self.normalize_text(text, field_type)
        mode = self.expected_mode(value, field_type)
        non_ascii = self._contains_non_ascii(value)
        for char in value.replace("\r\n", "\n").replace("\r", "\n"):
            if (ord(char) < 32 and char not in {"\n", "\t"}) or ord(char) == 127:
                raise ValueError(f"不支持的控制字符: U+{ord(char):04X}")
        use_local_unicode = bool(
            non_ascii
            and getattr(self.vnc, "supports_local_unicode_input", lambda: False)()
        )

        # 必须在任何按键副作用之前确认传输能力，避免先清空字段再发现无法输入。
        if non_ascii and not use_local_unicode:
            raise UnicodeInputUnsupported(
                "当前 VNC 通道不支持可靠的非 ASCII 输入；"
                "仅在确认 VNC 与 RPA 共享同一 Windows 交互桌面后设置 "
                "RPA_LOCAL_SENDINPUT=1"
            )

        if replace and cancel_composition:
            if use_local_unicode:
                self.vnc.local_key_combination("escape")
            else:
                self.cancel()
                if field_type == "url":
                    # Match the proven URL recovery order: cancel composition,
                    # restore address-bar focus, then switch and replace text.
                    self.vnc.key_combination("ctrl", "l")
        # SendInput 的 Unicode 路径绕过输入法状态；仅远程键盘恢复才允许切换。
        if switch_language and not use_local_unicode:
            self.toggle_language()
        if replace:
            select_modifier = (
                "cmd"
                if str(getattr(self.vnc, "system", "win")).lower() in {"mac", "macos", "darwin"}
                else "ctrl"
            )
            if use_local_unicode:
                self.vnc.local_key_combination(select_modifier, "a")
            else:
                self.vnc.key_combination(select_modifier, "a")

        if value:
            if use_local_unicode:
                self.vnc.type_unicode_text(value)
            else:
                self.vnc.type_text(value)
        elif replace:
            # 全选后必须真正删除；只留下选区并不等于清空字段。
            if use_local_unicode:
                self.vnc.local_key_combination("backspace")
            else:
                self.vnc.press_key("backspace")
        return value, mode
