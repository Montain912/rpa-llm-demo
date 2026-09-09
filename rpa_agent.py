"""
RPA Agent 核心逻辑 - 观察-决策-执行循环
使用视觉大模型分析截图，决定下一步操作，通过 VNC 执行
"""
from __future__ import annotations

import re
import json
import time
from PIL import Image
from llm_client import chat_vision, chat_text
from vnc_client import VNCClient
from knowledge_loader import get_summary
from tools import open_application, open_webpage, open_url, planning as parse_planning
import random

SYSTEMTYPE = "LINUX"
# 归一化系统标识（技能层与快捷键分支统一使用小写：windows/linux/mac）
SYSTEM = SYSTEMTYPE.strip().lower()

# 各系统专属快捷键与启动方式提示，注入提示词，防止模型跨系统误用按键
_OS_SHORTCUT_HINTS = {
    "windows": (
        "- 打开应用：按 Win 键开开始菜单，输入应用名后回车；Win+R 打开运行对话框，可直接输入程序名或网址启动\n"
        "- 显示桌面：win+d；最大化当前窗口：win+up"
    ),
    "linux": (
        "- 打开应用：按 Super 键（即 Win 键）打开活动概览，输入应用名后回车\n"
        "- 打开终端：Ctrl+Alt+T；终端中启动浏览器：firefox \"网址\" &、google-chrome \"网址\" &，或 xdg-open \"网址\" &\n"
        "- 显示桌面：super+d；最大化当前窗口：super+up\n"
        "- 严禁使用 Win+R 运行框、start 命令等 Windows 专属操作（Linux 上不存在，按下无任何效果）"
    ),
}
OS_SHORTCUT_HINT = _OS_SHORTCUT_HINTS.get(SYSTEM, _OS_SHORTCUT_HINTS[SYSTEM])

