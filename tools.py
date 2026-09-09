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


def _type(text: str) -> dict:
    return {"action": "type", "params": {"text": text}}


def _click(x: float, y: float) -> dict:
    return {"action": "click", "params": {"x": x, "y": y}}


def open_application(application: str, system: str = "win") -> list[dict]:
    """
    技能1：打开应用。
    通过开始菜单/活动概览/聚焦搜索应用名并启动，再最大化窗口。纯键盘流程，不依赖坐标。
    返回动作步骤列表。

    参数:
        application: 要打开的应用名称（如 "edge"、"记事本"、"notepad"、"firefox"）
        system: 操作系统，"win"（默认）/ "linux" / "mac"
    """
    if not application:
        return []

    system = (system or "win").strip().lower()

    if system == "mac":
        # macOS: Cmd+Space 打开聚焦搜索 → 输入应用名 → 回车
        return [
            _press("cmd+space"),
            _wait(1),
            _type(application),
            _wait(1),
            _press("enter"),
            _wait(2),
        ]

    if system == "linux":
        # Linux(GNOME/Ubuntu): Super 键开活动概览 → 搜索应用名 → 回车启动 → 最大化
        return [
            _press("super"),
            _wait(1),
            _type(application),
            _wait(1),
            _press("enter"),
            _wait(2),
            _press("super+up"),   # 最大化窗口
            _wait(1),
        ]

    # Windows: Win 键开开始菜单 → 搜索应用名 → 回车启动 → 最大化
    return [
        _press("win"),
        _wait(1),
        _type(application),
        _wait(1),
        _press("enter"),
        _wait(2),
        _press("win+up"),   # 最大化窗口
        _wait(1),
    ]


def open_webpage(system: str = "win",
                 needlogin: bool = False,
                 username: str = "",
                 password: str = "",
                 loginCoordinates: dict | None = None,
                 pdCoordinates: dict | None = None,
                 buttonCoordinates: dict | None = None) -> list[dict]:
    """
    技能2：网页登录固定流程。
    假定浏览器已打开并停在登录页，按视觉 LLM 提供的三个坐标顺序完成填表登录。
    needlogin=False 时无需任何操作，返回空列表。

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

    # 三个坐标缺一不可，否则无法走固定流程
    for name, coord in (("loginCoordinates", loginCoordinates),
                        ("pdCoordinates", pdCoordinates),
                        ("buttonCoordinates", buttonCoordinates)):
        if not coord or "x" not in coord or "y" not in coord:
            raise ValueError(f"open_webpage 缺少必要坐标参数: {name}")

    return [
        _click(loginCoordinates["x"], loginCoordinates["y"]),  # 聚焦用户名框
        _wait(0.5),
        _type(username),
        _click(pdCoordinates["x"], pdCoordinates["y"]),        # 聚焦密码框
        _wait(0.3),
        _type(password),
        _click(buttonCoordinates["x"], buttonCoordinates["y"]),  # 点登录
        _wait(2),
    ]


def open_url(url: str, browser: str = "", system: str = "win") -> list[dict]:
    """
    技能3：命令行直接打开网址（比视觉点击快且准）。
    通过系统"运行/终端"用命令启动浏览器访问 URL，再最大化窗口。

    Agent 仅有 VNC 键盘通道（无 SSH），无法事先探测浏览器是否安装：
    - browser 留空：直接输入 URL，走系统默认浏览器（最稳，等价于自动选用已存在的默认浏览器）
    - browser="chrome"/"firefox"：按系统用对应命令调用；若未安装系统会弹错误框，
      Agent 下步截图可见并可回退为默认浏览器

    参数:
        url: 要打开的完整网址（含 http(s)://）
        browser: 浏览器名，"chrome"/"firefox"/""（默认浏览器）
        system: "win"（默认）/ "linux"
    """
    if not url:
        return []

    system = (system or "win").strip().lower()
    browser = (browser or "").strip().lower()
    print("----------------",browser)
    print("打开网址:", url)
    if system == "linux":
        # 开终端 → 用命令启动浏览器（后台 &），xdg-open 走默认
        if browser == "chrome":
            launch_cmd = f'google-chrome "{url}" &'
        elif browser == "firefox":
            launch_cmd = f'firefox "{url}" &'
        else:
            # launch_cmd = f'xdg-open "{url}" &'
            launch_cmd = f'firefox "{url}" &'
        return [
            _press("ctrl+alt+t"),   # 打开终端
            _wait(1.5),
            _type(launch_cmd),
            _press("enter"),
            _wait(3),               # 等浏览器与页面加载
            _press("super+up"),     # 最大化浏览器窗口
            _wait(1),
        ]

    # Windows：Win+R 打开运行窗口，命令启动浏览器
    if browser == "chrome":
        launch_cmd = f'chrome "{url}"'
    elif browser == "firefox":
        launch_cmd = f'firefox "{url}"'
    else:
        # 直接输入网址，走系统默认浏览器（最稳定，不依赖浏览器名）
        launch_cmd = url

    print("启动浏览器命令:", launch_cmd)
    return [
        _press("win+r"),        # 打开运行对话框
        _wait(1),
        _type(launch_cmd),
        _press("enter"),
        _wait(3),                # 等浏览器启动与页面加载
        _press("win+up"),        # 最大化窗口
        _wait(1),
    ]


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

