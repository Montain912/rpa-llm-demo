"""
RPA 固定流程技能库（macro skills）
每个技能返回一个确定性的动作步骤列表（action dict，格式与 rpa_agent 一致），
agent 可在一个决策步内顺序执行，避免 LLM 在多步微操作间反复截图决策导致的漂移与重复。

坐标约定：click 类的 x/y 为 0~1 归一化比例，由调用方（视觉 LLM）识别后传入。
"""
from __future__ import annotations

import re
import json


def _wait(seconds: float) -> dict:
    return {"action": "wait", "params": {"seconds": seconds}}


def _press(key: str) -> dict:
    return {"action": "press", "params": {"key": key}}


def _maximize() -> dict:
    return {"action": "maximize_window", "params": {}}


def _type(text: str, field_type: str = "auto") -> dict:
    """兼容旧宏的追加输入；field_type 只用于选择安全的输入传输。"""
    return {
        "action": "type",
        "params": {"text": text, "field_type": field_type},
    }


def _input_text(
    text: str,
    field_type: str = "auto",
    replace: bool = True,
) -> dict:
    return {
        "action": "input_text",
        "params": {
            "text": text,
            "field_type": field_type,
            "replace": bool(replace),
        },
    }


def _click(x: float, y: float) -> dict:
    return {"action": "click", "params": {"x": x, "y": y}}


def open_application(application: str, system: str = "win") -> list[dict]:
    """
    技能1：打开应用搜索入口并输入应用名，但不自动回车启动。

    输入后由下一轮截图验收搜索文字和结果，再单独按 Enter；避免输入法错误
    被直接当成成功启动。

    参数:
        application: 要打开的应用名称（如 "edge"、"记事本"、"notepad"、"firefox"）
        system: 操作系统，"win"（默认）/ "linux" / "mac"
    """
    if not application:
        raise ValueError("open_application 缺少 application 参数")

    system = (system or "win").strip().lower()

    if system == "mac":
        # macOS: Cmd+Space 打开聚焦搜索 → 输入应用名 → 截图验收
        return [
            _press("cmd+space"),
            _wait(1),
            _input_text(application, "app_search"),
            _wait(0.5),
        ]

    if system == "linux":
        # Linux(GNOME/Ubuntu): Super 键开活动概览 → 搜索应用名 → 截图验收
        return [
            _press("super"),
            _wait(1),
            _input_text(application, "app_search"),
            _wait(0.5),
        ]

    # Windows: Win 键开开始菜单 → 搜索应用名 → 截图验收
    return [
        _press("win"),
        _wait(1),
        _input_text(application, "app_search"),
        _wait(0.5),
    ]


def open_webpage(system: str = "win",
                 needlogin: bool = False,
                 username: str = "",
                 password: str = "",
                 loginCoordinates: dict | None = None,
                 pdCoordinates: dict | None = None,
                 buttonCoordinates: dict | None = None,
                 stage: str = "username") -> list[dict]:
    """
    技能2：分阶段填写网页登录表单。

    每次只执行 username/password/submit 中的一步。两个输入阶段都返回观察
    循环验收，只有字段确认无误后才进入下一阶段或提交。

    参数:
        system: 操作系统（预留，当前登录流程跨系统一致）
        needlogin: 该网页是否需要登录
        username: 登录用户名
        password: 登录密码
        loginCoordinates: 用户名输入框坐标 {"x": 0~1, "y": 0~1}
        pdCoordinates:    密码输入框坐标   {"x": 0~1, "y": 0~1}
        buttonCoordinates: 登录按钮坐标   {"x": 0~1, "y": 0~1}
    """
    if not needlogin:
        return []

    stage = str(stage or "username").lower().strip()
    if stage == "username":
        if not loginCoordinates or not {"x", "y"} <= loginCoordinates.keys():
            raise ValueError("open_webpage username 阶段缺少 loginCoordinates")
        return [
            _click(loginCoordinates["x"], loginCoordinates["y"]),
            _wait(0.3),
            _input_text("" if username is None else str(username), "username"),
            _wait(0.5),
        ]
    if stage == "password":
        if not pdCoordinates or not {"x", "y"} <= pdCoordinates.keys():
            raise ValueError("open_webpage password 阶段缺少 pdCoordinates")
        return [
            _click(pdCoordinates["x"], pdCoordinates["y"]),
            _wait(0.3),
            _input_text("" if password is None else str(password), "password"),
            _wait(0.5),
        ]
    if stage == "submit":
        if not buttonCoordinates or not {"x", "y"} <= buttonCoordinates.keys():
            raise ValueError("open_webpage submit 阶段缺少 buttonCoordinates")
        return [
            _click(buttonCoordinates["x"], buttonCoordinates["y"]),
            _wait(2),
        ]
    raise ValueError("open_webpage stage 必须是 username/password/submit")