SYSTEM_PROMPT = """你是一个 GUI 自动化智能体（Computer Use Agent）。你的任务是通过观察屏幕截图，分析当前界面状态，并决定下一步操作来完成用户的任务。

你可以执行以下操作类型：
1. click(x, y) - 点击屏幕上的指定坐标（左键）
2. double_click(x, y) - 双击指定坐标
3. right_click(x, y) - 右键点击指定坐标
4. type(text) - 在当前焦点位置输入文本
5. press(key) - 按下按键；支持单键（enter, escape, tab, backspace, space, up, down, left, right 等）与组合键（ctrl+n, alt+f4, shift+tab 等，用 '+' 分隔）
6. scroll(direction) - 滚动鼠标滚轮（up 或 down）
7. wait(seconds) - 等待一段时间（用于页面加载等，seconds 为 0.5~3 的数字）
8. done() - 任务完成，结束循环
9. skill_open_app(application) - 【固定流程技能】打开指定应用。会自动通过开始菜单搜索并启动、最大化窗口，无需你分步点击。例：{"action": "skill_open_app", "params": {"application": "edge"}}
10. skill_open_webpage(needlogin, username, password, loginCoordinates, pdCoordinates, buttonCoordinates) - 【固定流程技能】在已打开的登录网页上自动完成填表登录。当你能在截图中看到登录表单时使用：先识别出用户名输入框、密码输入框、登录按钮三者的归一化坐标，一次性传入，系统会自动按"点用户名框→输入账号→点密码框→输入密码→点登录"的固定流程执行，无需你分步操作。
    例：{"action": "skill_open_webpage", "params": {"needlogin": true, "username": "admin", "password": "123456", "loginCoordinates": {"x": 0.5, "y": 0.24}, "pdCoordinates": {"x": 0.5, "y": 0.30}, "buttonCoordinates": {"x": 0.46, "y": 0.40}}}
    若网页无需登录（needlogin=false），不要使用此技能。
11. skill_open_url(url, browser) - 【固定流程技能】用系统命令行直接启动浏览器并打开指定网址，比视觉点击地址栏更快更准。访问网址时优先使用。browser 可留空（走系统默认浏览器，最稳），或指定 "chrome"/"firefox"。
    例：{"action": "skill_open_url", "params": {"url": "https://example.com/login", "browser": ""}}
    该技能只负责打开网页并等待加载；页面打开后若需登录，再调用 skill_open_webpage。
12. planning() - 【任务规划工具】当固定流程（打开应用/打开网址/登录）已完成，进入需要逐步操作的复杂任务阶段时调用。调用后系统会用当前截图分析页面并生成结构化子任务列表，后续逐步执行。无需参数。
    例：{"action": "planning", "params": {}, "thought": "登录已完成，需要规划测试任务"}
13. subtask_done(result, passed) - 【子任务完成标记】当当前子任务的操作已完成并达到预期时调用，标记该子任务完成并进入下一个子任务。result 为实际操作结果描述，passed 为是否达到预期（true/false）。所有子任务完成后输出 done()。
    例：{"action": "subtask_done", "params": {"result": "点击后显示了AGENT列表", "passed": true}, "thought": "预期结果已达成"}

坐标规则（非常重要）：
- x 和 y 必须是 0 到 1 之间的相对比例值（小数），表示目标点在截图宽度和高度中的位置比例
- 例如：屏幕正中间是 x=0.5, y=0.5；左上角附近是 x=0.05, y=0.1；右下角"开始"按钮在 x=0.95, y=0.97
- 严禁输出像素值（如 500、300），严禁输出大于 1 的数值
- 比例坐标与分辨率完全无关：系统会自动把你给的比例映射到真实屏幕像素。无论截图是 1920 还是 2560 宽，同一目标的比例值都一样
- 严禁做任何分辨率换算、缩放系数乘法、DPI 补偿。你只需目测目标在当前截图中的相对位置比例，直接报告该比例即可
- 点击/双击操作的坐标点必须定位到**待点击区域的几何正中心**：
  - 按钮：按钮边框矩形的正中心（按钮内含文字/图标，点整块区域的中心）
  - 下拉列表项/菜单项：高亮行整条区域的正中心（不要点文字左边缘或右侧滚动条）
  - 复选框/单选框：控件正方形框体的正中心
  - 表格单元格/行：该格或该行的正中心
  - 图标/缩略图：图形矩形区域的正中心
  - ⚠️ 严禁故意点左边缘、右边缘、文字首字、滚动条、关闭按钮等"看起来在附近"的位置，必须瞄准整块可点击区域的正中心

输出格式要求：
- 请严格以 JSON 格式输出，不要输出其他多余文字
- JSON 格式：{"action": "操作类型", "params": {...参数}, "thought": "你的思考过程"}

示例：
- 点击按钮：{"action": "click", "params": {"x": 0.52, "y": 0.31}, "thought": "我看到了'开始'按钮，点击它"}
- 输入文字：{"action": "type", "params": {"text": "hello world"}, "thought": "在输入框中输入文字"}
- 按回车：{"action": "press", "params": {"key": "enter"}, "thought": "按回车键确认"}
- 等待加载：{"action": "wait", "params": {"seconds": 2}, "thought": "页面正在加载，等待"}
- 任务完成：{"action": "done", "params": {}, "thought": "任务已完成"}

注意事项：
- 当前操作系统已在文末明确给出，严禁猜测；所有快捷键与启动方式必须与该系统匹配，禁止使用其他系统的专属操作
- 打开应用后系统会自动最大化窗口，无需你手动最大化；若发现窗口未最大化，可用文末给出的本系统最大化快捷键
- 当识别到当前任务正在使用的应用窗口不是最大化时，要先最大化该应用，使用文末给出的本系统最大化快捷键
- 若是当前桌面的应用与任务相关，要先强制最小化或关闭该应用，再执行任务操作
- 坐标比例要对应截图中实际元素的位置，要准确，直接报告目标相对位置，不要自行做分辨率/缩放换算
- 每次只执行一个操作，逐步完成任务
- 不要重复执行上一步完全相同的操作，除非上一步明显未生效；卡住时换一种方式
- 如果不确定，先 wait() 观察变化
- 如果任务已完成，输出 done()
- 如果外部要求暂停，输出 pause()
- 如果外部要求终止，输出 stop()
- 注意输入的网址中英文混合，要正确解析，不要用中文的'：'
- 执行自动化测试时，要确保截图中包含所有必要元素，避免元素缺失导致操作失败
- 执行自动化测试时，要先分析截图中的元素，结合任务生成参数或外部要求，确定操作目标，再执行操作
- 访问网页时先打开另一个浏览器窗口，不要直接使用当前的窗口
- 重要：截图中可能出现 IDE 编辑器、浏览器控制台、命令行终端等开发工具界面，请完全忽略它们，寻找远程桌面的正常桌面环境（如桌面图标、任务栏、开始菜单等）。如果看到系统网页端界面，说明窗口未最小化，应先最小化或关闭该窗口。

- 【下拉列表交互规则，极其重要】遇到下拉选择框（select/dropdown），优先使用键盘而不是鼠标逐个点：
  - 先用鼠标点击下拉框头部展开列表
  - 展开后**不要逐个点列表项**（视觉坐标估算误差会反复摇摆），改用键盘：
    · 输入选项文字的前几个字（如"云南"），系统通常会自动匹配；或
    · 用上下方向键 ↑↓ 定位到目标项，然后按 Enter 确认
  - 同一页面如果有多个下拉框，**必须先在大脑里区分每个下拉框的选项范围**，避免跨下拉框串项（例如："场景渠道"的选项和"标题模型"的选项不能混选）
  - 用鼠标点列表项时，严格执行前面"坐标规则"中的"正中心"要求——瞄准高亮行整条区域的正中心

【本系统操作要点】
""" + OS_SHORTCUT_HINT + """
当前操作系统为：
""" + SYSTEMTYPE


SYSTEM_PROMPT_PLANNING = """
你是一个 GUI 自动化智能体（Computer Use Agent）的任务规划部件，你的任务是根据用户输入的任务描述，规划出完成任务的步骤序列。

示例1：
    任务：在163邮箱发送邮件，用户为duyx912@163.com，密码为q1w2e3,收件人为123@qq.com，主题为测试邮件，内容为这是一封测试邮件
    步骤：
    1. 当前在浏览器中，新开一个页面并进入该页面
    2. 打开163邮箱登录页面
    3. 输入用户名duyx912@163.com
    4. 输入密码q1w2e3
    5. 点击"登录"按钮
    6. 选择"发送邮件"选项
    7. 输入收件人邮箱123@qq.com
    8. 输入主题测试邮件
    9. 输入内容这是一封测试邮件
    10. 点击"发送"按钮
    11. 等待邮箱发送完成
    12. 按回车键确认

示例2：
    任务：登陆页面http://172.20.194.189:8801/views/login.html， 用户admin，密码123456 测试工作流agent的各个功能
    步骤：
    1. 当前在浏览器中，新开一个页面并进入该页面
    2. 打开http://172.20.194.189:8801/views/login.html登录页面
    3. 输入用户名admin
    4. 输入密码123456
    5. 点击"登录"按钮
    6. 进入待测试页面
    7. 根据知识库，规划测试任务，生成测试用例
    8. 执行测试用例，验证功能是否正常
注意事项：
- 确保在执行任务前，当前页面与任务无关的话先最小化，避免与任务相关应用冲突
- 所有操作可以使用键盘操作就用键盘，避免使用鼠标操作找不到ui元素
- 你只是给出任务的步骤序列，不要给出任何操作的坐标或参数
- 常用组合键用 '+' 分隔，如 ctrl+n、alt+f4、shift+tab；系统专属按键必须与文末给出的操作系统匹配，禁止跨系统误用（如 Linux 上没有 Win+R 运行框）

【本系统操作要点】
""" + OS_SHORTCUT_HINT + """
当前操作系统为：
""" + SYSTEMTYPE


PLANNING_TOOL_PROMPT = """你是一个 GUI 自动化测试规划助手。请分析当前截图中的页面状态，结合用户任务和知识库信息，规划出完成该任务所需的子任务列表。

要求：
- 分析截图中当前页面的可见元素（导航栏、按钮、表格、表单等）
- 结合任务描述和知识库，将复杂任务拆解为 N 个有序的子任务
- 每个子任务必须包含 content（子任务内容描述）和 expected_result（预期结果描述）
- 子任务粒度适中：一个子任务对应页面上一个可验证的功能操作

输出格式：
严格输出 JSON 数组，不要输出其他文字。示例：
[
  {"content": "点击左侧导航栏'AGENT管理'菜单，进入AGENT列表页", "expected_result": "页面显示AGENT列表表格"},
  {"content": "点击'新建'按钮，验证新建表单弹出", "expected_result": "右侧弹出配置抽屉表单"},
  {"content": "在表单中填写必填项并保存，验证新建成功", "expected_result": "列表中新增一条记录"}
]
"""