def open_url(url: str, browser: str = "", system: str = "win") -> list[dict]:
    """在已打开的浏览器中聚焦地址栏并输入网址，但不自动按 Enter。

    调用方必须先确认浏览器已在前台。本技能不再使用 Win+R 或终端拼接
    命令，避免 URL 被系统当成文件路径。``browser`` 仅保留作参数校验。
    """
    if not url:
        raise ValueError("open_url 缺少 url 参数")

    system = (system or "win").strip().lower()
    system = {
        "windows": "win", "win32": "win", "darwin": "mac", "macos": "mac",
    }.get(system, system)
    if system not in {"win", "linux", "mac"}:
        raise ValueError(f"不支持的操作系统: {system!r}")

    browser = (browser or "").strip().lower()
    supported = {
        "", "default", "chrome", "google chrome", "firefox",
        "edge", "msedge", "microsoft edge", "safari",
    }
    if browser not in supported:
        raise ValueError(f"不支持的浏览器名称: {browser!r}")

    address_shortcut = "cmd+l" if system == "mac" else "ctrl+l"
    return [
        _maximize(),
        _press("escape"),
        _press(address_shortcut),
        _wait(0.2),
        _input_text(str(url), "url", replace=True),
        _wait(0.5),
    ]


def input_text(
    text: str,
    field_type: str = "auto",
    replace: bool = True,
) -> list[dict]:
    """在当前焦点字段输入一次且不提交，供下一轮截图做结构化验收。"""
    if text is None:
        raise ValueError("input_text 缺少 text 参数")
    return [
        _input_text(str(text), field_type, replace=replace),
        _wait(0.5),
    ]


def input_method(operation: str, candidate: int | None = None) -> list[dict]:
    """把具名输入法操作包装成执行器动作。"""
    if not operation:
        raise ValueError("input_method 缺少 operation 参数")
    return [{
        "action": "ime_operation",
        "params": {"operation": operation, "candidate": candidate},
    }]


def _normalize_subtasks(raw_list: list) -> list[dict]:
    """将原始列表元素归一为标准子任务格式"""
    tasks = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or item.get("task") or item.get("name") or ""
        expected = (item.get("expected_result")
                    or item.get("expectedResult")
                    or item.get("result") or "")
        if not content:
            continue
        tasks.append({
            "content": str(content),
            "completed": False,
            "expected_result": str(expected),
        })
    return tasks


def planning(response: str) -> list[dict]:
    """
    工具4：任务规划解析。
    将视觉模型输出的规划文本解析为结构化子任务列表。
    每个子任务: {"content": str, "completed": False, "expected_result": str}
    解析失败时返回空列表，调用方据此回退到基础动作模式。
    """
    # 1. 去除 markdown 围栏后尝试直接解析
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)

    def _try_parse(text):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
            # 模型可能包裹在 {"tasks": [...]} 里
            if isinstance(data, dict) and "tasks" in data:
                return data["tasks"]
        except json.JSONDecodeError:
            pass
        return None

    result = _try_parse(cleaned)
    if result is not None:
        return _normalize_subtasks(result)

    # 2. 括号深度扫描提取 JSON 数组（[...]），跳过字符串字面量内的方括号
    start = response.find("[")
    if start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(response)):
            ch = response[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        result = _try_parse(response[start:i + 1])
                        if result is not None:
                            return _normalize_subtasks(result)
                        break

    return []