class RPAgent:
    """RPA 智能体，执行观察-决策-执行ui循环"""

    def __init__(self, vnc_host: str = "localhost", vnc_port: int = 5901, vnc_password: str = "123456"):
        self.vnc = VNCClient(host=vnc_host, port=vnc_port, password=vnc_password)
        self.max_steps = 120  # 最大步数，防止无限循环
        self.step = 0
        self.history = []  # 操作历史

        self.planning = ""
        self.task = ""              # 当前任务描述（供 planning 工具使用）
        self.task_plan = []         # 规划生成的子任务列表
        self.current_plan_idx = -1  # 当前执行的子任务索引，-1 表示无活跃计划
        self._subtask_result = None  # subtask_done 动作暂存的结果，由 for 循环读取
        self._prev_screenshot = None  # 上一步观察帧（画面差异检测）
        self._repeat_guidance = ""    # 真重复时注入下一步 prompt 的引导

    def _parse_action(self, response: str) -> dict:
        """解析模型输出的 JSON 动作"""
        # 1. 优先直接解析整体文本（去除 markdown 围栏后）
        cleaned = response.strip()
        if cleaned.startswith("```"):
            # 去掉首行 ``` 或 ```json 围栏
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # 2. 括号深度扫描：从第一个 { 起提取最外层完整 JSON 对象
        #    需跳过字符串字面量内的花括号与转义符，避免 thought 文本干扰
        start = response.find("{")
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
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(response[start:i + 1])
                            except json.JSONDecodeError:
                                break

        # 兜底：解析失败时返回 wait 动作并附原始输出片段
        return {"action": "wait", "params": {}, "thought": f"无法解析模型输出: {response[:100]}"}

    @staticmethod
    def _validate_xy(params: dict) -> tuple:
        """校验归一化坐标合法性，返回 (是否有效, 原因)"""
        try:
            x, y = float(params.get("x")), float(params.get("y"))
        except (TypeError, ValueError):
            return False, "x/y 缺失或不是数字"
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            return False, f"x/y 超出 [0,1] 范围: x={x}, y={y}"
        return True, ""

    @staticmethod
    def _is_repeat(last_action: dict, new_action: dict, xy_eps: float = 0.01) -> bool:
        """判断新动作是否与上一步重复；click 类坐标用 eps 容差比较"""
        a1 = (last_action.get("action") or "").lower()
        a2 = (new_action.get("action") or "").lower()
        if a1 != a2 or not a1:
            return False
        p1, p2 = last_action.get("params", {}) or {}, new_action.get("params", {}) or {}
        if a2 in ("click", "double_click", "right_click"):
            try:
                return (abs(float(p1.get("x", 0)) - float(p2.get("x", 0))) <= xy_eps and
                        abs(float(p1.get("y", 0)) - float(p2.get("y", 0))) <= xy_eps)
            except (TypeError, ValueError):
                return False
        if a2 == "type":
            return p1.get("text", "") == p2.get("text", "")
        if a2 == "press":
            return p1.get("key", "") == p2.get("key", "")
        if a2 == "scroll":
            return p1.get("direction", "") == p2.get("direction", "")
        # wait/done 等不做重复拦截
        return False

    @staticmethod
    def _screen_changed(prev: Image.Image, curr: Image.Image, threshold: float = 6.0) -> bool:
        """
        对比两张截图判断画面是否发生实质变化。
        缩到 64x40 灰度做平均像素差，抗噪（光标闪烁/时钟跳动不会误判）。
        threshold: 平均灰度差阈值，超过视为画面变化
        """
        if prev is None or curr is None:
            return True  # 无上一帧参考，视为已变化（不误拦截）
        size = (64, 40)
        a = prev.convert("L").resize(size, Image.Resampling.BILINEAR)
        b = curr.convert("L").resize(size, Image.Resampling.BILINEAR)
        pa = a.load()
        pb = b.load()
        total = 0
        for yy in range(size[1]):
            for xx in range(size[0]):
                total += abs(pa[xx, yy] - pb[xx, yy])
        mean_diff = total / (size[0] * size[1])
        return mean_diff > threshold

    def _expand_skill(self, skill_action: str, params: dict) -> list:
        """将技能动作展开为确定性子动作列表；参数非法时返回单步 wait 并提示

        系统类型一律使用配置常量 SYSTEM（模型不传也无法传错，防止跨系统误用快捷键）
        """
        try:
            if skill_action == "skill_open_app":
                return open_application(
                    application=params.get("application", ""),
                    system=SYSTEM,
                )

            if skill_action == "skill_open_url":
                return open_url(
                    url=params.get("url", ""),
                    browser=params.get("browser", ""),
                    system=SYSTEM,
                )

            if skill_action == "skill_open_webpage":
                # needlogin 兼容 bool 与字符串
                needlogin = params.get("needlogin", False)
                if isinstance(needlogin, str):
                    needlogin = needlogin.strip().lower() in ("true", "1", "yes", "是")
                return open_webpage(
                    system=SYSTEM,
                    needlogin=bool(needlogin),
                    username=params.get("username", ""),
                    password=params.get("password", ""),
                    loginCoordinates=params.get("loginCoordinates"),
                    pdCoordinates=params.get("pdCoordinates"),
                    buttonCoordinates=params.get("buttonCoordinates"),
                )
        except Exception as e:
            print(f"  [技能] 参数非法: {e}")
            return [{"action": "wait", "params": {"seconds": 1},
                     "thought": f"技能参数非法: {e}，请改用基础动作分步操作"}]

        return []

    def _execute_action(self, action_data: dict, screenshot: Image.Image) -> bool:
        """
        执行一个动作
        screenshot: 当步截图，用于归一化坐标到像素的换算
        返回 True 表示继续循环，False 表示结束
        """
        action = action_data.get("action", "wait")
        params = action_data.get("params", {})
        thought = action_data.get("thought", "")

        print(f"[Step {self.step}] 动作: {action}, 参数: {params}")
        print(f"  思考: {thought}")

        try:
            if action == "click":
                x, y = self._to_pixels(params, screenshot)
                self.vnc.click(x, y)
                # 点任务栏区域（归一化 y>0.95）通常是打开/切换应用，确定性最大化窗口
                if float(params.get("y", 0)) > 0.95:
                    self.vnc.maximize_window(SYSTEM)

            elif action == "double_click":
                x, y = self._to_pixels(params, screenshot)
                self.vnc.double_click(x, y)
                # 双击桌面图标打开应用后，确定性最大化窗口
                self.vnc.maximize_window(SYSTEM)

            elif action == "right_click":
                x, y = self._to_pixels(params, screenshot)
                self.vnc.click(x, y, button=3)

            elif action == "type":
                text = params.get("text", "")
                self.vnc.type_text(text)

            elif action == "press":
                key = params.get("key", "")
                self.vnc.press_key(key)

            elif action == "scroll":
                direction = params.get("direction", "down")
                # 获取当前鼠标位置（用屏幕中心）
                screenshot = self.vnc.screenshot()
                cx, cy = screenshot.width // 2, screenshot.height // 2
                dir_val = 1 if direction == "up" else -1
                self.vnc.scroll(cx, cy, dir_val)

            elif action == "wait":
                # 模型可指定等待时长，限制在 0.5~3 秒避免浪费
                seconds = params.get("seconds", 1.0)
                try:
                    seconds = float(seconds)
                except (TypeError, ValueError):
                    seconds = 1.0
                time.sleep(max(0.5, min(3.0, seconds)))

            elif action == "done":
                print("任务完成！")
                return False

            elif action in ("skill_open_app", "skill_open_webpage", "skill_open_url"):
                # 固定流程技能：展开为确定性子动作顺序执行（一个决策步内完成）
                sub_steps = self._expand_skill(action, params)
                print(f"  [技能] 展开为 {len(sub_steps)} 个子步骤")
                for i, sub in enumerate(sub_steps, 1):
                    sub_action = sub.get("action", "")
                    sub_params = sub.get("params", {})
                    print(f"  [技能 {i}/{len(sub_steps)}] {sub_action} {sub_params}")
                    # 子动作坐标基于当步截图换算；子动作不含 done，忽略其返回值
                    self._execute_action(sub, screenshot)

            elif action == "planning":
                # 已存在计划时忽略重复调用，避免阶段2中覆盖正在执行的子任务列表
                if self.task_plan:
                    print("  [规划] 子任务计划已存在，忽略重复的 planning 调用")
                else:
                    # 调用视觉模型分析当前页面并生成结构化子任务列表
                    plan_prompt = f"""用户任务：{self.task}
知识库信息：{self.knowledge_summary or '无'}

请分析当前截图中的页面状态，规划完成该任务所需的子任务列表。"""
                    plan_response = chat_vision(plan_prompt, screenshot,
                                                system_prompt=PLANNING_TOOL_PROMPT)
                    self.task_plan = parse_planning(plan_response)
                    if self.task_plan:
                        print(f"  [规划] 生成 {len(self.task_plan)} 个子任务:")
                        for i, t in enumerate(self.task_plan, 1):
                            print(f"    {i}. {t['content']} (预期: {t['expected_result']})")
                    else:
                        print(f"  [规划] 解析失败，继续使用基础动作模式")

            elif action == "subtask_done":
                # 暂存本子任务执行结果，由 run_task 的 for 循环读取后写入计划并推进
                self._subtask_result = {
                    "result": params.get("result", ""),
                    "passed": bool(params.get("passed", False)),
                }
                passed_str = "通过" if self._subtask_result["passed"] else "未通过"
                print(f"  [子任务 {self.current_plan_idx + 1}/{len(self.task_plan)}] "
                      f"{passed_str}，进入下一子任务")

            else:
                print(f"未知动作: {action}")
                time.sleep(1)

        except Exception as e:
            print(f"执行动作出错: {e}")
            time.sleep(1)

        return True

    @staticmethod
    def _to_pixels(params: dict, screenshot: Image.Image) -> tuple:
        """归一化坐标 (0~1) 转像素坐标，基于当步截图实际尺寸换算"""
        x_norm, y_norm = float(params.get("x", 0.5)), float(params.get("y", 0.5))
        w, h = screenshot.size
        px = max(0, min(w - 1, int(x_norm * (w - 1))))
        py = max(0, min(h - 1, int(y_norm * (h - 1))))
        return px, py

    def _decision_step(self, task: str, knowledge_block: str, plan: dict | None,
                       progress_callback=None, control_checker=None) -> str:
        """
        执行单步观察-决策-执行循环体。
        plan=None 为固定流程阶段；传入 plan dict 为子任务阶段（注入子任务上下文）。
        返回状态码：
          "continue"     - 正常执行一步，继续当前阶段循环
          "done"         - 模型输出 done，任务全部结束
          "planned"      - planning 已生成子任务计划（固定流程阶段据此进入子任务阶段）
          "subtask_done" - 当前子任务完成（子任务阶段据此推进到下一子任务）
          "stop"         - 外部终止
        """
        # 0. 外部控制检查：终止则结束任务，暂停则等待恢复
        if control_checker:
            cmd = control_checker()
            if cmd == "stop":
                return "stop"
            while cmd == "pause":
                time.sleep(0.5)
                cmd = control_checker()
                # 暂停期间也允许直接终止
                if cmd == "stop":
                    return "stop"

        self.step += 1
        t_step_start = time.perf_counter()

        # 1. 观察：截图（原始图用于坐标换算，缩图由 llm_client 内部处理）
        t_obs_start = time.perf_counter()
        screenshot = self.vnc.screenshot()
        self.saveScreenShot(self.step, self.rand, screenshot)
        t_obs = time.perf_counter() - t_obs_start

        # 2. 决策：调用视觉模型，坐标校验失败时带反馈重试一次
        guidance_block = f"\n{self._repeat_guidance}\n" if self._repeat_guidance else ""
        # 子任务阶段注入当前子任务上下文
        subtask_block = ""
        if plan is not None:
            completed_count = sum(1 for t in self.task_plan if t.get("completed"))
            subtask_block = f"""
【当前子任务 ({self.current_plan_idx + 1}/{len(self.task_plan)})】
内容：{plan['content']}
预期结果：{plan['expected_result']}
已完成：{completed_count} 个子任务
当前子任务完成后，请输出 subtask_done 标记完成，再继续下一个子任务。所有子任务完成后输出 done。
"""
        prompt = f"""用户的任务是：{task}
任务规划是：{self.planning}
当前是第 {self.step} 步操作。请分析当前屏幕截图，判断界面状态，并决定下一步应该执行什么操作。
{knowledge_block}{subtask_block}{guidance_block}
之前的操作历史：
{json.dumps(self.history[-5:], ensure_ascii=False, indent=2) if self.history else '（无，这是第一步）'}

请输出下一步操作的 JSON。"""

        action_data = None
        last_invalid_reason = ""
        for attempt in range(2):
            hint = ""
            if attempt == 1 and last_invalid_reason:
                hint = f"\n注意：上一次输出无效（{last_invalid_reason}）。x/y 必须是 0~1 的相对比例值，严禁像素值。"
            response = chat_vision(prompt + hint, screenshot, system_prompt=SYSTEM_PROMPT)
            candidate = self._parse_action(response)

            action = (candidate.get("action") or "").lower()
            if action in ("click", "double_click", "right_click"):
                ok, reason = self._validate_xy(candidate.get("params", {}))
                if ok:
                    action_data = candidate
                    break
                # 坐标非法，记录原因进入重试
                last_invalid_reason = reason
                print(f"  [校验] 坐标无效: {reason}，重试...")
                continue

            action_data = candidate
            break

        if action_data is None:
            # 两次均产出非法坐标，跳过执行等待画面变化
            action_data = {"action": "wait", "params": {"seconds": 1},
                           "thought": f"坐标校验失败: {last_invalid_reason}"}

        # 计算 LLM 决策耗时（观察之后到 LLM+校验结束）
        t_llm = time.perf_counter() - t_obs_start - t_obs

        # 3. 重复动作防护：动作与上步相同 且 画面未变化，才判定为原地踏步
        same_action = bool(self.history and self._is_repeat(self.history[-1], action_data))

        if same_action:
            changed = self._screen_changed(self._prev_screenshot, screenshot)
            if changed:
                # 画面已变化：上步动作生效了（如输入框已聚焦/页面已切换），放行当前决策
                print(f"  [防护] 动作相同但画面已变化，判定生效，放行")
                self._repeat_guidance = ""
            else:
                # 画面未变化：上步动作真没生效，拦截并等待，下步注入引导
                print(f"  [防护] 动作重复且画面未变化 ({action_data.get('action')})，跳过执行")
                self._repeat_guidance = (
                    "重要：上一步操作后画面没有任何变化，说明该操作未生效。"
                    "请判断：①若目标是输入框且应已聚焦，直接执行 type 输入，不要再点击；"
                    "②若是应用/页面正在启动加载，执行 wait(2) 耐心等待；"
                    "③若点击没有命中目标，重新瞄准目标的视觉中心，或换一种操作方式（如改用快捷键）。"
                )
                action_data = {"action": "wait", "params": {"seconds": 2},
                               "thought": "上一步动作重复且画面未变化，等待并换思路"}
        else:
            self._repeat_guidance = ""

        # 4. 执行
        t_exec_start = time.perf_counter()
        self._execute_action(action_data, screenshot)
        t_exec = time.perf_counter() - t_exec_start

        # 记录历史（含完整耗时）+ 进度回调（写入 JSONL log）
        t_total = time.perf_counter() - t_step_start
        duration = {
            "observe": round(t_obs, 2),
            "llm": round(t_llm, 2),
            "execute": round(t_exec, 2),
            "total": round(t_total, 2),
        }

        step_record = {
            "step": self.step,
            "action": action_data.get("action"),
            "params": action_data.get("params", {}),
            "thought": action_data.get("thought", ""),
            "duration": duration,
        }
        if plan is not None:
            step_record["subtask"] = {
                "index": self.current_plan_idx + 1,
                "total": len(self.task_plan),
                "content": plan["content"],
                "expected_result": plan["expected_result"],
            }
        self.history.append(step_record)

        if progress_callback:
            progress_callback(step_record)

        # 记录本步观察帧，供下一步画面差异检测（判断本步动作是否引起变化）
        self._prev_screenshot = screenshot

        # 操作后短暂等待，让界面响应
        time.sleep(0.8)

        # 5. 返回阶段状态码
        action_name = (action_data.get("action") or "").lower()
        if action_name == "done":
            return "done"
        if plan is None and action_name == "planning" and self.task_plan:
            return "planned"
        if plan is not None and action_name == "subtask_done":
            return "subtask_done"
        return "continue"

    def run_task(self, task: str, progress_callback=None, control_checker=None) -> str:
        """
        执行一个任务，两阶段循环直到完成或达到最大步数：
        阶段1（固定流程）：打开应用/网址/登录，模型调用 planning 后进入阶段2
        阶段2（子任务）：for plan in task_plan，每个子任务独立观察-决策-执行内循环
        progress_callback: 回调函数，接收当前步骤信息用于前端展示
        control_checker: 控制回调，返回 "pause"/"stop"/None，用于外部暂停与终止
        """
        self.step = 0
        self.rand = random.randint(0, 1000000)
        self.history = []
        self.task = task
        self.task_plan = []
        self.current_plan_idx = -1
        self._subtask_result = None
        self._prev_screenshot = None
        self._repeat_guidance = ""
        self.vnc.connect()

        # 加载知识摘要：按任务文本匹配页面知识，注入后续每步 prompt
        self.knowledge_summary = get_summary(task)
        if self.knowledge_summary:
            print(f"[知识] 匹配到页面知识，已注入决策上下文")
        knowledge_block = f"\n{self.knowledge_summary}\n" if self.knowledge_summary else ""
        self.planning = chat_text(task + knowledge_block, SYSTEM_PROMPT_PLANNING)

        # 预处理：确定性地显示桌面（最小化所有窗口），避免 LLM 读到 IDE/浏览器自身页面
        # Windows: win+d；Linux(GNOME): super+d
        show_desktop_key = "super+d" if SYSTEM == "linux" else "win+d"
        self.vnc.press_key(show_desktop_key)
        time.sleep(1.0)

        try:
            # 阶段1：固定流程（打开应用/打开网址/登录），直到 planning 生成子任务计划
            while self.step < self.max_steps:
                status = self._decision_step(task, knowledge_block, None,
                                             progress_callback, control_checker)
                if status == "stop":
                    return f"任务已手动终止，共执行 {self.step} 步。"
                if status == "done":
                    return f"任务完成！共执行 {self.step} 步。"
                if status == "planned":
                    break

            # 阶段2：子任务执行，for 循环逐个推进，每个子任务独立内循环直到 subtask_done
            if self.task_plan:
                for plan_idx, plan in enumerate(self.task_plan):
                    self.current_plan_idx = plan_idx
                    print(f"[规划] 开始子任务 {plan_idx + 1}/{len(self.task_plan)}: {plan['content']}")
                    while self.step < self.max_steps:
                        status = self._decision_step(task, knowledge_block, plan,
                                                     progress_callback, control_checker)
                        if status == "stop":
                            return f"任务已手动终止，共执行 {self.step} 步。"
                        if status == "done":
                            return f"任务完成！共执行 {self.step} 步。"
                        if status == "subtask_done":
                            # 写入本子任务结果，break 内循环后由 for 推进到下一子任务
                            res = self._subtask_result or {}
                            plan["completed"] = True
                            plan["actual_result"] = res.get("result", "")
                            plan["passed"] = res.get("passed", False)
                            self._subtask_result = None
                            break

                print(f"[规划] 全部 {len(self.task_plan)} 个子任务已执行完成")

            if self.step >= self.max_steps:
                return f"已达到最大步数 ({self.max_steps})，任务可能未完成。"
            return f"任务完成！共执行 {self.step} 步。"

        finally:
            self.vnc.disconnect()

    def saveScreenShot(self, step: int, rand: int, screenshot: Image.Image):
        screenshot.save(f"./screenshots/sh_{rand}_{step}.png")


if __name__ == "__main__":
    # 简单测试
    agent = RPAgent(vnc_host="localhost", vnc_port=5901, vnc_password="123456")
    result = agent.run_task("打开终端")
    print(result)
