"""
RPA Agent 核心逻辑 - 观察-决策-执行循环
使用视觉大模型分析截图，决定下一步操作，通过 VNC 执行
"""
from __future__ import annotations

import re
import json
import math
import time
import unicodedata
from pathlib import Path
from PIL import Image, ImageDraw
from llm_client import chat_vision, chat_text, token_tracker
from vnc_client import VNCClient
from knowledge_loader import get_summary
from prompt_context import history_for_prompt, login_for_prompt, direct_url_verification, use_business_knowledge, pointer_relocation
from interaction_guard import (
    InteractionGuard,
    InteractionGuardError,
    compare_screen_state,
    target_region_around_point,
    validate_normalized_coordinates,
)
from task_policy import QueryOnlyPolicy
from tools import (
    input_method,
    input_text,
    open_application,
    open_webpage,
    open_url,
    planning as parse_planning,
)
import random

SYSTEMTYPE = "WINDOWS"
# 归一化系统标识（技能层与快捷键分支统一使用小写：windows/linux/mac）
SYSTEM = SYSTEMTYPE.strip().lower()

SCREENSHOT_DIR = Path(__file__).resolve().parent / "screenshots"
SUMMARY_DIR = Path(__file__).resolve().parent / "summary"

# 各系统专属快捷键与启动方式提示，注入提示词，防止模型跨系统误用按键
_OS_SHORTCUT_HINTS = {
    "windows": (
        "- 打开应用：按 Win 键开开始菜单，输入应用名后回车；网址只能在已打开浏览器的地址栏输入，禁止交给 Win+R\n"
        "- 显示桌面：win+m（非切换式最小化）；最大化当前窗口使用 maximize_window"
    ),
    "linux": (
        "- 打开应用：按 Super 键（即 Win 键）打开活动概览，输入应用名后回车\n"
        "- 打开终端：Ctrl+Alt+T；终端中启动浏览器：firefox \"网址\" &、google-chrome \"网址\" &，或 xdg-open \"网址\" &\n"
        "- 显示桌面：super+d；最大化当前窗口：super+up\n"
        "- 严禁使用 Win+R 运行框、start 命令等 Windows 专属操作（Linux 上不存在，按下无任何效果）"
    ),
    "mac": (
        "- 打开应用或网址：按 Cmd+Space 打开聚焦搜索，输入内容并验收后再按 Enter\n"
        "- 选择当前字段全部内容：Cmd+A；切换输入法：Ctrl+Space\n"
        "- 严禁使用 Win+R、Win+D 或 Ctrl+Alt+T 等 Windows/Linux 专属快捷键"
    ),
}
OS_SHORTCUT_HINT = _OS_SHORTCUT_HINTS.get(SYSTEM, _OS_SHORTCUT_HINTS["windows"])


def _prompt_for_system(template: str, system: str) -> str:
    """用实例目标系统替换模块默认快捷键提示，避免配置与决策分裂。"""
    normalized = {"win": "windows", "win32": "windows", "darwin": "mac"}.get(
        str(system or "windows").lower().strip(),
        str(system or "windows").lower().strip(),
    )
    hint = _OS_SHORTCUT_HINTS.get(normalized, _OS_SHORTCUT_HINTS["windows"])
    display_name = {"windows": "WINDOWS", "linux": "LINUX", "mac": "MACOS"}.get(
        normalized,
        "WINDOWS",
    )
    rendered = template.replace(OS_SHORTCUT_HINT, hint)
    return rendered.replace(
        f"当前操作系统为：\n{SYSTEMTYPE}",
        f"当前操作系统为：\n{display_name}",
    )

SYSTEM_PROMPT = """你是一个 GUI 自动化智能体（Computer Use Agent）。你的任务是通过观察屏幕截图，分析当前界面状态，并决定下一步操作来完成用户的任务。

你可以执行以下操作类型：
1. click(x, y, target) - 点击屏幕上的指定坐标（左键）；target 必须是截图上实际可见的目标名称
2. double_click(x, y, target) - 双击指定坐标
3. right_click(x, y, target) - 右键点击指定坐标
4. type(text) - 旧调用兼容的追加输入，仍须在下一步 verify_text_input；新的表单字段输入优先使用 skill_input_text
5. press(key) - 按下按键；支持单键（enter, escape, tab, backspace, space, up, down, left, right 等）与组合键（ctrl+n, alt+f4, shift+tab 等，用 '+' 分隔）
6. scroll(x, y, amount, target) - 在指定目标容器内滚动；x/y 为容器内部坐标，amount 为 -5~5 的非零整数（负数向下），target 为截图上可见的容器名称
7. wait(seconds) - 等待一段时间（用于页面加载等，seconds 为 0.5~3 的数字）
8. done() - 任务完成，结束循环
9. skill_open_app(application) - 【固定流程技能】打开系统应用搜索并输入应用名，但不自动回车。下一步先 verify_text_input，确认正确后再 press("enter") 启动。例：{"action": "skill_open_app", "params": {"application": "edge"}}
10. skill_open_webpage(needlogin, stage, username, password, loginCoordinates, pdCoordinates, buttonCoordinates) - 【分阶段登录技能】stage 必须按 username→password→submit 依次调用。username/password 阶段只聚焦并输入，随后必须 verify_text_input；前一字段验收通过后才能进入下一阶段。submit 阶段只点击登录按钮。
    用户名阶段示例：{"action": "skill_open_webpage", "params": {"needlogin": true, "stage": "username", "username": "admin", "loginCoordinates": {"x": 0.5, "y": 0.24}}}
    密码阶段示例：{"action": "skill_open_webpage", "params": {"needlogin": true, "stage": "password", "password": "123456", "pdCoordinates": {"x": 0.5, "y": 0.30}}}
    提交阶段示例：{"action": "skill_open_webpage", "params": {"needlogin": true, "stage": "submit", "buttonCoordinates": {"x": 0.46, "y": 0.40}}}
    若网页无需登录（needlogin=false），不要使用此技能。
11. skill_open_url(url, browser) - 【固定流程技能】仅在浏览器已经打开且位于前台时，聚焦地址栏并输入网址，但不自动回车。下一步先 verify_text_input，确认正确后再 press("enter")。browser 仅作兼容标记，不用于拼接系统命令。
    例：{"action": "skill_open_url", "params": {"url": "https://example.com/login", "browser": "edge"}}
    若浏览器尚未打开，先使用 skill_open_app 并在输入验收后按 Enter；严禁把网址输入 Win+R 运行框。
12. planning() - 【任务规划工具】当固定流程（打开应用/打开网址/登录）已完成，进入需要逐步操作的复杂任务阶段时调用。调用后系统会用当前截图分析页面并生成结构化子任务列表，后续逐步执行。无需参数。
    例：{"action": "planning", "params": {}, "thought": "登录已完成，需要规划测试任务"}
13. subtask_done(result, passed) - 【子任务完成标记】当当前子任务的操作已完成并达到预期时调用，标记该子任务完成并进入下一个子任务。result 为实际操作结果描述，passed 为是否达到预期（true/false）。所有子任务完成后输出 done()。
    例：{"action": "subtask_done", "params": {"result": "点击后显示了AGENT列表", "passed": true}, "thought": "预期结果已达成"}
14. discover_options(category, options, task_template) - 【动态子任务发现工具】当你在执行过程中点开一个下拉框/选项列表，看到多个选项，且任务要求"测试所有""每个都试一遍"等遍历场景时使用。调用后会根据选项数量动态生成对应数量的子任务，替换当前模糊的子任务。就像人类看到有3个选项后心里会规划"那我要做3次"一样。
    参数：
    - category: 选项类别名称，如"助手类型"
    - options: 识别到的所有选项列表，如["纯文本", "TTS", "虚拟人"]
    - task_template: 每个选项对应的子任务模板，用 {opt} 占位符表示选项值
    例：{"action": "discover_options", "params": {"category": "助手类型", "options": ["纯文本", "TTS", "虚拟人"], "task_template": "创建一个助手类型为 {opt} 的场景（场景渠道选云南大瓦特），填写必填项后保存并验证列表新增成功"}, "thought": "发现助手类型有3个选项，需要每个都创建一个场景来测试"}
    使用时机：当你点开下拉框看到所有选项后立即调用，不要先选了一个又来回切换。调用后系统会自动替换当前子任务为N个具体子任务，你按顺序逐个执行即可。
15. skill_input_text(text, field_type, replace) - 【字段输入】仅在输入框已聚焦时输入一次但不提交。replace 默认 true，会先全选再输入，避免把内容追加到旧值后面；终端命令追加时才设 false。field_type 可为 auto/text/url/email/username/password/number/code/command/app_search。下一步必须看截图并调用 verify_text_input。
16. verify_text_input(observed_text, readable, cause, ime_visible) - 【输入验收】普通字段逐字符抄录截图中的实际文本；密码字段 observed_text 填截图可见的掩码字符。cause 可为 unknown/ime/focus/partial/format。
17. retry_text_input(text, field_type, cause) - 【一次性纠错】替换当前字段；只有截图明确显示输入法干扰时 cause 才能用 ime，同一字段最多重试一次。
18. skill_input_method(operation, candidate) - 【输入法操作】operation 可为 toggle_language、cycle_layout、cancel、commit、select_candidate；只有截图明确需要时使用。skill_ime 是兼容别名。
19. abort_input(reason) - 【受控失败出口】字段无法可靠读取，或唯一一次纠错后仍错误时调用。它会停止当前任务并返回明确失败，绝不能带着未确认文本继续提交。
20. move(x, y, duration_ms, steps, target) - 将鼠标移到指定目标；不点击。duration_ms 为 0~3000，steps 为 1~100。
21. drag(start_x, start_y, end_x, end_y, duration_ms, steps, button, target) - 安全拖拽；steps 为 1~100，默认左键，中途异常也会释放鼠标。桌面文件移动必须使用此动作并验证前后位置。
22. maximize_window() - 显式最大化当前活动窗口；不能通过普通点击/双击隐式触发。

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
- 点击按钮：{"action": "click", "params": {"x": 0.52, "y": 0.31, "target": "开始按钮"}, "thought": "我看到了'开始'按钮，点击它"}
- 输入文字：{"action": "skill_input_text", "params": {"text": "hello world", "field_type": "text", "replace": true}, "thought": "在已聚焦输入框中替换输入，随后验收"}
- 按回车：{"action": "press", "params": {"key": "enter"}, "thought": "按回车键确认"}
- 等待加载：{"action": "wait", "params": {"seconds": 2}, "thought": "页面正在加载，等待"}
- 任务完成：{"action": "done", "params": {}, "thought": "任务已完成"}

注意事项：
- 当前操作系统已在文末明确给出，严禁猜测；所有快捷键与启动方式必须与该系统匹配，禁止使用其他系统的专属操作
- 打开应用搜索文字验收通过后，单独按 Enter 启动；窗口出现后若未最大化，再使用文末给出的本系统快捷键
- 当识别到当前任务正在使用的应用窗口不是最大化时，要先最大化该应用，使用文末给出的本系统最大化快捷键
- 任务要求创建一条新的记录时必须有名称，名称格式为：“test_任务名称前两个字的英文缩写_从1开始递增，重复了的话随机加一个三位数”
  例如：test_zj_1, test_zj_2, test_zj_3, test_zj_4, test_zj_5, test_zj_6, test_zj_7, test_zj_8, test_zj_9, test_zj_10, test_zj_11, test_zj_12, test_zj_13, test_zj_14, test_zj_15, test_zj_16, test_zj_17, test_zj_18, test_zj_19, test_zj_20, test_zj_21, test_zj_22, test_zj_23, test_zj_24, test_zj_25, test_zj_26, test_zj_27, test_zj_28, test_zj_29, test_zj_30
- 若是当前桌面的应用与任务相关，要先强制最小化或关闭该应用，再执行任务操作
- 坐标比例要对应截图中实际元素的位置，要准确，直接报告目标相对位置，不要自行做分辨率/缩放换算
- 每次只执行一个操作，逐步完成任务
- click/move/scroll 的 params.target 必须填写截图中实际可见的控件或容器名称；不得用虚构标签绕过任务范围
- 新的表单字段输入必须使用 skill_input_text；输入后先 verify_text_input，验收前禁止点击其他字段、按 Enter/Tab、提交、规划或标记完成
- 闪烁的竖直文本插入光标、选区背景和拼写检查波浪线都不是字段字符；若光标贴住或覆盖末字符导致字符轮廓有歧义，必须 readable=false 并先 wait 获取另一闪烁相位，禁止把光标竖线合并成字母笔画
- ime_visible 只表示截图中确实存在候选框或未提交的输入法组合串；任务栏语言标记或一个看似错误的拉丁字母本身不能作为 ime_visible=true 的依据
- 首次输入不要猜测或盲切输入法；只有截图明确出现候选框、全角乱码或输入法模式干扰时，才使用 cause="ime" 做一次纠错
- 中文和 emoji 需要可靠 Unicode 通道；若执行器报告当前远程 VNC 不支持，不要循环重输或改用剪贴板
- 唯一一次 retry_text_input 后仍未通过时，若执行器要求排除光标闪烁，只允许被动 wait 一次；复核仍失败或密码掩码持续无法可靠读取时，立即 abort_input，禁止再次输入或无限 wait
- 不要重复执行上一步完全相同的操作，除非上一步明显未生效；卡住时换一种方式
- 如果不确定，先 wait() 观察变化
- 如果任务已完成，输出 done()
- 如果外部要求暂停，输出 pause()
- 如果外部要求终止，输出 stop()
- 注意输入的网址中英文混合，要正确解析，不要用中文的'：'
- 执行自动化测试时，要确保截图中包含所有必要元素，避免元素缺失导致操作失败
- 执行自动化测试时，要先分析截图中的元素，结合任务生成参数或外部要求，确定操作目标，再执行操作
- 若当前浏览器已经显示任务指定页面，直接在该窗口继续；只有浏览器未打开或页面无关时才打开新窗口
- 重要：截图中可能出现 IDE 编辑器、浏览器控制台、命令行终端等开发工具界面，请完全忽略它们，寻找远程桌面的正常桌面环境（如桌面图标、任务栏、开始菜单等）。如果看到系统网页端界面，说明窗口未最小化，应先最小化或关闭该窗口。

- 【下拉列表交互规则】遇到下拉选择框（select/dropdown）：
  - 先用鼠标点击下拉框头部展开列表
  - 按当前任务策略使用可见选项定位；只有焦点和控件键盘行为明确且策略允许时，才用键盘导航，不要猜测通用下拉框支持输入匹配或 Enter。
  - 多选框选中一个值后可能仍然展开，先核对已选标签/勾选，再操作另一个目标；不要重复切换已正确的选项。
  - 需要关闭弹层时优先 press(escape)，随后观察弹层关闭且已选值保留；也可点击经过验收的原下拉框头部。不要点击业务卡片或不明确的“页面空白区域”。
  - 同一页面如果有多个下拉框，**必须先在大脑里区分每个下拉框的选项范围**，避免跨下拉框串项（例如："场景渠道"的选项和"标题模型"的选项不能混选）
  - 用鼠标点列表项时，严格执行前面"坐标规则"中的"正中心"要求——瞄准高亮行整条区域的正中心

- 【表单操作前置检查，极其重要】点击任何表单控件前，必须先做"读值决策"：
  - ✅ **先读后点**：看截图中该控件当前显示的值是什么（输入框里的文字、下拉框里已选中的文本、Toggle 是 ON 还是 OFF），再决定要不要操作
  - ✅ **已正确则跳过**：如果控件当前值已经是任务要求的目标值（如"助手类型"默认就是"纯文本"，知识里也说"一般不需要修改"）→ **直接跳过这个字段，不要点、不要改**
  - ✅ **下拉展开后先校验**：如果下拉框已经处于展开状态，先看当前高亮/已选中的那个选项是不是目标值——是就点空白处关闭列表，不是才用键盘或鼠标切换
  - ✅ **相邻控件防误点**：同一列里紧邻的下拉框/输入框（如"助手类型"和"标题模型"上下相邻），点击前必须在脑子里再次确认自己瞄准的是哪个控件的 y 坐标，不要让 ±0.02 的估算误差飘到上一行或下一行

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
    任务：从页面http://172.19.133.168:7010/web/views/login.html登录，只在体验中心的智能体能力页面执行查询测试
    步骤：
    1. 当前在浏览器中，新开一个页面并进入该页面
    2. 打开http://172.19.133.168:7010/web/views/login.html登录页面
    3. 分阶段输入并验收用户名
    4. 分阶段输入并验收密码
    5. 点击"登录"按钮
    6. 进入待测试页面
    7. 进入体验中心的智能体能力页面
    8. 仅规划并执行测试类型/测试场景名称的查询，不测试新增、编辑、删除、保存或其他功能
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


STATE_VERIFIER_PROMPT = """你是独立的 GUI 状态验收器。你只能依据这一张当前截图中的可见证据判断，不得相信执行智能体的文字声明、历史 thought 或预期结果本身。

严格输出一个 JSON 对象：
{"passed": true或false, "state": "当前页面状态", "evidence": ["截图中实际可见的文字/控件/结果"], "reason": "判断理由"}

规则：
- 只有截图中存在与验收目标直接对应的可见锚点时 passed 才能为 true。
- 点击已派发、页面正在加载、目标可能存在、或智能体声称完成，都不是通过证据。
- 查询结果可以是表格/分页，也可以是明确的空结果提示，但必须已经结束加载且筛选值仍正确。
- 登录成功必须以登录表单消失并出现平台导航/首页锚点为证据。
- 退出成功必须以登录表单重新出现为证据。
- evidence 至少包含一个具体可见事实；无法读取或有歧义时返回 false。
"""


QUERY_POINTER_VERIFIER_PROMPT = """你是只读查询任务的鼠标落点验收器。截图上叠加了一个红色圆环和十字，十字中心就是即将执行的鼠标坐标；红色标记不是原页面内容。

严格输出 JSON：
{"passed": true或false, "target_visible": true或false, "marker_inside_target": true或false, "target_bbox": [x_min, y_min, x_max, y_max]或null, "suggested_x": 0到1的小数或null, "suggested_y": 0到1的小数或null, "evidence": ["可见事实"], "reason": "理由"}

规则：
- 只有目标控件/菜单项/选项/下拉容器清楚可见，并且红色十字中心确实位于其可交互区域内部时才能 passed=true。
- target_bbox 必须是目标完整可交互区域的紧致外接矩形，并按完整截图以 x/(width-1)、y/(height-1) 归一化到 0~1；目标不可见或无法可靠定位时必须为 null。
- 下拉框头部的完整有边框控件都是有效可交互区，包括右侧下拉箭头区域；只要十字中心在边框内，不得仅因靠近箭头而判定未命中。
- 标记落在相邻菜单、相邻按钮、文字边缘、滚动条、页面主体或目标外部时必须 false。
- scroll/move 的目标若是下拉弹层，标记中心必须位于弹层内容区域，不得位于页面主体。
- 若目标清楚可见但标记未命中，必须按提示中的实际完整截图尺寸计算目标可交互区域几何中心，填入 suggested_x/suggested_y；若目标不可见或无法可靠定位则填 null。候选坐标不会直接执行，仍须下一帧红色十字复验。
- 不得因为动作参数声称某个 target 就相信它；必须以截图位置为准。
- evidence 至少给出一个截图中的具体锚点；有歧义时 fail closed。
"""



class RPAgent:
    """RPA 智能体，执行观察-决策-执行ui循环"""

    def __init__(
        self,
        vnc_host: str = "localhost",
        vnc_port: int = 5901,
        vnc_password: str = "123456",
        vnc_user: str = "",
        system: str = SYSTEM,
        local_sendinput: bool | None = None,
    ):
        self.vnc = VNCClient(
            host=vnc_host,
            port=vnc_port,
            password=vnc_password,
            user=vnc_user,
            system=system,
            local_sendinput=local_sendinput,
        )
        self.system = self.vnc.system
        self.system_prompt = _prompt_for_system(SYSTEM_PROMPT, self.system)
        self.system_prompt_planning = _prompt_for_system(
            SYSTEM_PROMPT_PLANNING, self.system
        )
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
        self.pending_input = None      # 等待下一帧结构化验收的字段
        self.input_retry_count = 0
        self.ime_operation_counts = {}
        self.failure_reason = ""
        self._abort_requested = False
        self.login_progress = "idle"
        self._active_login_input_stage = None
        self._active_skill_action = None
        self._last_focus_coordinates = None
        self._last_skill_expand_error = ""
        self.task_policy = QueryOnlyPolicy.for_task("")
        self.interaction_guard = InteractionGuard(coordinate_grid=50)
        self._executed_observations = []
        self._loop_block_counts = {}
        self._scroll_counts = {}
        self._stale_block_count = 0
        self._last_state_verification = None
        self._planning_verification_failures = 0
        self._query_login_entry_verified = False
        self._query_search_click_count = 0
        self._query_last_search_evidence = None
        self._query_target_block_count = 0
        self._preplan_scope_block_count = 0
        self._query_maximize_count = 0
        self._app_launch_recovery = None
        self._app_launch_redirect_count = 0

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
    def _parse_json_object(response: str) -> dict | None:
        cleaned = str(response or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)
        try:
            value = json.loads(cleaned)
            return value if isinstance(value, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    def _verify_visible_state(
        self,
        screenshot: Image.Image,
        *,
        objective: str,
        expected_result: str,
    ) -> dict:
        prompt = f"""请独立验收当前截图。
操作目标：{objective}
必须满足的可见结果：{expected_result}

仅根据截图返回 JSON。"""
        try:
            response = chat_vision(
                prompt,
                screenshot,
                system_prompt=STATE_VERIFIER_PROMPT,
            )
            parsed = self._parse_json_object(response)
        except Exception as exc:
            parsed = None
            error = str(exc)
        else:
            error = ""

        if parsed is None:
            return {
                "passed": False,
                "state": "unknown",
                "evidence": [],
                "reason": f"验收器输出无效{(': ' + error) if error else ''}",
            }
        evidence = parsed.get("evidence")
        if not isinstance(evidence, list):
            evidence = []
        evidence = [str(item).strip() for item in evidence if str(item).strip()]
        passed = parsed.get("passed") is True and bool(evidence)
        return {
            "passed": passed,
            "state": str(parsed.get("state", "unknown") or "unknown"),
            "evidence": evidence,
            "reason": str(parsed.get("reason", "") or ""),
        }

    def _verify_query_pointer_target(
        self,
        screenshot: Image.Image,
        action_data: dict,
    ) -> dict:
        """Independently confirm a query pointer lands inside its declared target."""
        params = action_data.get("params", {})
        if not isinstance(params, dict):
            params = {}
        x_norm, y_norm = validate_normalized_coordinates(params)
        target = str(params.get("target", "") or "")
        compact_target = re.sub(
            r"[\s:：,，_\-/\\]+", "", target.lower().strip()
        )
        direct_logout_target = (
            "确认" not in compact_target
            and any(label in compact_target for label in (
                "退出登录图标", "直接退出登录按钮", "直接退出按钮", "顶部栏退出",
            ))
        )
        if (
            str(action_data.get("action", "") or "").lower().strip() == "click"
            and direct_logout_target
        ):
            logout_bbox = self._detect_direct_logout_button(screenshot)
            if logout_bbox is not None:
                x_min, y_min, x_max, y_max = logout_bbox
                inside = x_min <= x_norm <= x_max and y_min <= y_norm <= y_max
                return {
                    "passed": inside,
                    "target_visible": True,
                    "marker_inside_target": inside,
                    "geometry_inside_target": inside,
                    "local_zoom_confirmed": False,
                    "target_bbox": list(logout_bbox),
                    "suggested_x": None if inside else (x_min + x_max) / 2,
                    "suggested_y": None if inside else (y_min + y_max) / 2,
                    "evidence": [
                        "本地像素检测到绿色顶部栏最右侧的浅色门框/向右箭头图标"
                    ],
                    "reason": (
                        "本地确定性几何确认落点位于直接退出按钮内"
                        if inside
                        else "直接退出按钮可见，但当前落点不在其本地检测边界内"
                    ),
                }
        marked = screenshot.convert("RGB").copy()
        px = round(x_norm * (marked.width - 1))
        py = round(y_norm * (marked.height - 1))
        radius = max(8, min(18, round(min(marked.size) * 0.015)))
        draw = ImageDraw.Draw(marked)
        draw.ellipse(
            (px - radius, py - radius, px + radius, py + radius),
            outline=(255, 0, 0),
            width=3,
        )
        draw.line((px - radius, py, px + radius, py), fill=(255, 0, 0), width=2)
        draw.line((px, py - radius, px, py + radius), fill=(255, 0, 0), width=2)
        current = (
            self.task_plan[self.current_plan_idx]
            if 0 <= self.current_plan_idx < len(self.task_plan)
            else {}
        )
        prompt = (
            f"拟执行动作：{action_data.get('action', '')}\n"
            f"声明目标：{target}\n"
            f"当前固定计划步骤：{current.get('content', '登录前阶段')}\n"
            f"原始完整截图尺寸：{marked.width}×{marked.height} 像素；"
            f"红色十字中心：像素 ({px}, {py})，归一化 ({x_norm:.4f}, {y_norm:.4f})。\n"
            "坐标包含浏览器标题栏及截图中的桌面区域；不得按常见分辨率猜测画布高度。\n"
            "请确认红色十字中心是否真的位于声明目标内部。"
        )
        normalized_target = target.lower().strip()
        if "体验中心" in normalized_target:
            prompt += (
                "\n导航锚点提示：平台首页左侧顶级菜单通常按“渠道管理 → 体验中心 → "
                "系统配置”纵向排列；必须逐行读取，不能把三行中的中间一行误认成渠道管理。"
            )
        elif "智能体能力" in normalized_target:
            prompt += (
                "\n导航锚点提示：展开“体验中心”后，其下会出现“智能体应用”和“智能体能力”"
                "两个缩进子项；必须根据红色十字所在行逐字区分。"
            )
        if any(word in normalized_target for word in ("退出", "注销")):
            prompt += (
                "\n退出控件锚点提示：本系统绿色顶部栏最右侧、用户姓名“周昊”及其"
                "下拉箭头的右边，有一个始终可见的白色门框/向右箭头图标；它是无文字的"
                "直接退出登录按钮，不需要先展开用户菜单。必须核对红色十字是否位于该"
                "图标按钮区域，不能因没有“退出登录”文字就声称目标不可见。"
            )
        if (
            str(action_data.get("action", "") or "").lower().strip() == "scroll"
            and any(word in target.lower() for word in ("测试类型", "下拉弹层"))
        ):
            prompt += (
                "\n本页面的测试类型弹层已知可见锚点包括：文本结构化增强、图像文字提取、"
                "图文理解、关键词提取、问答提取、摘要提取、追问。"
                "这些是测试类型而不是渠道。滚动验收只判断弹层容器及红色落点；"
                "目标项“智能问数/新SQL生成”本来就可能在滚动前不可见，"
                "不得因目标项暂不可见而否定弹层身份。"
            )
        try:
            response = chat_vision(
                prompt,
                marked,
                system_prompt=QUERY_POINTER_VERIFIER_PROMPT,
                temperature=0.0,
                image_format="PNG",
            )
            parsed = self._parse_json_object(response)
        except Exception as exc:
            parsed = None
            error = str(exc)
        else:
            error = ""
        compact_declared_target = re.sub(
            r"[\s:：,，_\-/\\]+", "", normalized_target
        )
        dropdown_header_target = compact_declared_target in {
            "测试类型", "请选择测试类型", "测试类型下拉", "测试类型下拉框",
            "测试类型控件", "测试类型选择框", "测试类型筛选框",
        }
        preliminary_bbox = (
            self._parse_normalized_target_bbox(parsed.get("target_bbox"))
            if isinstance(parsed, dict)
            else None
        )
        preliminary_inside = bool(
            preliminary_bbox is not None
            and preliminary_bbox[0] <= x_norm <= preliminary_bbox[2]
            and preliminary_bbox[1] <= y_norm <= preliminary_bbox[3]
        )
        local_zoom_confirmed = False
        # Full-screen screenshots are required for context, but narrow sidebar
        # labels can still be too small for the verifier. If it says the target
        # is invisible or the marker misses it, retry once on a magnified
        # marker-centered crop. The
        # crop's structured geometry is then mapped back to the original frame;
        # no coordinate is executed directly from model prose.
        if isinstance(parsed, dict) and (
            any(word in normalized_target for word in (
                "智能问数", "新sql生成", "新SQL生成", "体验中心", "智能体能力", "登录",
                "密码", "用户名", "账号", "输入框",
            ))
            or parsed.get("target_visible") is not True
            or (
                parsed.get("marker_inside_target") is not True
                and not (
                    dropdown_header_target
                    and parsed.get("target_visible") is True
                    and preliminary_inside
                )
            )
        ):
            radius_x = max(140, round(marked.width * 0.16))
            radius_y = max(100, round(marked.height * 0.14))
            left = max(0, px - radius_x)
            top = max(0, py - radius_y)
            right = min(marked.width, px + radius_x + 1)
            bottom = min(marked.height, py + radius_y + 1)
            local = marked.crop((left, top, right, bottom))
            scale = max(2, min(4, round(900 / max(local.size))))
            zoomed = local.resize(
                (local.width * scale, local.height * scale),
                Image.Resampling.LANCZOS,
            )
            local_marker_x = (px - left) * scale
            local_marker_y = (py - top) * scale
            local_prompt = (
                f"拟执行动作：{action_data.get('action', '')}\n"
                f"声明目标：{target}\n"
                f"当前固定计划步骤：{current.get('content', '登录前阶段')}\n"
                f"这是原图区域 x={left}..{right - 1}, y={top}..{bottom - 1} "
                f"的 {scale} 倍局部放大图，当前图尺寸 {zoomed.width}×{zoomed.height}；"
                f"红色十字中心在当前图像素 ({local_marker_x}, {local_marker_y})。\n"
                "只按当前局部放大图读取目标文字与红点所在行。target_bbox 和 "
                "suggested_x/y 必须按当前局部图以 x/(width-1)、y/(height-1) "
                "归一化；不得沿用原图的像素或归一化坐标，也不得相信 target 声明本身。"
            )
            if any(word in normalized_target for word in ("密码", "用户名", "账号", "输入框")):
                local_prompt += (
                    "\n输入框有时只有底部横线。可交互行应围绕占位文字/已输入文字和对应图标，"
                    "不要把上一行底线与本行文字之间的留白算入控件。红点必须贴近文字行的"
                    "垂直中心，不能在两行之间。若未命中，suggested_x/y 返回本行文字区域中心。"
                )
            if "体验中心" in normalized_target:
                local_prompt += (
                    "\n逐行区分渠道管理、体验中心、系统配置；目标是中间的体验中心。"
                )
            elif "智能体能力" in normalized_target:
                local_prompt += (
                    "\n逐行区分缩进的智能体应用与智能体能力；目标是下方的智能体能力。"
                )
            if any(word in normalized_target for word in ("退出", "注销")):
                local_prompt += (
                    "\n绿色顶部栏中，用户姓名“周昊”右侧的白色门框/向右箭头图标"
                    "就是无文字的直接退出登录按钮；不要求出现菜单或文字标签。"
                )
            try:
                local_response = chat_vision(
                    local_prompt,
                    zoomed,
                    system_prompt=QUERY_POINTER_VERIFIER_PROMPT,
                    temperature=0.0,
                    image_format="PNG",
                )
                local_parsed = self._parse_json_object(local_response)
            except Exception:
                local_parsed = None
            if isinstance(local_parsed, dict) and local_parsed.get("target_visible") is True:
                local_bbox = self._parse_normalized_target_bbox(
                    local_parsed.get("target_bbox")
                )

                def to_full_x(value: float) -> float:
                    return (left + value * max(1, local.width - 1)) / max(
                        1, marked.width - 1
                    )

                def to_full_y(value: float) -> float:
                    return (top + value * max(1, local.height - 1)) / max(
                        1, marked.height - 1
                    )

                local_evidence = local_parsed.get("evidence")
                local_semantic_pass = bool(
                    local_parsed.get("passed") is True
                    and local_parsed.get("marker_inside_target") is True
                    and isinstance(local_evidence, list)
                    and any(str(item).strip() for item in local_evidence)
                )
                if local_semantic_pass:
                    # The enlarged crop is the independent second semantic
                    # check. Some vision models still emit bbox numbers in the
                    # old full-frame coordinate system despite the prompt; do
                    # not let those numbers negate an explicit, evidenced hit.
                    local_parsed["target_bbox"] = None
                    local_parsed["suggested_x"] = None
                    local_parsed["suggested_y"] = None
                    local_zoom_confirmed = True
                elif local_bbox is not None:
                    local_parsed["target_bbox"] = [
                        to_full_x(local_bbox[0]),
                        to_full_y(local_bbox[1]),
                        to_full_x(local_bbox[2]),
                        to_full_y(local_bbox[3]),
                    ]
                if not local_semantic_pass:
                    try:
                        local_sx, local_sy = validate_normalized_coordinates({
                            "x": local_parsed.get("suggested_x"),
                            "y": local_parsed.get("suggested_y"),
                        })
                    except (InteractionGuardError, TypeError, ValueError):
                        local_parsed["suggested_x"] = None
                        local_parsed["suggested_y"] = None
                    else:
                        local_parsed["suggested_x"] = to_full_x(local_sx)
                        local_parsed["suggested_y"] = to_full_y(local_sy)
                local_reason = str(local_parsed.get("reason", "") or "")
                local_parsed["reason"] = (
                    "局部放大复核：" + local_reason
                    if local_reason
                    else "局部放大复核识别到声明目标"
                )
                parsed = local_parsed
        if not isinstance(parsed, dict):
            return {
                "passed": False,
                "target_visible": False,
                "marker_inside_target": False,
                "evidence": [],
                "reason": f"落点验收器输出无效{(': ' + error) if error else ''}",
            }
        evidence = parsed.get("evidence")
        if not isinstance(evidence, list):
            evidence = []
        evidence = [str(item).strip() for item in evidence if str(item).strip()]
        target_visible = parsed.get("target_visible") is True
        reported_marker_inside = parsed.get("marker_inside_target") is True
        target_bbox = self._parse_normalized_target_bbox(parsed.get("target_bbox"))
        geometry_inside = None
        if target_bbox is not None:
            x_min, y_min, x_max, y_max = target_bbox
            geometry_inside = (
                x_min <= x_norm <= x_max and y_min <= y_norm <= y_max
            )
            dropdown_header = dropdown_header_target
            # Structured geometry can safely correct the known dropdown-arrow
            # mistake because the entire bordered header is clickable. For a
            # concrete option/menu item, however, a bbox may itself be attached
            # to an adjacent row; require the semantic verifier to agree too.
            marker_inside = bool(
                geometry_inside
                and (reported_marker_inside or dropdown_header)
            )
        else:
            # Backwards-compatible fallback for verifier responses/tests that
            # predate structured geometry. New responses are instructed to
            # provide target_bbox so code, rather than prose, decides hit-test
            # geometry.
            marker_inside = reported_marker_inside
        suggested_x = None
        suggested_y = None
        if target_visible and not marker_inside:
            if target_bbox is not None and geometry_inside is False:
                suggested_x = (target_bbox[0] + target_bbox[2]) / 2
                suggested_y = (target_bbox[1] + target_bbox[3]) / 2
            else:
                try:
                    suggested_x, suggested_y = validate_normalized_coordinates({
                        "x": parsed.get("suggested_x"),
                        "y": parsed.get("suggested_y"),
                    })
                except (InteractionGuardError, TypeError, ValueError):
                    suggested_x = None
                    suggested_y = None
        passed = bool(
            target_visible
            and marker_inside
            and evidence
            and (target_bbox is not None or parsed.get("passed") is True)
        )
        reason = str(parsed.get("reason", "") or "")
        if target_bbox is not None and (
            marker_inside != reported_marker_inside
            or passed != (parsed.get("passed") is True)
            or (geometry_inside is True and not reported_marker_inside)
        ):
            if marker_inside:
                geometry_reason = "下拉头部 target_bbox 包含红色十字中心，代码确定性判定命中"
            elif geometry_inside and not reported_marker_inside:
                geometry_reason = (
                    "target_bbox 虽包含红色十字中心，但语义验收称其位于相邻目标；"
                    "具体选项按保守策略判定未命中"
                )
            else:
                geometry_reason = "结构化 target_bbox 不包含红色十字中心，代码确定性判定未命中"
            reason = f"{geometry_reason}；验收器原始理由：{reason}" if reason else geometry_reason
        return {
            "passed": passed,
            "target_visible": target_visible,
            "marker_inside_target": marker_inside,
            "geometry_inside_target": geometry_inside,
            "local_zoom_confirmed": local_zoom_confirmed,
            "target_bbox": list(target_bbox) if target_bbox is not None else None,
            "suggested_x": suggested_x,
            "suggested_y": suggested_y,
            "evidence": evidence,
            "reason": reason,
        }

    @staticmethod
    def _detect_direct_logout_button(
        screenshot: Image.Image,
    ) -> tuple[float, float, float, float] | None:
        """Locate this application's icon-only logout button without an LLM."""
        image = screenshot.convert("RGB")
        width, height = image.size
        if width < 200 or height < 160:
            return None

        def is_header_green(pixel: tuple[int, int, int]) -> bool:
            red, green, blue = pixel
            return (
                green >= 100
                and green - red >= 25
                and green - blue >= 10
            )

        top_limit = max(1, min(height, round(height * 0.25)))
        row_threshold = max(20, round(width * 0.55))
        qualifying_rows = []
        for y_pos in range(top_limit):
            green_count = sum(
                1
                for x_pos in range(width)
                if is_header_green(image.getpixel((x_pos, y_pos)))
            )
            if green_count >= row_threshold:
                qualifying_rows.append(y_pos)

        row_runs: list[list[int]] = []
        for y_pos in qualifying_rows:
            if not row_runs or y_pos != row_runs[-1][-1] + 1:
                row_runs.append([y_pos])
            else:
                row_runs[-1].append(y_pos)
        row_runs = [
            run
            for run in row_runs
            if len(run) >= max(12, round(height * 0.015))
        ]
        if not row_runs:
            return None
        header_run = max(row_runs, key=len)
        top, bottom = header_run[0], header_run[-1]
        band_height = bottom - top + 1

        column_threshold = max(4, round(band_height * 0.55))
        qualifying_columns = []
        for x_pos in range(width):
            green_count = sum(
                1
                for y_pos in range(top, bottom + 1)
                if is_header_green(image.getpixel((x_pos, y_pos)))
            )
            if green_count >= column_threshold:
                qualifying_columns.append(x_pos)
        if not qualifying_columns:
            return None
        header_left = min(qualifying_columns)
        header_right = max(qualifying_columns)
        if header_right < round(width * 0.75):
            return None

        search_left = max(
            header_left,
            header_right - round(band_height * 1.3),
        )

        def is_light_icon(pixel: tuple[int, int, int]) -> bool:
            red, green, blue = pixel
            return red >= 110 and green >= 204 and blue >= 90

        remaining = {
            (x_pos, y_pos)
            for y_pos in range(top, bottom + 1)
            for x_pos in range(search_left, header_right + 1)
            if is_light_icon(image.getpixel((x_pos, y_pos)))
        }
        components: list[set[tuple[int, int]]] = []
        while remaining:
            seed = remaining.pop()
            component = {seed}
            stack = [seed]
            while stack:
                x_pos, y_pos = stack.pop()
                for x_delta in (-1, 0, 1):
                    for y_delta in (-1, 0, 1):
                        neighbor = (x_pos + x_delta, y_pos + y_delta)
                        if neighbor in remaining:
                            remaining.remove(neighbor)
                            component.add(neighbor)
                            stack.append(neighbor)
            components.append(component)

        icon_components = []
        for component in components:
            x_values = [point[0] for point in component]
            y_values = [point[1] for point in component]
            component_width = max(x_values) - min(x_values) + 1
            component_height = max(y_values) - min(y_values) + 1
            if (
                len(component) >= 8
                and component_width >= 3
                and component_height >= 3
            ):
                icon_components.append(component)
        if not icon_components:
            return None

        icon_points = set().union(*icon_components)
        icon_x = [point[0] for point in icon_points]
        icon_y = [point[1] for point in icon_points]
        # Only the icon is clickable on this page, not its green surroundings.
        padding = 0
        x_min = max(search_left, min(icon_x) - padding)
        y_min = max(top, min(icon_y) - padding)
        x_max = min(header_right, max(icon_x) + padding)
        y_max = min(bottom, max(icon_y) + padding)
        if x_max <= x_min or y_max <= y_min:
            return None
        return (
            x_min / max(1, width - 1),
            y_min / max(1, height - 1),
            x_max / max(1, width - 1),
            y_max / max(1, height - 1),
        )

    @staticmethod
    def _parse_normalized_target_bbox(value) -> tuple[float, float, float, float] | None:
        """Return a bounded, tight normalized target box or None."""
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        if any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in value
        ):
            return None
        coords = tuple(float(item) for item in value)
        if not all(math.isfinite(item) and 0.0 <= item <= 1.0 for item in coords):
            return None
        x_min, y_min, x_max, y_max = coords
        width = x_max - x_min
        height = y_max - y_min
        if width <= 0.0 or height <= 0.0:
            return None
        # Reject screenshot-sized boxes: they do not tightly identify the
        # declared control and could otherwise turn visibility into a click.
        if width > 0.85 or height > 0.85 or width * height > 0.35:
            return None
        return coords

    @staticmethod
    def _validate_xy(params: dict) -> tuple:
        """校验归一化坐标合法性，返回 (是否有效, 原因)"""
        try:
            validate_normalized_coordinates(params)
        except (InteractionGuardError, TypeError, ValueError) as exc:
            return False, str(exc)
        return True, ""

    @staticmethod
    def _validate_drag(params: dict) -> tuple:
        try:
            validate_normalized_coordinates(params, x_key="start_x", y_key="start_y")
            validate_normalized_coordinates(params, x_key="end_x", y_key="end_y")
        except (InteractionGuardError, TypeError, ValueError) as exc:
            return False, str(exc)
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
        if a2 in ("type", "input_text", "retry_text_input"):
            return (
                p1.get("text", "") == p2.get("text", "")
                and p1.get("field_type", "auto") == p2.get("field_type", "auto")
            )
        if a2 == "press":
            return p1.get("key", "") == p2.get("key", "")
        if a2 == "ime_operation":
            return (
                p1.get("operation", "") == p2.get("operation", "")
                and p1.get("candidate") == p2.get("candidate")
            )
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

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").lower().strip() in {"1", "true", "yes", "是"}

    @staticmethod
    def _same_input_text(expected: str, observed: str) -> bool:
        """按 Unicode NFC 后逐字符比较，不做 trim 或模糊匹配。"""
        return unicodedata.normalize("NFC", str(expected)) == unicodedata.normalize(
            "NFC", str(observed)
        )

    @staticmethod
    def _is_one_edit_apart(expected: str, observed: str) -> bool:
        """判断两个 NFC 字符串是否恰好相差一次插入、删除或替换。"""
        left = unicodedata.normalize("NFC", str(expected))
        right = unicodedata.normalize("NFC", str(observed))
        if left == right or not left or not right or abs(len(left) - len(right)) > 1:
            return False
        if len(left) == len(right):
            return sum(a != b for a, b in zip(left, right)) == 1
        if len(left) < len(right):
            left, right = right, left
        # left 比 right 长一个字符；跳过 left 中至多一个字符后其余必须一致。
        i = j = differences = 0
        while i < len(left) and j < len(right):
            if left[i] == right[j]:
                i += 1
                j += 1
                continue
            differences += 1
            i += 1
            if differences > 1:
                return False
        differences += len(left) - i
        return differences == 1

    def _clear_pending_input(self) -> None:
        self.pending_input = None
        self.input_retry_count = 0
        self.ime_operation_counts = {}

    @staticmethod
    def _compact_visible_target(value) -> str:
        return re.sub(
            r"[\s:：,，;；_\-/\\>→（）()\[\]【】'\"]+",
            "",
            str(value or "").lower().strip(),
        )

    @classmethod
    def _browser_application(cls, value) -> str:
        compact = cls._compact_visible_target(value)
        if compact in {"edge", "msedge", "microsoftedge"}:
            return "edge"
        if compact in {"chrome", "googlechrome"}:
            return "chrome"
        return ""

    @classmethod
    def _is_app_launch_recovery_target(cls, application, target) -> bool:
        """Bind a Start-menu fallback to the exact browser originally requested."""
        browser = cls._browser_application(application)
        compact = cls._compact_visible_target(target)
        allowed = {
            "edge": {
                "microsoftedge最佳匹配",
                "microsoftedge最佳匹配项",
                "microsoftedge应用最佳匹配",
                "microsoftedge最佳匹配应用",
            },
            "chrome": {
                "googlechrome最佳匹配",
                "googlechrome最佳匹配项",
                "googlechrome应用最佳匹配",
                "googlechrome最佳匹配应用",
            },
        }
        return bool(browser and compact in allowed[browser])

    @classmethod
    def _is_raw_browser_icon_target(cls, target) -> bool:
        """Recognize only explicitly named Edge/Chrome launch icons."""
        return cls._compact_visible_target(target) in {
            "edge图标",
            "microsoftedge图标",
            "桌面edge图标",
            "桌面microsoftedge图标",
            "edge桌面图标",
            "microsoftedge桌面图标",
            "任务栏edge图标",
            "任务栏microsoftedge图标",
            "edge任务栏图标",
            "microsoftedge任务栏图标",
            "chrome图标",
            "googlechrome图标",
            "桌面chrome图标",
            "桌面googlechrome图标",
            "chrome桌面图标",
            "googlechrome桌面图标",
            "任务栏chrome图标",
            "任务栏googlechrome图标",
            "chrome任务栏图标",
            "googlechrome任务栏图标",
        }

    @classmethod
    def _browser_icon_application(cls, target) -> str:
        compact = cls._compact_visible_target(target)
        if compact in {
            "edge图标", "microsoftedge图标",
            "桌面edge图标", "桌面microsoftedge图标",
            "edge桌面图标", "microsoftedge桌面图标",
            "任务栏edge图标", "任务栏microsoftedge图标",
            "edge任务栏图标", "microsoftedge任务栏图标",
        }:
            return "edge"
        if compact in {
            "chrome图标", "googlechrome图标",
            "桌面chrome图标", "桌面googlechrome图标",
            "chrome桌面图标", "googlechrome桌面图标",
            "任务栏chrome图标", "任务栏googlechrome图标",
            "chrome任务栏图标", "googlechrome任务栏图标",
        }:
            return "chrome"
        return ""

    def _is_armed_app_launch_recovery(self, action: str, params: dict) -> bool:
        recovery = getattr(self, "_app_launch_recovery", None)
        return bool(
            isinstance(recovery, dict)
            and recovery.get("remaining_clicks") == 1
            and int(recovery.get("wait_count", 0)) <= 1
            and not self.task_plan
            and str(action or "").lower().strip() == "click"
            and self._is_app_launch_recovery_target(
                recovery.get("application", ""),
                (params or {}).get("target", ""),
            )
        )

    @staticmethod
    def _has_windows_ime_candidate_panel(screenshot: Image.Image | None) -> bool:
        """Conservatively detect the shallow Microsoft IME candidate rectangle.

        This is used while verifying Windows application search and URL fields. It
        guards against a vision response claiming ``ime_visible=false`` even
        though the numbered candidate strip is still open below Start search.
        """
        if screenshot is None or not isinstance(screenshot, Image.Image):
            return False
        try:
            image = screenshot.convert("RGB")
            width, height = image.size
            if width < 320 or height < 240:
                return False
            pixels = image.load()
            min_run = max(180, int(width * 0.28))
            y_start = max(1, int(height * 0.04))
            y_stop = min(height - 25, int(height * 0.26))

            def border(pixel) -> bool:
                return max(pixel) - min(pixel) <= 3 and 218 <= pixel[0] <= 236

            def fill(pixel) -> bool:
                return max(pixel) - min(pixel) <= 4 and 246 <= pixel[0] <= 253

            for top in range(y_start, y_stop):
                start = None
                runs = []
                for x in range(width + 1):
                    matches = x < width and border(pixels[x, top])
                    if matches and start is None:
                        start = x
                    elif not matches and start is not None:
                        if x - start >= min_run:
                            runs.append((start, x - 1))
                        start = None
                for left, right in runs:
                    span = right - left + 1
                    for bottom in range(
                        top + max(18, int(height * 0.025)),
                        min(top + max(24, int(height * 0.065)), height - 1) + 1,
                    ):
                        sample_step = max(1, span // 160)
                        xs = range(left, right + 1, sample_step)
                        bottom_ratio = sum(
                            1 for x in xs if border(pixels[x, bottom])
                        ) / max(1, len(range(left, right + 1, sample_step)))
                        if bottom_ratio < 0.70:
                            continue
                        interior_y = min(bottom - 1, top + 2)
                        interior_xs = range(left + 2, right - 1, sample_step)
                        interior_values = list(interior_xs)
                        if not interior_values:
                            continue
                        fill_ratio = sum(
                            1 for x in interior_values if fill(pixels[x, interior_y])
                        ) / len(interior_values)
                        if fill_ratio < 0.70:
                            continue
                        side_ys = range(top, bottom + 1, max(1, (bottom - top) // 12))
                        side_values = list(side_ys)
                        side_ratio = sum(
                            int(border(pixels[left, y])) + int(border(pixels[right, y]))
                            for y in side_values
                        ) / (2 * len(side_values))
                        if side_ratio >= 0.70:
                            return True
            return False
        except (OSError, TypeError, ValueError):
            return False

    @staticmethod
    def _normalize_input_cause(cause) -> str:
        """把模型给出的纠错原因收敛为可比较的有限集合。"""
        value = str(cause or "").lower().strip()
        aliases = {
            "ime": "ime",
            "input_method": "ime",
            "中文输入法": "ime",
            "mode": "ime",
            "focus": "focus",
            "unfocused": "focus",
            "no_input": "focus",
            "未聚焦": "focus",
            "无输入": "focus",
            "partial": "partial",
            "missing": "partial",
            "缺字": "partial",
            "format": "format",
            "fullwidth": "format",
            "格式": "format",
            "unknown": "unknown",
        }
        return aliases.get(value, "")

    def _read_focused_ime_mode(self) -> str:
        """Read a fresh frame after focus changes; never reuse another app's mode."""
        time.sleep(0.2)
        frame = self.vnc.screenshot()
        # Full-desktop downscaling makes the tiny 中/英 glyph disappear. Read
        # the notification area at native resolution with a magnified fallback.
        tray = frame.crop((int(frame.width * 0.65), int(frame.height * 0.88),
                           frame.width, frame.height))
        tray = tray.resize((tray.width * 3, tray.height * 3))
        response = chat_vision(
            "这是Windows任务栏右下角通知区域的放大截图。识别当前输入法的实际中英文状态。仅依据"
            "清楚可见的中/英/A/ENG标志。中文输入法名称、键盘图标和已有英文文本"
            "不代表英文模式。看不到明确状态标志时必须返回unknown。"
            '只返回JSON: {"mode":"english/chinese/unknown","evidence":"可见状态标志及位置"}。',
            tray,
            system_prompt="只读屏幕中的输入法状态，不推测目标文本需要的语言。",
            temperature=0.0, image_format="PNG",
        )
        result = self._parse_json_object(response) or {}
        mode = str(result.get("mode", "")).lower().strip()
        return mode if mode in {"english", "chinese"} and result.get("evidence") else "unknown"

    def _input_once(
        self,
        text,
        field_type: str = "auto",
        replace: bool = True,
    ) -> bool:
        """执行一次字段输入；成功后必须由下一帧截图验收。"""
        field = str(field_type or "auto").lower().strip()
        try:
            value = self.vnc.ime.normalize_text(text, field)
        except Exception as exc:
            self.failure_reason = f"字段 {field} 的输入参数无效：{exc}"
            self._repeat_guidance = self.failure_reason
            return False

        if self.pending_input:
            description = (
                f"密码字段（目标长度 {len(self.pending_input.get('text', ''))}）"
                if self.pending_input.get("field_type") == "password"
                else f"字段 {self.pending_input.get('field_type', 'auto')}"
            )
            self.failure_reason = f"{description} 尚未离开，已阻止重复或跨字段输入"
            self._repeat_guidance = (
                self.failure_reason
                + "。若尚未验收请调用 verify_text_input；验收通过后再点击下一个字段。"
            )
            return False

        if field == "url" and str(self.system).lower() in {"win", "windows", "win32"}:
            try:
                self.vnc.ime.ensure_english(self._read_focused_ime_mode)
            except Exception as exc:
                self.failure_reason = f"字段 {field} 输入前准备失败：{exc}"
                self._repeat_guidance = self.failure_reason + "。本步未清空或输入网址。"
                self._abort_requested = True
                return False
        try:
            value, mode = self.vnc.ime.type_value(value, field, replace=bool(replace))
        except Exception as exc:
            self.failure_reason = f"字段 {field} 输入失败：{exc}"
            self._repeat_guidance = (
                self.failure_reason
                + "。键盘传输可能已产生部分文本，任务已安全停止；"
                "不要重复首次输入或改用剪贴板。"
            )
            self._abort_requested = True
            print(f"  [输入] {self.failure_reason}")
            return False

        focus_coordinates = self._last_focus_coordinates
        self.pending_input = {
            "text": value,
            "field_type": field,
            "expected_mode": mode,
            "verified": False,
            "verification_attempted": False,
            "replace": bool(replace),
            "focus_coordinates": focus_coordinates,
            "login_stage": self._active_login_input_stage,
            "source_skill": getattr(self, "_active_skill_action", None),
        }
        self._last_focus_coordinates = None
        self.input_retry_count = 0
        self.ime_operation_counts = {}
        self.failure_reason = ""
        target = (
            f"密码字段（目标长度 {len(value)}）"
            if field == "password"
            else f"字段 {field} 的目标文本 {value!r}"
        )
        self._repeat_guidance = (
            f"{target} 已输入但尚未验收；下一步必须根据新截图调用 verify_text_input，"
            "禁止直接提交或切换字段。"
        )
        return True

    def _retry_input(self, text, field_type: str, cause: str) -> bool:
        """根据截图诊断全选替换字段；同一字段最多执行一次。"""
        pending = self.pending_input or {}
        if not pending or pending.get("verified") is True:
            self.failure_reason = "当前没有待纠错的字段输入"
            self._repeat_guidance = self.failure_reason
            return False

        field = str(field_type or pending.get("field_type") or "auto").lower().strip()
        try:
            value = self.vnc.ime.normalize_text(text, field)
        except Exception as exc:
            self.failure_reason = f"字段 {field} 的纠错参数无效：{exc}"
            self._repeat_guidance = self.failure_reason
            return False

        if value != pending.get("text") or field != pending.get("field_type"):
            self.failure_reason = "纠错目标与上一条待验收输入不一致，已停止以避免修改错误字段"
            self._repeat_guidance = self.failure_reason
            return False
        if self.input_retry_count >= 1:
            self.failure_reason = f"字段 {field} 经一次针对性纠错后仍未正确，已停止重复输入"
            self._repeat_guidance = self.failure_reason + "；任务已停止，未执行提交。"
            self._abort_requested = True
            self._clear_pending_input()
            return False

        if not pending.get("verification_attempted"):
            self.failure_reason = "尚未根据新截图验收当前字段，已阻止未经诊断的重输"
            self._repeat_guidance = self.failure_reason + "；请先调用 verify_text_input。"
            return False

        normalized_cause = self._normalize_input_cause(cause)
        recorded_cause = self._normalize_input_cause(pending.get("cause"))
        if normalized_cause not in {"ime", "focus", "partial", "format"}:
            self.failure_reason = f"字段输入失败原因 {cause!r} 无法识别，已停止盲目重试"
            self._repeat_guidance = self.failure_reason
            return False
        if normalized_cause != recorded_cause:
            self.failure_reason = (
                f"纠错原因 {normalized_cause!r} 与截图验收记录 {recorded_cause!r} 不一致，"
                "已阻止未经证实的重输"
            )
            self._repeat_guidance = self.failure_reason
            return False
        if normalized_cause == "focus" and not pending.get("refocused"):
            self.failure_reason = "截图显示输入框未聚焦；请先单独点击原字段，再执行一次纠错"
            self._repeat_guidance = self.failure_reason
            return False

        ime_was_switched = any(
            signature.startswith(("toggle_language:", "cycle_layout:"))
            for signature in self.ime_operation_counts
        )
        try:
            value, mode = self.vnc.ime.type_value(
                value,
                field,
                replace=True,
                switch_language=normalized_cause == "ime" and not ime_was_switched,
                cancel_composition=normalized_cause == "ime",
            )
        except Exception as exc:
            # 失败前不更新 pending 或重试计数，避免把未完成动作伪装成成功。
            # 底层异常仍可能发生在部分按键已发送之后，因此必须 fail-stop。
            self.failure_reason = f"字段 {field} 的针对性纠错失败：{exc}"
            self._repeat_guidance = (
                self.failure_reason + "。字段内容已处于未知状态，任务已安全停止。"
            )
            self._abort_requested = True
            print(f"  [输入纠错] {self.failure_reason}")
            return False

        self.pending_input = {
            "text": value,
            "field_type": field,
            "expected_mode": mode,
            "verified": False,
            "verification_attempted": False,
            "replace": True,
            "focus_coordinates": pending.get("focus_coordinates"),
            "login_stage": pending.get("login_stage"),
            "source_skill": pending.get("source_skill"),
        }
        self._last_focus_coordinates = None
        self.input_retry_count += 1
        self._repeat_guidance = "字段已完成唯一一次纠错；下一步只能重新截图并验收，禁止再次输入。"
        return True

    def _verify_pending_input(
        self,
        params: dict,
        screenshot: Image.Image | None = None,
    ) -> bool:
        """记录截图中的字段值；返回值表示验收动作本身是否合法。"""
        pending = self.pending_input
        if not pending:
            self.failure_reason = "当前没有待验收的字段输入"
            self._repeat_guidance = self.failure_reason
            return False

        expected = str(pending.get("text", ""))
        field = str(pending.get("field_type", "auto"))
        readable = self._as_bool(params.get("readable", False))
        observed = str(params.get("observed_text", ""))
        cause = self._normalize_input_cause(params.get("cause", "unknown")) or "unknown"
        ime_visible = self._as_bool(params.get("ime_visible", False))

        if field == "url" and screenshot is not None:
            # Do not show the expected URL/history to the reader: the action
            # model has repeatedly echoed the intended text instead of the UI.
            try:
                transcription = self._parse_action(chat_vision(
                    "仅抄录浏览器地址栏（返回/刷新按钮右侧的长输入框）当前实际显示的完整文字。"
                    "不要抄录最上方标签页的标题/截断网址，也不要抄录下方搜索建议。"
                    "保留中文、空格、全角标点和组合串，不要补全或修正为网址。"
                    "只返回 JSON: {\"observed_text\":\"实际文字\",\"readable\":true,"
                    "\"ime_visible\":false}。看不清则 readable=false；"
                    "有输入法候选条或未提交组合串则 ime_visible=true。",
                    screenshot.crop((0, 0, screenshot.width, max(1, int(screenshot.height * 0.25)))),
                    system_prompt="你是独立的屏幕文字抄录器，只报告图片中的文字，不推测用户意图。",
                    temperature=0.0, image_format="PNG",
                ))
                observed = str(transcription.get("observed_text", ""))
                readable = self._as_bool(transcription.get("readable", False))
                ime_visible = self._as_bool(transcription.get("ime_visible", False))
                if readable and (ime_visible or any(ord(c) > 127 for c in observed)):
                    ime_visible = True
                    pending["ime_evidence"] = "url_transcription_ime"
                    pending["ime_recovery_required"] = True
                elif readable and not self._same_input_text(expected, observed):
                    cause = "partial"
            except Exception:
                readable = False
                observed = ""

        local_ime_evidence = bool(
            field in {"app_search", "url"}
            and str(self.system or "").lower().strip() in {"win", "windows", "win32"}
            and self._has_windows_ime_candidate_panel(screenshot)
        )
        if local_ime_evidence:
            ime_visible = True
            pending["ime_evidence"] = "windows_candidate_panel"
            pending["ime_recovery_required"] = True
        elif pending.get("ime_recovery_required"):
            # Code-side evidence already proved this text was an uncommitted
            # composition. Waiting for the strip to disappear is not a repair;
            # retry_text_input must create a fresh pending record.
            ime_visible = True

        if ime_visible:
            cause = "ime"
        elif (
            field != "password"
            and cause == "ime"
            and readable
            and self._same_input_text(expected, observed)
        ):
            # 模型可能沿用上一帧的 IME 原因；没有候选/组合串且文本已经精确
            # 一致时，主观 cause 不能推翻当前帧的直接可见证据。
            cause = "unknown"

        if field == "password":
            visible = observed.strip()
            mask_characters = {"*", "•", "●", "·", "▪", "◦", "○"}
            pure_mask = bool(visible) and all(char in mask_characters for char in visible)
            mask_count = len(visible) if pure_mask else None
            verified = readable and cause != "ime" and (
                (not expected and not visible)
                or (pure_mask and mask_count == len(expected))
            )
            pending["mask_count"] = mask_count
        else:
            verified = (
                readable
                and self._same_input_text(expected, observed)
                and cause != "ime"
            )

        if not verified and cause == "unknown" and readable:
            if not observed:
                cause = "focus"
            elif observed in expected or expected.startswith(observed):
                cause = "partial"
            else:
                cause = "format"

        pending["verified"] = verified
        pending["verification_attempted"] = True
        pending["cause"] = cause
        pending["observed_text"] = observed if readable and field != "password" else ""
        if verified:
            pending.pop("visual_recheck_required", None)
            pending.pop("visual_recheck_waited", None)
            pending.pop("first_ambiguous_observed_text", None)
            login_stage = pending.get("login_stage")
            if login_stage == "username":
                self.login_progress = "username_verified"
            elif login_stage == "password":
                self.login_progress = "password_verified"
            self.failure_reason = ""
            self._repeat_guidance = "当前字段已逐字符验收通过；现在可以执行下一次点击、Tab 或提交。"
            print(f"  [输入验收] 字段 {field} 验收通过")
            return True

        target = (
            f"密码字段目标长度 {len(expected)}"
            if field == "password"
            else f"字段 {field} 期望 {expected!r}，截图实际为 {observed!r}"
        )
        transient_ambiguity = bool(
            field != "password"
            and not ime_visible
            and (
                (readable and self._is_one_edit_apart(expected, observed))
                or (not readable and self.input_retry_count >= 1)
            )
        )
        if transient_ambiguity and not pending.get("visual_recheck_required"):
            # 聚焦输入框的闪烁光标可能贴住末字符，让视觉模型把 o+| 看成 d。
            # 第一次近似转写不能触发再次输入或终止；只允许换一个光标相位复核。
            pending["visual_recheck_required"] = True
            pending["visual_recheck_waited"] = False
            pending["first_ambiguous_observed_text"] = observed
            self._repeat_guidance = (
                f"{target}，但两者仅相差一个字符，可能是闪烁插入光标、选区或"
                "拼写下划线造成的瞬态视觉误读。禁止重输、切换输入法或提交；"
                "下一步只能 wait(seconds=0.6)，再用新截图调用 verify_text_input。"
            )
            return True
        if pending.get("visual_recheck_required") and pending.get("visual_recheck_waited"):
            # 复核帧仍不一致：该歧义已被确认，不得继续困在复核门中。
            # 未用过纠错时回到正常的一次性 retry；已纠错时由下方终止。
            pending.pop("visual_recheck_required", None)
            pending.pop("visual_recheck_waited", None)
            pending.pop("first_ambiguous_observed_text", None)
        if self.input_retry_count >= 1:
            self.failure_reason = f"{target}，唯一一次纠错后仍未通过验收（原因 {cause}）"
            self._repeat_guidance = self.failure_reason + "；任务已停止，未执行提交。"
            self._abort_requested = True
            self._clear_pending_input()
            return True

        self._repeat_guidance = (
            f"{target}，验收未通过（原因 {cause}）。"
            "若截图不清晰先 wait；否则按真实原因调用 retry_text_input，同一字段仅允许一次。"
        )
        return True

    def _blocks_unverified_input(self, action: str, params: dict) -> bool:
        """有待验收字段时，阻止提交、跨字段和重复输入。"""
        pending = self.pending_input
        if not pending or pending.get("verified") is True:
            return False
        if pending.get("visual_recheck_required"):
            waited = bool(pending.get("visual_recheck_waited"))
            allowed = {"verify_text_input", "abort_input"} if waited else {"wait"}
            if action in allowed:
                return False
            phase = (
                "已经等待；现在只能根据新截图再次 verify_text_input"
                if waited
                else "必须先 wait(seconds=0.6) 获取不同的光标闪烁相位"
            )
            self._repeat_guidance = (
                "字段存在单字符视觉歧义，" + phase
                + "；已阻止重输、输入法切换、离开字段或提交。"
            )
            return True
        if action == "click" and pending.get("cause") == "focus":
            expected_coordinates = pending.get("focus_coordinates")
            try:
                x = float(params.get("x"))
                y = float(params.get("y"))
                close_to_original = bool(
                    expected_coordinates
                    and abs(x - float(expected_coordinates[0])) <= 0.03
                    and abs(y - float(expected_coordinates[1])) <= 0.03
                )
            except (TypeError, ValueError, IndexError):
                close_to_original = False
            if close_to_original and not pending.get("refocused"):
                return False
            self._repeat_guidance = (
                "待恢复字段只能重新点击首次聚焦位置附近；已阻止可能写入其他字段的点击。"
                "若原位置未知或字段已消失，请调用 abort_input。"
            )
            return True
        allowed = {
            "wait",
            "verify_text_input",
            "retry_text_input",
            "ime_operation",
            "skill_input_method",
            "skill_ime",
            "abort_input",
        }
        if action in allowed:
            return False
        field = str(pending.get("field_type", "auto"))
        target = (
            f"密码字段（目标长度 {len(pending.get('text', ''))}）"
            if field == "password"
            else f"字段 {field}"
        )
        self._repeat_guidance = (
            f"{target} 尚未通过结构化验收；已阻止 {action!r}。"
            "下一步只能 wait、verify_text_input，或按已确认原因执行一次 retry_text_input。"
        )
        print(f"  [输入验收防护] {self._repeat_guidance}")
        return True

    def _prepare_login_stage(self, params: dict) -> tuple[bool, str | None, str]:
        """校验登录阶段顺序，并返回 ``(可执行, stage, 之前状态)``。"""
        needlogin = params.get("needlogin", False)
        if isinstance(needlogin, str):
            needlogin = self._as_bool(needlogin)
        previous = self.login_progress
        if not needlogin:
            return True, None, previous

        stage = str(params.get("stage", "username") or "username").lower().strip()
        if stage == "username":
            if previous not in {"idle", "submitted"}:
                self.failure_reason = f"登录当前处于 {previous}，不能重新执行 username 阶段"
                self._repeat_guidance = self.failure_reason
                return False, stage, previous
            self.login_progress = "username_pending"
        elif stage == "password":
            if previous != "username_verified":
                self.failure_reason = "用户名尚未验收通过，已阻止进入 password 阶段"
                self._repeat_guidance = self.failure_reason
                return False, stage, previous
            self.login_progress = "password_pending"
        elif stage == "submit":
            if previous != "password_verified":
                self.failure_reason = "密码尚未验收通过，已阻止 submit 阶段"
                self._repeat_guidance = self.failure_reason
                return False, stage, previous
            self.login_progress = "submit_pending"
        else:
            self.failure_reason = "登录 stage 必须是 username/password/submit"
            self._repeat_guidance = self.failure_reason
            return False, stage, previous

        coordinate_name = {
            "username": "loginCoordinates",
            "password": "pdCoordinates",
            "submit": "buttonCoordinates",
        }[stage]
        coordinate = params.get(coordinate_name)
        valid_coordinate = isinstance(coordinate, dict) and self._validate_xy(coordinate)[0]
        if not valid_coordinate:
            self.login_progress = previous
            self.failure_reason = f"登录 {stage} 阶段的 {coordinate_name} 缺失或不在 [0,1]"
            self._repeat_guidance = self.failure_reason
            return False, stage, previous
        return True, stage, previous

    def _blocks_login_sequence(self, action: str, params: dict) -> bool:
        """登录字段通过后，只允许进入紧邻的下一阶段，防止直接点击绕过。"""
        required_stage = {
            "username_verified": "password",
            "password_verified": "submit",
        }.get(self.login_progress)
        if not required_stage:
            return False
        if action in {"wait", "verify_text_input", "abort_input"}:
            return False
        if (
            action == "skill_open_webpage"
            and str(params.get("stage", "username")).lower().strip() == required_stage
        ):
            return False
        self._repeat_guidance = (
            f"登录当前只允许执行 skill_open_webpage(stage={required_stage!r})；"
            f"已阻止 {action!r}，避免跳过字段或提前提交。"
        )
        print(f"  [登录顺序防护] {self._repeat_guidance}")
        return True

    def _expand_skill(self, skill_action: str, params: dict) -> list:
        """将技能动作展开为确定性子动作列表；参数非法时记录错误并返回空列表。

        系统类型一律使用构造时固化的 self.system（模型不能覆盖，防止跨系统误用快捷键）
        """
        self._last_skill_expand_error = ""
        try:
            if skill_action == "skill_open_app":
                return open_application(
                    application=params.get("application", ""),
                    system=self.system,
                )

            if skill_action == "skill_open_url":
                steps = open_url(
                    url=params.get("url", ""),
                    browser=params.get("browser", ""),
                    system=self.system,
                )
                policy = getattr(self, "task_policy", None)
                if (
                    policy is not None
                    and policy.active
                    and getattr(self, "_query_maximize_count", 0) >= 1
                ):
                    # The query contract allows at most one explicit maximize.
                    # open_url normally starts with another maximize for generic
                    # robustness; remove that physical duplicate once the same
                    # task has already maximized the current browser window.
                    steps = [
                        step for step in steps
                        if str(step.get("action", "")).lower() != "maximize_window"
                    ]
                return steps

            if skill_action == "skill_input_text":
                return input_text(
                    text=params.get("text"),
                    field_type=params.get("field_type", "auto"),
                    replace=self._as_bool(params.get("replace", True)),
                )

            if skill_action in ("skill_input_method", "skill_ime"):
                return input_method(
                    operation=params.get("operation", ""),
                    candidate=params.get("candidate"),
                )

            if skill_action == "skill_open_webpage":
                # needlogin 兼容 bool 与字符串
                needlogin = params.get("needlogin", False)
                if isinstance(needlogin, str):
                    needlogin = needlogin.strip().lower() in ("true", "1", "yes", "是")
                return open_webpage(
                    system=self.system,
                    needlogin=bool(needlogin),
                    username=params.get("username", ""),
                    password=params.get("password", ""),
                    loginCoordinates=params.get("loginCoordinates"),
                    pdCoordinates=params.get("pdCoordinates"),
                    buttonCoordinates=params.get("buttonCoordinates"),
                    stage=params.get("stage", "username"),
                )
        except Exception as e:
            print(f"  [技能] 参数非法: {e}")
            self._last_skill_expand_error = str(e)
            return []

        return []

    def _validate_task_scope(self, action: str, params: dict) -> bool:
        """Fail closed before an action can broaden a query-only task."""
        policy = getattr(self, "task_policy", None)
        if policy is None or not policy.active:
            return True
        internal_skill = getattr(self, "_active_skill_action", None)
        if self._is_armed_app_launch_recovery(action, params):
            internal_skill = "skill_open_app_recovery"
        recovery = getattr(self, "_app_launch_recovery", None)
        action_name = str(action or "").lower().strip()
        direct_browser_icon = bool(
            action_name in {"click", "double_click"}
            and self._is_raw_browser_icon_target(params.get("target", ""))
            and not self.task_plan
        )
        if (
            isinstance(recovery, dict)
            and internal_skill != "skill_open_app_recovery"
            and action_name not in {"wait", "skill_open_url", "skill_open_app"}
            and not direct_browser_icon
        ):
            recovery["remaining_clicks"] = 0
            recovery["scope_block_count"] = int(
                recovery.get("scope_block_count", 0)
            ) + 1
            self._repeat_guidance = (
                "浏览器启动结果尚未验收，本步已阻止且未发送任何输入。"
                "当前只允许被动 wait、重新执行 skill_open_app，或在浏览器已清楚可见时"
                "调用受控的 skill_open_url；一次性最佳匹配点击权限已失效。"
            )
            if recovery["scope_block_count"] >= 2:
                self.failure_reason = "浏览器启动未确认时连续提出越权动作，任务安全终止"
                self._abort_requested = True
            print(f"  [启动阶段] {self._repeat_guidance}")
            return False
        pending = self.pending_input if isinstance(self.pending_input, dict) else {}
        login_stage = str(pending.get("login_stage", "") or "").lower().strip()
        retry_field = str(params.get("field_type", "") or "").lower().strip()
        if (
            action_name == "retry_text_input"
            and pending.get("source_skill") == "skill_open_app"
            and str(pending.get("field_type", "")).lower().strip() == "app_search"
            and retry_field == "app_search"
            and str(params.get("text", "")) == str(pending.get("text", ""))
        ):
            internal_skill = "skill_open_app"
        if (
            action_name == "retry_text_input"
            and login_stage in {"username", "password"}
            and retry_field == login_stage
            and str(params.get("text", "")) == str(pending.get("text", ""))
        ):
            # This remains part of the controlled login stage.  The input state
            # machine separately requires a prior visual diagnosis, a verified
            # refocus and the single-retry budget before any key is sent.
            internal_skill = "skill_open_webpage"
        allowed, reason = policy.validate_action(
            action,
            params,
            internal_skill=internal_skill,
        )
        if allowed:
            allowed, reason = policy.validate_runtime_action(
                action,
                params,
                internal_skill=internal_skill,
                task_plan=self.task_plan,
                current_plan_idx=self.current_plan_idx,
                search_clicks=getattr(self, "_query_search_click_count", 0),
                pending_input=self.pending_input,
            )
        if allowed:
            self._preplan_scope_block_count = 0
            if str(action or "").lower().strip() == "skill_open_app":
                self._app_launch_redirect_count = 0
            return True
        if str(reason).startswith("尚未生成固定查询计划"):
            # A correctly identified safe navigation target may arrive one
            # decision too early.  Block it with zero input and require the
            # deterministic planning gate, instead of turning sequencing into
            # a terminal failure.
            self._repeat_guidance = (
                f"任务范围阶段门已阻止本步且未发送任何输入：{reason}。"
                "下一步必须调用 planning，不得重放该点击。"
            )
            print(f"  [范围阶段] {self._repeat_guidance}")
            return False
        if (
            not self.task_plan
            and str(reason).startswith("测试类型控件只能在固定计划的筛选步骤执行")
        ):
            # A prior stopped run may have left the read-only test-type popup
            # open. Clicking its header would toggle business UI before the
            # mandated login entry. Block the click with zero input and give
            # one explicit recovery turn to dismiss the transient with Escape.
            self._preplan_scope_block_count = (
                getattr(self, "_preplan_scope_block_count", 0) + 1
            )
            self._repeat_guidance = (
                "登录前阶段已阻止测试类型控件点击，未发送任何鼠标事件。"
                "若遗留测试类型弹层仍展开，下一步必须输出 press(key=escape) 关闭它；"
                "随后立即调用 skill_open_url 访问指定 login.html，禁止再次点击下拉头部。"
            )
            if self._preplan_scope_block_count >= 2:
                self.failure_reason = "登录前连续两次提出测试类型控件操作，任务安全终止"
                self._abort_requested = True
            print(f"  [范围阶段] {self._repeat_guidance}")
            return False
        self.failure_reason = f"任务范围策略已阻止操作：{reason}"
        self._repeat_guidance = self.failure_reason
        self._abort_requested = True
        print(f"  [范围策略] {self.failure_reason}")
        return False

    @staticmethod
    def _is_query_search_click(action: str, params: dict) -> bool:
        if str(action or "").lower().strip() != "click" or not isinstance(params, dict):
            return False
        target = str(params.get("target", "") or "").lower().strip()
        if "输入框" in target or "场景名称" in target:
            return False
        return "搜索" in target or target in {"查询", "查询按钮"}

    @staticmethod
    def _target_matches_pending_field(target: str, field_type: str) -> bool:
        """Require a semantic field match before accepting relocated focus."""
        normalized = str(target or "").lower().strip()
        field = str(field_type or "auto").lower().strip()
        keywords = {
            "username": ("用户名", "账号", "账户", "user name", "username"),
            "password": ("密码", "password"),
            "url": ("地址栏", "网址", "url", "address bar"),
        }.get(field, (field,))
        return bool(normalized and any(word and word in normalized for word in keywords))

    @staticmethod
    def _is_query_search_plan(plan: dict | None) -> bool:
        if not isinstance(plan, dict):
            return False
        content = str(plan.get("content", "") or "")
        return "搜索" in content and "不执行搜索" not in content

    def _completion_block_reason(self) -> str:
        """Return why ``done`` is premature; empty means the plan is complete."""
        policy = getattr(self, "task_policy", None)
        if policy is not None and policy.active and not self.task_plan:
            return "查询任务尚未生成受限查询计划"
        if not self.task_plan:
            return ""
        incomplete = [
            index + 1 for index, item in enumerate(self.task_plan)
            if not item.get("completed") or item.get("passed") is not True
        ]
        if incomplete:
            return f"子任务 {incomplete} 尚未完成并通过验收"
        return ""

    @staticmethod
    def _target_region_for_action(
        action_data: dict,
        image_size: tuple[int, int],
    ) -> tuple[int, int, int, int] | None:
        action = str(action_data.get("action", "") or "").lower().strip()
        params = action_data.get("params", {})
        if not isinstance(params, dict):
            return None
        try:
            if action in {"click", "double_click", "right_click", "move", "scroll"}:
                return target_region_around_point(params["x"], params["y"], image_size)
            if action == "drag":
                start = target_region_around_point(
                    params["start_x"], params["start_y"], image_size
                )
                end = target_region_around_point(
                    params["end_x"], params["end_y"], image_size
                )
                return (
                    min(start[0], end[0]),
                    min(start[1], end[1]),
                    max(start[2], end[2]),
                    max(start[3], end[3]),
                )
        except (KeyError, InteractionGuardError, TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _diff_evidence(result) -> dict:
        if result is None:
            return {}
        return {
            "changed": result.changed,
            "geometry_changed": result.geometry_changed,
            "full_score": round(result.full_score, 6),
            "full_changed": result.full_changed,
            "max_tile_score": round(result.max_tile_score, 6),
            "tile_changed": result.tile_changed,
            "target_score": (
                None if result.target_score is None else round(result.target_score, 6)
            ),
            "target_changed": result.target_changed,
            "target_region": result.target_region,
            "reason": result.reason,
        }

    def _loop_decision(
        self,
        action_data: dict,
        screenshot: Image.Image,
    ) -> tuple[bool, str, dict | None]:
        """Decide whether a candidate belongs to a short no-progress cycle."""
        if action_data.get("action") == "verify_text_input" and self.pending_input:
            # Re-reading a field after a repair must not be treated as replaying
            # a click. The input state machine owns verification and retry limits.
            return False, "输入验收由字段状态机控制", None
        guard = getattr(self, "interaction_guard", None)
        if guard is None:
            guard = InteractionGuard(coordinate_grid=50)
            self.interaction_guard = guard
        try:
            result = guard.detect_loop(self.history, action_data)
        except (InteractionGuardError, TypeError, ValueError) as exc:
            return True, f"动作签名无效：{exc}", None
        if not result.detected:
            return False, "", None

        visual = None
        observations = getattr(self, "_executed_observations", [])
        if result.period and len(observations) >= result.period:
            prior = observations[-result.period]
            region = self._target_region_for_action(action_data, screenshot.size)
            visual = compare_screen_state(prior, screenshot, target_region=region)

        action = str(action_data.get("action", "") or "").lower().strip()
        # Consecutive scrolling is legitimate while the target container is changing.
        # It becomes a boundary/loop as soon as its ROI stops changing or its budget ends.
        if action == "scroll" and result.period == 1 and visual and visual.changed:
            try:
                signature = guard.signature(action_data)
            except (InteractionGuardError, TypeError, ValueError):
                signature = ("scroll",)
            counts = getattr(self, "_scroll_counts", {})
            next_count = counts.get(signature, 0) + 1
            counts[signature] = next_count
            self._scroll_counts = counts
            if next_count <= 8:
                return False, "滚动容器仍在变化，允许继续", {
                    "period": result.period,
                    "visual_changed": True,
                    "scroll_count": next_count,
                }

        # Repeating the same click after it changed the target is unsafe: it may
        # toggle a checkbox or close/reopen the dropdown. Other periods are blocked
        # when they return to the same visual state.
        should_block = result.period == 1 or visual is None or not visual.changed
        if not should_block:
            return False, "周期动作伴随新的页面状态，暂不拦截", {
                "period": result.period,
                "visual_changed": True,
            }

        key = repr((result.period, result.cycle))
        counts = getattr(self, "_loop_block_counts", {})
        counts[key] = counts.get(key, 0) + 1
        self._loop_block_counts = counts
        reason = (
            f"检测到周期 {result.period} 的重复动作，"
            f"页面状态{'仍未变化' if visual is None or not visual.changed else '已变化但同一动作不可重放'}"
        )
        evidence = {
            "period": result.period,
            "repetitions": result.repetitions,
            "visual_changed": None if visual is None else visual.changed,
            "block_count": counts[key],
        }
        if counts[key] >= 2:
            replans = getattr(self, "_loop_replans", {})
            phase = getattr(self, "current_plan_idx", -1)
            if replans.get(phase, 0) < 2:
                replans[phase] = replans.get(phase, 0) + 1
                self._loop_replans = replans
                self._loop_replan_requested = True
                counts[key] = 0
                reason += "；转入当前步骤重新规划，下一帧重新观察后选择恢复动作"
                evidence["replan_requested"] = True
            else:
                self.failure_reason = reason + "；当前步骤两次重新规划后仍无法推进，已停止"
                self._abort_requested = True
        return True, reason, evidence

    def _execute_action(self, action_data: dict, screenshot: Image.Image) -> bool:
        """
        执行一个动作
        screenshot: 当步截图，用于归一化坐标到像素的换算
        返回 True 表示继续循环，False 表示结束
        """
        self._last_action_executed = False
        action = str(action_data.get("action", "wait") or "wait").lower().strip()
        params = action_data.get("params", {})
        if not isinstance(params, dict):
            params = {}
        thought = action_data.get("thought", "")

        print(f"[Step {self.step}] 动作: {action}, 参数: {params}")
        print(f"  思考: {thought}")

        try:
            reverify_url_after_escape = False
            app_launch_submission = None
            launch_recovery_click = False
            # Input-state gating comes first so an IME composition is reported as
            # an unverified field, not mislabelled as an unknown-focus scope breach.
            if self._blocks_unverified_input(action, params):
                return True
            if not self._validate_task_scope(action, params):
                return False
            if self._blocks_login_sequence(action, params):
                return True

            direct_browser_icon = ""
            policy = getattr(self, "task_policy", None)
            if (
                policy is not None
                and policy.active
                and not self.task_plan
                and action in {"click", "double_click"}
            ):
                direct_browser_icon = self._browser_icon_application(
                    params.get("target", "")
                )
                if direct_browser_icon:
                    # A direct desktop launch supersedes an unresolved Start-menu
                    # attempt. URL entry will still require foreground verification.
                    self._app_launch_recovery = None

            if self.pending_input and self.pending_input.get("verified") is True:
                mutates_verified = action in {
                    "type", "input_text", "retry_text_input", "ime_operation",
                    "skill_input_text", "skill_input_method", "skill_ime",
                }
                if mutates_verified:
                    self._repeat_guidance = (
                        "当前字段已经验收通过，已阻止继续修改；"
                        "请先点击下一个字段，或按 Enter/Tab/提交离开当前字段。"
                    )
                    return True
                pending_field = str(
                    self.pending_input.get("field_type", "") or ""
                ).lower().strip()
                key = str(params.get("key", "") or "").lower().strip()
                if (
                    action == "press"
                    and key in {"escape", "esc"}
                    and pending_field in {"url", "app_search"}
                ):
                    # Edge may show an address suggestion list after the exact
                    # URL was typed. Escape is allowed to dismiss it, but the
                    # address must be read from the next frame again before
                    # Enter; do not discard the expected value or authorize a
                    # blind submit.
                    reverify_url_after_escape = True
                elif action not in {"wait", "verify_text_input"}:
                    if (
                        action == "press"
                        and key in {"enter", "return"}
                        and pending_field == "app_search"
                        and self.pending_input.get("source_skill") == "skill_open_app"
                        and self._browser_application(self.pending_input.get("text"))
                        and getattr(getattr(self, "task_policy", None), "active", False)
                    ):
                        app_launch_submission = self._browser_application(
                            self.pending_input.get("text")
                        )
                    self._clear_pending_input()

            if action == "click":
                launch_recovery_click = self._is_armed_app_launch_recovery(action, params)
                refocusing_input = bool(
                    self.pending_input
                    and self.pending_input.get("verified") is not True
                    and self.pending_input.get("cause") == "focus"
                )
                x, y = self._to_pixels(params, screenshot)
                if launch_recovery_click:
                    # Consume before sending: mouseDown may already have happened
                    # when a transport error is reported, so never authorize replay.
                    self._app_launch_recovery["remaining_clicks"] = 0
                    self._app_launch_recovery["click_attempted"] = True
                    self._app_launch_recovery["url_verification_failures"] = 0
                try:
                    self.vnc.click(x, y)
                except Exception:
                    if launch_recovery_click:
                        self._abort_requested = True
                    raise
                if direct_browser_icon:
                    self._app_launch_recovery = {
                        "application": direct_browser_icon,
                        "remaining_clicks": 0,
                        "wait_count": 0,
                        "url_verification_failures": 0,
                        "source": "verified_browser_icon",
                    }
                    self._repeat_guidance = (
                        f"已点击验收过的 {direct_browser_icon} 浏览器图标。"
                        "下一步先观察窗口；调用 skill_open_url 前仍会独立确认"
                        "浏览器窗口、标签栏和地址栏已经位于前台。"
                    )
                policy = getattr(self, "task_policy", None)
                if (
                    policy is not None
                    and policy.active
                    and self._is_query_search_click(action, params)
                ):
                    self._query_search_click_count = (
                        getattr(self, "_query_search_click_count", 0) + 1
                    )
                    self._query_last_search_evidence = {
                        "step": self.step,
                        "plan_index": self.current_plan_idx,
                        "after_captured": False,
                    }
                self._last_focus_coordinates = (
                    float(params.get("x")),
                    float(params.get("y")),
                )
                if refocusing_input:
                    self.pending_input["refocused"] = True
                    self._repeat_guidance = (
                        "已重新点击待恢复的输入框；现在只能调用 retry_text_input "
                        "对原字段做一次替换输入，然后重新验收。"
                    )
            elif action == "double_click":
                x, y = self._to_pixels(params, screenshot)
                self.vnc.double_click(x, y)
                if direct_browser_icon:
                    self._app_launch_recovery = {
                        "application": direct_browser_icon,
                        "remaining_clicks": 0,
                        "wait_count": 0,
                        "url_verification_failures": 0,
                        "source": "verified_browser_icon",
                    }
                    self._repeat_guidance = (
                        f"已双击验收过的 {direct_browser_icon} 浏览器图标。"
                        "下一步等待窗口出现；调用 skill_open_url 前仍会独立确认"
                        "浏览器窗口、标签栏和地址栏已经位于前台。"
                    )

            elif action == "right_click":
                x, y = self._to_pixels(params, screenshot)
                self.vnc.click(x, y, button=3)

            elif action == "move":
                x, y = self._to_pixels(params, screenshot)
                self.vnc.move_mouse(
                    x,
                    y,
                    duration_ms=params.get("duration_ms", 0),
                    steps=params.get("steps", 1),
                )

            elif action == "drag":
                start_x, start_y = self._to_pixels(
                    params, screenshot, x_key="start_x", y_key="start_y"
                )
                end_x, end_y = self._to_pixels(
                    params, screenshot, x_key="end_x", y_key="end_y"
                )
                self.vnc.drag(
                    start_x,
                    start_y,
                    end_x,
                    end_y,
                    duration_ms=params.get("duration_ms", 400),
                    steps=params.get("steps", 8),
                    button=params.get("button", "left"),
                )

            elif action == "type":
                if not self._input_once(
                    params.get("text"),
                    params.get("field_type", "auto"),
                    replace=False,
                ):
                    return False

            elif action == "input_text":
                if not self._input_once(
                    params.get("text"),
                    params.get("field_type", "auto"),
                    self._as_bool(params.get("replace", True)),
                ):
                    return False

            elif action == "retry_text_input":
                source_text = (
                    params["text"]
                    if "text" in params
                    else (self.pending_input or {}).get("text")
                )
                if not self._retry_input(
                    source_text,
                    params.get("field_type", ""),
                    params.get("cause", ""),
                ):
                    return False

            elif action == "verify_text_input":
                if not self._verify_pending_input(params, screenshot):
                    return False

            elif action == "ime_operation":
                if self.pending_input and self.pending_input.get("verified") is not True:
                    recorded_cause = self._normalize_input_cause(
                        self.pending_input.get("cause")
                    )
                    if (
                        not self.pending_input.get("verification_attempted")
                        or recorded_cause != "ime"
                    ):
                        self.failure_reason = (
                            "输入法操作必须先由 verify_text_input 确认 cause='ime'"
                        )
                        self._repeat_guidance = self.failure_reason
                        return False
                operation = self.vnc.ime.normalize_operation(params.get("operation", ""))
                signature = f"{operation}:{params.get('candidate', '')}"
                if self.ime_operation_counts.get(signature, 0) >= 1:
                    self.failure_reason = (
                        f"输入法操作 {operation!r} 已执行一次，已停止重复切换或选择"
                    )
                    self._repeat_guidance = self.failure_reason
                    print(f"  [输入法防循环] {self.failure_reason}")
                    return False
                self.vnc.ime.perform(operation, params.get("candidate"))
                self.ime_operation_counts[signature] = (
                    self.ime_operation_counts.get(signature, 0) + 1
                )
                if self.pending_input and self.pending_input.get("verified") is not True:
                    # 输入法操作会改变字段可见状态，必须重新截图验收后才能纠错。
                    self.pending_input["verification_attempted"] = False
                    self.pending_input["cause"] = "unknown"

            elif action == "press":
                key = params.get("key", "")
                self.vnc.press_key(key)
                self._last_focus_coordinates = None
                if app_launch_submission:
                    self._app_launch_recovery = {
                        "application": app_launch_submission,
                        "remaining_clicks": 1,
                        "wait_count": 0,
                    }
                    self._repeat_guidance = (
                        f"已向验收过的 {app_launch_submission} 开始菜单结果发送一次 Enter。"
                        "若下一帧仍显示同一浏览器的‘最佳匹配’，说明 Enter 仅提交了"
                        "输入法组合串；只允许点击该精确最佳匹配一次，且仍须通过独立"
                        "红十字落点验收。若浏览器已打开，则直接访问指定登录页。"
                    )
                if reverify_url_after_escape and self.pending_input:
                    self.pending_input["verified"] = False
                    self.pending_input["verification_attempted"] = False
                    self.pending_input["cause"] = "unknown"
                    self.pending_input.pop("visual_recheck_required", None)
                    self.pending_input.pop("visual_recheck_waited", None)
                    self._repeat_guidance = (
                        "已用 Escape 关闭地址栏建议；下一步必须根据新截图再次调用 "
                        "verify_text_input 核对完整 URL，验收通过后才能按 Enter。"
                    )

            elif action == "maximize_window":
                policy = getattr(self, "task_policy", None)
                query_mode = policy is not None and policy.active
                if not query_mode or getattr(self, "_query_maximize_count", 0) < 1:
                    self.vnc.maximize_window(self.system)
                    if query_mode:
                        self._query_maximize_count = 1
                else:
                    print("  [范围阶段] 当前查询任务已最大化一次，本步作为幂等 no-op")
                self._last_focus_coordinates = None

            elif action == "scroll":
                x, y = self._to_pixels(params, screenshot)
                amount = params.get("amount")
                if amount is None:
                    # 兼容旧动作格式，但不再回退到屏幕中心。
                    direction = str(params.get("direction", "down")).lower().strip()
                    amount = 1 if direction == "up" else -1
                if isinstance(amount, bool) or not isinstance(amount, int):
                    raise ValueError("scroll amount 必须是 -5~5 的非零整数")
                if amount == 0 or abs(amount) > 5:
                    raise ValueError("scroll amount 必须是 -5~5 的非零整数")
                self.vnc.scroll(x, y, amount=amount)

            elif action == "wait":
                # 模型可指定等待时长，限制在 0.5~3 秒避免浪费
                seconds = params.get("seconds", 1.0)
                try:
                    seconds = float(seconds)
                except (TypeError, ValueError):
                    seconds = 1.0
                time.sleep(max(0.5, min(3.0, seconds)))
                recovery = getattr(self, "_app_launch_recovery", None)
                if isinstance(recovery, dict):
                    recovery["wait_count"] = int(recovery.get("wait_count", 0)) + 1
                    if recovery["wait_count"] > 1:
                        recovery["remaining_clicks"] = 0
                        self._repeat_guidance = (
                            "应用启动恢复凭据已过期；不得点击任何开始菜单结果。"
                            "请重新执行受控的 skill_open_app。"
                        )
                if self.pending_input and self.pending_input.get("visual_recheck_required"):
                    self.pending_input["visual_recheck_waited"] = True
                    self._repeat_guidance = (
                        "已完成被动等待并取得新的光标闪烁相位；"
                        "下一步只能重新调用 verify_text_input，禁止重输或提交。"
                    )

            elif action == "abort_input":
                reason = str(params.get("reason", "") or "字段无法可靠验收")
                field = str((self.pending_input or {}).get("field_type", "auto"))
                if field == "password":
                    reason = "密码字段无法可靠验收"
                self.failure_reason = f"输入任务已安全停止：{reason}"
                self._repeat_guidance = self.failure_reason + "；未执行提交。"
                self._abort_requested = True
                self._clear_pending_input()

            elif action == "done":
                blocked_reason = self._completion_block_reason()
                if blocked_reason:
                    self._repeat_guidance = (
                        f"已阻止提前完成：{blocked_reason}。"
                        "请继续当前受限子任务；若无法验证，应报告失败而不是 done。"
                    )
                    print(f"  [完成门] {self._repeat_guidance}")
                    return True
                print("任务完成！")
                self._last_action_executed = True
                return False

            elif action in (
                "skill_open_app",
                "skill_open_webpage",
                "skill_open_url",
                "skill_input_text",
                "skill_input_method",
                "skill_ime",
            ):
                # 固定流程技能：只展开到下一个可截图验收的状态。
                login_stage = None
                previous_login_progress = self.login_progress
                if action == "skill_open_app":
                    # A new controlled launch supersedes the unresolved old one
                    # before its nested key events begin.
                    self._app_launch_recovery = None
                if action == "skill_open_url" and isinstance(
                    getattr(self, "_app_launch_recovery", None), dict
                ):
                    recovery = self._app_launch_recovery
                    requested_browser = self._browser_application(
                        recovery.get("application", "")
                    )
                    browser_label = (
                        "Microsoft Edge" if requested_browser == "edge"
                        else "Google Chrome"
                    )
                    declared_browser_value = str(
                        params.get("browser", "") or ""
                    ).lower().strip()
                    if declared_browser_value not in {"", "default"} and (
                        self._browser_application(declared_browser_value)
                        != requested_browser
                    ):
                        recovery["remaining_clicks"] = 0
                        recovery["url_verification_failures"] = int(
                            recovery.get("url_verification_failures", 0)
                        ) + 1
                        self._repeat_guidance = (
                            f"浏览器参数与受控启动的 {browser_label} 不一致，"
                            "本步未向任何焦点输入网址。请使用相同浏览器重新请求。"
                        )
                        if recovery["url_verification_failures"] >= 2:
                            self.failure_reason = "连续两次无法绑定受控启动的浏览器，任务安全终止"
                            self._abort_requested = True
                        return True
                    launch_check = self._verify_visible_state(
                        screenshot,
                        objective="确认受控启动的浏览器已经真正打开",
                        expected_result=(
                            "Windows 开始菜单和输入法候选条均已关闭，且截图中清楚可见"
                            f" {browser_label} 浏览器窗口、标签栏和地址栏；"
                            "不得用其他浏览器窗口作为通过证据"
                        ),
                    )
                    visible_description = self._compact_visible_target(" ".join(
                        [
                            str(launch_check.get("state", "")),
                            *[
                                str(item)
                                for item in launch_check.get("evidence", [])
                            ],
                        ]
                    ))
                    browser_markers = {
                        "edge": ("microsoftedge", "edge浏览器", "edgebrowser"),
                        "chrome": ("googlechrome", "chrome浏览器", "chromebrowser"),
                    }.get(requested_browser, ())
                    if launch_check.get("passed") and not any(
                        marker in visible_description for marker in browser_markers
                    ):
                        launch_check = dict(launch_check)
                        launch_check["passed"] = False
                        launch_check["reason"] = (
                            f"验收证据未明确绑定受控启动的 {browser_label}"
                        )
                    self._last_state_verification = launch_check
                    if not launch_check["passed"]:
                        recovery["url_verification_failures"] = int(
                            recovery.get("url_verification_failures", 0)
                        ) + 1
                        if recovery.get("remaining_clicks") == 1:
                            self._repeat_guidance = (
                                "第一次 Enter 后尚未验证浏览器已启动，已阻止向未知焦点输入网址。"
                                "若开始菜单仍显示同一浏览器的精确‘最佳匹配’，只允许点击该结果"
                                "一次；否则重新执行 skill_open_app。"
                            )
                        else:
                            self._repeat_guidance = (
                                "尚未验证浏览器已启动，且一次性开始菜单恢复权限已经过期。"
                                "本步未输入网址；请重新执行 skill_open_app。"
                            )
                        if recovery["url_verification_failures"] >= 2:
                            self.failure_reason = "连续两次无法验证浏览器已启动，任务安全终止"
                            self._abort_requested = True
                        return True
                    self._app_launch_recovery = None
                if action == "skill_open_webpage":
                    requested_stage = str(
                        params.get("stage", "username") or "username"
                    ).lower().strip()
                    policy = getattr(self, "task_policy", None)
                    if (
                        policy is not None
                        and policy.active
                        and requested_stage == "username"
                        and not getattr(self, "_query_login_entry_verified", False)
                    ):
                        entry_check = self._verify_visible_state(
                            screenshot,
                            objective="确认当前正位于用户指定的登录入口",
                            expected_result=(
                                f"浏览器地址栏清楚显示 {policy.login_url}，"
                                "且页面可见用户登录表单、用户名框、密码框和登录按钮"
                            ),
                        )
                        self._last_state_verification = entry_check
                        if not entry_check["passed"]:
                            self.failure_reason = (
                                "无法确认当前位于用户指定的 login.html 登录入口："
                                + entry_check.get("reason", "缺少可见证据")
                            )
                            self._repeat_guidance = self.failure_reason
                            self._abort_requested = True
                            return False
                        self._query_login_entry_verified = True
                    allowed, login_stage, previous_login_progress = self._prepare_login_stage(params)
                    if not allowed:
                        return False
                self._active_login_input_stage = login_stage
                previous_active_skill = getattr(self, "_active_skill_action", None)
                self._active_skill_action = action
                skill_failed = False
                try:
                    sub_steps = self._expand_skill(action, params)
                    if self._last_skill_expand_error:
                        self.failure_reason = f"技能 {action} 参数非法：{self._last_skill_expand_error}"
                        self._repeat_guidance = self.failure_reason
                        skill_failed = True
                    print(f"  [技能] 展开为 {len(sub_steps)} 个子步骤")
                    for i, sub in enumerate(sub_steps, 1) if not skill_failed else []:
                        sub_action = sub.get("action", "")
                        sub_params = sub.get("params", {})
                        print(f"  [技能 {i}/{len(sub_steps)}] {sub_action} {sub_params}")
                        # 子动作坐标基于当步截图换算；任一失败都必须终止后续子动作。
                        continued = self._execute_action(sub, screenshot)
                        if not continued or not self._last_action_executed:
                            skill_failed = True
                            self._repeat_guidance = (
                                self._repeat_guidance
                                or f"技能 {action} 的子动作 {sub_action} 执行失败，已停止后续步骤。"
                            )
                            break
                finally:
                    self._active_login_input_stage = None
                    self._active_skill_action = previous_active_skill
                if skill_failed:
                    if action == "skill_open_webpage":
                        self.login_progress = previous_login_progress
                    return False
                if action == "skill_open_url":
                    self._app_launch_recovery = None
                if action == "skill_open_webpage" and login_stage == "submit":
                    self.login_progress = "submitted"

            elif action == "planning":
                # 已存在计划时忽略重复调用，避免阶段2中覆盖正在执行的子任务列表
                if self.task_plan:
                    print("  [规划] 子任务计划已存在，忽略重复的 planning 调用")
                else:
                    policy = getattr(self, "task_policy", None)
                    if policy is not None and policy.active:
                        if not getattr(self, "_query_login_entry_verified", False):
                            self.failure_reason = (
                                "未取得从指定 login.html 分阶段登录的运行证据，"
                                "已阻止进入业务查询计划"
                            )
                            self._repeat_guidance = self.failure_reason
                            self._abort_requested = True
                            return False
                        login_check = self._verify_visible_state(
                            screenshot,
                            objective="确认已通过指定 login.html 登录并进入平台",
                            expected_result=(
                                "登录表单不再显示，且截图中可见平台首页、左侧导航、"
                                "体验中心或智能体能力等已登录锚点"
                            ),
                        )
                        self._last_state_verification = login_check
                        if not login_check["passed"]:
                            self._planning_verification_failures = (
                                getattr(self, "_planning_verification_failures", 0) + 1
                            )
                            self._repeat_guidance = (
                                "尚无已登录平台的可见证据，已阻止生成业务查询计划："
                                + login_check.get("reason", "")
                            )
                            if self._planning_verification_failures >= 2:
                                self.failure_reason = "连续两次无法验证登录成功，任务安全终止"
                                self._abort_requested = True
                            return True
                        self._planning_verification_failures = 0
                        # 查询场景使用确定性计划，避免“测试”被扩大成 CRUD 全功能测试。
                        self.task_plan = policy.build_plan(self.task)
                    else:
                        # 调用视觉模型分析当前页面并生成结构化子任务列表
                        plan_prompt = f"""用户任务：{self.task}
知识库信息：{self.knowledge_summary or '无'}

请分析当前截图中的页面状态，规划完成该任务所需的子任务列表。"""
                        plan_response = chat_vision(plan_prompt, screenshot,
                                                    system_prompt=PLANNING_TOOL_PROMPT)
                        self.task_plan = parse_planning(plan_response)
                    if policy is not None:
                        valid_plan, plan_reason = policy.validate_plan(self.task_plan)
                        if not valid_plan:
                            self.failure_reason = f"任务计划越界：{plan_reason}"
                            self._repeat_guidance = self.failure_reason
                            self._abort_requested = True
                            return False
                    if self.task_plan:
                        print(f"  [规划] 生成 {len(self.task_plan)} 个子任务:")
                        for i, t in enumerate(self.task_plan, 1):
                            print(f"    {i}. {t['content']} (预期: {t['expected_result']})")
                    else:
                        print(f"  [规划] 解析失败，继续使用基础动作模式")

            elif action == "subtask_done":
                # 暂存本子任务执行结果，由 run_task 的 for 循环读取后写入计划并推进
                claimed_passed = self._as_bool(params.get("passed", False))
                verification_screenshot = screenshot
                if claimed_passed and 0 <= self.current_plan_idx < len(self.task_plan):
                    current = self.task_plan[self.current_plan_idx]
                    policy = getattr(self, "task_policy", None)
                    if (
                        policy is not None
                        and policy.active
                        and self._is_query_search_plan(current)
                    ):
                        expected_searches = sum(
                            1
                            for item in self.task_plan[:self.current_plan_idx + 1]
                            if self._is_query_search_plan(item)
                        )
                        evidence = getattr(self, "_query_last_search_evidence", None)
                        if (
                            getattr(self, "_query_search_click_count", 0) != expected_searches
                            or not isinstance(evidence, dict)
                            or evidence.get("plan_index") != self.current_plan_idx
                            or evidence.get("after_captured") is not True
                        ):
                            self._repeat_guidance = (
                                "搜索子任务缺少“本阶段恰好一次已执行点击 + 动作后截图”证据，"
                                "已拒绝完成标记。"
                            )
                            print(f"  [搜索验收] {self._repeat_guidance}")
                            return True
                        verification_frame = evidence.get("verification_frame")
                        try:
                            verification_path = Path(str(verification_frame or "")).resolve()
                            screenshot_root = SCREENSHOT_DIR.resolve()
                            if not verification_path.is_relative_to(screenshot_root):
                                raise ValueError("验收帧不在任务截图目录内")
                            with Image.open(verification_path) as saved_frame:
                                verification_screenshot = saved_frame.convert("RGB").copy()
                        except Exception as exc:
                            self._repeat_guidance = (
                                "搜索子任务缺少可读取的本次搜索后验收帧，已拒绝完成标记："
                                f"{exc}"
                            )
                            print(f"  [搜索验收] {self._repeat_guidance}")
                            return True
                    verification = self._verify_visible_state(
                        verification_screenshot,
                        objective=current.get("content", "当前子任务"),
                        expected_result=current.get("expected_result", ""),
                    )
                    self._last_state_verification = verification
                    if not verification["passed"]:
                        self._repeat_guidance = (
                            "独立截图验收未通过，已拒绝 subtask_done："
                            + verification.get("reason", "未找到可见证据")
                        )
                        print(f"  [状态验收] {self._repeat_guidance}")
                        return True
                self._subtask_result = {
                    "result": params.get("result", ""),
                    "passed": claimed_passed,
                }
                passed_str = "通过" if self._subtask_result["passed"] else "未通过"
                print(f"  [子任务 {self.current_plan_idx + 1}/{len(self.task_plan)}] "
                      f"{passed_str}")
                if not self._subtask_result["passed"]:
                    self.failure_reason = (
                        f"子任务 {self.current_plan_idx + 1} 验收未通过："
                        f"{self._subtask_result['result'] or '未提供失败证据'}"
                    )
                    self._repeat_guidance = self.failure_reason
                    self._abort_requested = True

            elif action == "discover_options":
                # 动态子任务发现：根据下拉框选项生成 N 个具体子任务，替换当前及之后的子任务
                category = params.get("category", "")
                options = params.get("options", [])
                task_template = params.get("task_template", "")

                if self.current_plan_idx < 0 or not self.task_plan:
                    print("  [发现] 不在子任务阶段，忽略 discover_options")
                elif not options or not task_template:
                    print(f"  [发现] 参数无效: category={category}, options={options}")
                else:
                    # 生成新子任务列表
                    new_subtasks = []
                    for opt in options:
                        content = task_template.replace("{opt}", str(opt))
                        new_subtasks.append({
                            "content": content,
                            "expected_result": f"{category}={opt} 的操作完成且验证通过",
                            "completed": False,
                        })
                    # 替换当前及之后的子任务，保留之前已完成的
                    old_count = len(self.task_plan) - self.current_plan_idx
                    self.task_plan = self.task_plan[:self.current_plan_idx] + new_subtasks

                    print(f"  [发现] {category} 有 {len(options)} 个选项: {options}")
                    print(f"  [发现] 已生成 {len(new_subtasks)} 个子任务，"
                          f"替换原计划中第 {self.current_plan_idx + 1} 个及之后的 {old_count} 个子任务")
                    for i, st in enumerate(new_subtasks, 1):
                        print(f"    {i}. {st['content']}")

            else:
                print(f"未知动作: {action}")
                time.sleep(1)
                self.failure_reason = f"未知动作: {action}"
                self._repeat_guidance = self.failure_reason
                return False

            recovery = getattr(self, "_app_launch_recovery", None)
            if (
                isinstance(recovery, dict)
                and action != "wait"
                and not launch_recovery_click
                and not app_launch_submission
            ):
                # Any successful detour invalidates the one-shot click grant.
                # Keep the record only so skill_open_url still has to prove that
                # the originally requested browser is actually in the foreground.
                recovery["remaining_clicks"] = 0

        except Exception as e:
            print(f"执行动作出错: {e}")
            self.failure_reason = f"动作 {action} 执行失败：{e}"
            self._repeat_guidance = self.failure_reason
            time.sleep(1)
            return False

        self._last_action_executed = True
        return True

    @staticmethod
    def _to_pixels(
        params: dict,
        screenshot: Image.Image,
        *,
        x_key: str = "x",
        y_key: str = "y",
    ) -> tuple:
        """归一化坐标 (0~1) 转像素坐标，基于当步截图实际尺寸换算"""
        x_norm, y_norm = validate_normalized_coordinates(
            params, x_key=x_key, y_key=y_key
        )
        w, h = screenshot.size
        if w <= 0 or h <= 0:
            raise ValueError(f"截图尺寸非法: {w}x{h}")
        px = int(x_norm * (w - 1))
        py = int(y_norm * (h - 1))
        return px, py

    def _normalize_browser_search_request(self, proposal: dict) -> dict:
        """Route a startup search intention to the already-approved app macro.

        Model prose selects an existing capability, never grants pointer access.
        In particular, a bare business Search click keeps its original policy.
        """
        policy = getattr(self, "task_policy", None)
        if (
            not policy or not policy.active
            or self.task_plan or self.pending_input
            or proposal.get("action") != "click"
        ):
            return proposal
        target = str(proposal.get("params", {}).get("target", ""))
        context = (target + " " + str(proposal.get("thought", ""))).lower()
        if (
            "搜索" not in target
            or not any(word in context for word in ("任务栏", "开始菜单", "windows搜索", "系统搜索"))
            or not any(word in context for word in ("浏览器", "edge", "chrome"))
        ):
            return proposal
        application = "chrome" if "chrome" in context else "edge"
        return {
            "action": "skill_open_app",
            "params": {"application": application},
            "thought": "识别到通过系统搜索启动浏览器的请求，使用应用启动流程聚焦并输入浏览器名；下一步截图验收，不自动回车。",
            "source": "browser_search_normalization",
            "requested_action": proposal,
        }

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
          "discover"     - discover_options 动态扩展了子任务列表（不推进 idx，重新获取当前子任务）
          "failed"       - 输入链路按安全策略终止
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
        before_path = self.saveScreenShot(self.step, self.rand, screenshot)
        # 搜索点击后，查询策略只允许被动 wait 或 subtask_done。因而每个
        # 后续决策开始时保存的这一帧都与同一次 Search 保持因果绑定；用它
        # 更新验收帧既能等待异步结果稳定，又不会接受改过筛选后的任意帧。
        search_evidence = getattr(self, "_query_last_search_evidence", None)
        if (
            self._is_query_search_plan(plan)
            and isinstance(search_evidence, dict)
            and search_evidence.get("plan_index") == self.current_plan_idx
            and search_evidence.get("after_captured") is True
        ):
            search_evidence["verification_frame"] = before_path
        t_obs = time.perf_counter() - t_obs_start

        # 2. 决策：调用视觉模型，坐标校验失败时带反馈重试一次
        self._last_state_verification = None
        guidance_block = f"\n{self._repeat_guidance}\n" if self._repeat_guidance else ""
        if use_business_knowledge(plan, self.pending_input, self._repeat_guidance):
            knowledge_block = getattr(self, "_business_knowledge_block", knowledge_block)
        policy = getattr(self, "task_policy", None)
        policy_block = f"\n{policy.prompt}\n" if policy is not None and policy.prompt else ""
        if policy is not None and policy.active and not self.task_plan:
            policy_block += login_for_prompt(
                getattr(self, "login_progress", "idle"),
                getattr(self, "_query_login_entry_verified", False),
                policy.login_url,
            )
        pending_input_block = ""
        if self.pending_input:
            pending = self.pending_input
            field = str(pending.get("field_type", "auto"))
            target = (
                f"密码字段，目标长度 {len(pending.get('text', ''))}"
                if field == "password"
                else f"字段 {field}，目标文本 {pending.get('text', '')!r}"
            )
            status = "已验收" if pending.get("verified") is True else "待验收"
            pending_input_block = f"\n【输入状态】{target}；当前状态：{status}。\n"
            if pending.get("verified") is not True and field == "username":
                pending_input_block += (
                    "【用户名验收区域】只读取登录卡片中带人形/用户图标的用户名输入行。"
                    "若该行仍显示“请输入登录账号”或类似占位符，用户名实际内容就是空，"
                    "必须 observed_text=\"\"、cause=\"focus\"；不得把下方密码行的圆点、"
                    "浏览器自动填充卡片、历史 thought 或目标文本当成用户名已输入。\n"
                )
            elif pending.get("verified") is not True and field == "password":
                pending_input_block += (
                    "【密码验收区域】只统计登录卡片中带锁图标的密码输入行内可见掩码；"
                    "用户名行、浏览器凭据提示和预期长度本身都不是掩码证据。\n"
                )
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
【本步原始截图坐标系】宽 {screenshot.width} 像素，高 {screenshot.height} 像素，包含浏览器标题栏以及截图中可见的桌面区域。归一化坐标必须按 x=目标中心像素x/{max(1, screenshot.width - 1)}、y=目标中心像素y/{max(1, screenshot.height - 1)} 计算，不得套用 1024、960 等假定高度。
        {knowledge_block}{policy_block}{subtask_block}{pending_input_block}{guidance_block}
之前的操作历史：
{history_for_prompt(self.history)}

请输出下一步操作的 JSON。"""

        action_data = None
        if getattr(self, "_start_at_login_entry", False) and self.step == 1:
            browser_check = self._verify_visible_state(
                screenshot,
                objective="确认浏览器已在前台，可以使用地址栏导航到本次登录入口",
                expected_result="截图清楚显示浏览器窗口、标签栏和地址栏；没有开始菜单、系统对话框或其他应用遮挡地址栏。不能仅凭桌面浏览器图标判定通过。",
            )
            self._last_state_verification = browser_check
            if browser_check["passed"]:
                action_data = {
                    "action": "skill_open_url",
                    "params": {"url": policy.login_url},
                    "thought": "已独立确认前台浏览器，先导航到本次登录入口，不复用上次残留的业务页面状态。",
                    "source": "verified_login_entry_navigation",
                }
        if getattr(self, "_loop_replan_requested", False):
            self._loop_replan_requested = False
            response = chat_vision(
                prompt + "\n【循环恢复：重新规划】先独立判断当前真实页面状态及上次动作是否已生效，"
                "再重新规划当前未完成步骤的执行路径。不要重置已完成步骤，不重复已执行搜索；"
                "目标和业务范围保持不变。菜单已经展开时点击目标子菜单，选项已勾选时不要再次切换。"
                "不得重复被拦截的相同动作或改名绕过。输出一个正常动作JSON，thought中说明"
                "原路径为什么卡住、新路径及本次选择；仅执行新路径的第一步，后续再看截图。",
                screenshot,
                system_prompt=getattr(self, "system_prompt", SYSTEM_PROMPT),
                temperature=0.0, image_format="PNG",
            )
            action_data = self._parse_action(response)
            action_data["source"] = "loop_replan"
        pending = self.pending_input or {}
        if action_data is None:
            action_data = direct_url_verification(pending)
        if action_data is None:
            action_data = pointer_relocation(self.history, plan)
        if (
            pending.get("verified") is not True
            and pending.get("verification_attempted") is True
            and (
                (pending.get("field_type") == "app_search"
                 and pending.get("source_skill") == "skill_open_app"
                 and self._browser_application(pending.get("text")))
                or (pending.get("field_type") == "url"
                    and pending.get("source_skill") == "skill_open_url")
            )
            and pending.get("ime_evidence") in {"windows_candidate_panel", "url_transcription_ime"}
            and pending.get("cause") == "ime"
            and self.input_retry_count == 0
            and not self.task_plan
            and policy is not None and policy.active
            and (
                pending.get("field_type") == "url"
                or self._has_windows_ime_candidate_panel(screenshot)
            )
        ):
            # The local detector has diagnosed this exact focused Start field.
            # Schedule the existing bounded repair, rather than asking the model
            # again and letting it loop over blocked Enter/click proposals.
            action_data = {
                "action": "retry_text_input",
                "params": {
                    "text": pending["text"],
                    "field_type": pending["field_type"],
                    "cause": "ime",
                },
                "thought": "本地检测到输入法候选条，自动取消组合串、切英文并重输当前字段；随后重新截图验收。",
                "source": "local_ime_recovery",
            }
        last_invalid_reason = ""
        for attempt in range(2):
            if action_data is not None:
                break
            hint = ""
            if attempt == 1 and last_invalid_reason:
                hint = f"\n注意：上一次输出无效（{last_invalid_reason}）。x/y 必须是 0~1 的相对比例值，严禁像素值。"
            vision_options = {}
            if self.pending_input and self.pending_input.get("verified") is not True:
                # 精确文本验收保留像素边缘，避免 JPEG 压缩把细小字符和光标粘连。
                vision_options = {"temperature": 0.0, "image_format": "PNG"}
            response = chat_vision(
                prompt + hint,
                screenshot,
                system_prompt=getattr(self, "system_prompt", SYSTEM_PROMPT),
                **vision_options,
            )
            candidate = self._parse_action(response)

            action = (candidate.get("action") or "").lower()
            if action in ("click", "double_click", "right_click", "move", "scroll"):
                ok, reason = self._validate_xy(candidate.get("params", {}))
                if ok:
                    action_data = candidate
                    break
                # 坐标非法，记录原因进入重试
                last_invalid_reason = reason
                print(f"  [校验] 坐标无效: {reason}，重试...")
                continue

            if action == "drag":
                ok, reason = self._validate_drag(candidate.get("params", {}))
                if ok:
                    action_data = candidate
                    break
                last_invalid_reason = reason
                print(f"  [校验] 拖拽坐标无效: {reason}，重试...")
                continue

            action_data = candidate
            break

        if action_data is None:
            # 两次均产出非法坐标，跳过执行等待画面变化
            action_data = {"action": "wait", "params": {"seconds": 1},
                           "thought": f"坐标校验失败: {last_invalid_reason}"}

        action_data = self._normalize_browser_search_request(action_data)

        # 计算 LLM 决策耗时（观察之后到 LLM+校验结束）
        t_llm = time.perf_counter() - t_obs_start - t_obs

        # 3. 坐标新鲜度：模型观察与实际输入之间再抓一帧；目标区域变化时零输入。
        pointer_actions = {"click", "double_click", "right_click", "move", "scroll", "drag"}
        action_name = str(action_data.get("action", "") or "").lower().strip()
        execution_screenshot = screenshot
        pre_action_path = None
        freshness_evidence = None
        blocked_by_stale = False
        # A focus-repair click needs the visual verifier to relocate the field;
        # all other pending-input blocks can be decided before pointer inspection.
        relocating_focus = (
            action_name == "click"
            and (self.pending_input or {}).get("cause") == "focus"
        )
        blocked_by_input = not relocating_focus and self._blocks_unverified_input(
            action_name, action_data.get("params", {})
        )
        if action_name in pointer_actions and not blocked_by_input:
            try:
                pre_action = self.vnc.screenshot()
                pre_action_path = self.saveScreenShot(
                    self.step, self.rand, pre_action, phase="pre_action"
                )
                region = self._target_region_for_action(action_data, screenshot.size)
                freshness = compare_screen_state(
                    screenshot, pre_action, target_region=region
                )
                freshness_evidence = self._diff_evidence(freshness)
                if freshness.geometry_changed or freshness.target_changed:
                    blocked_by_stale = True
                    self._stale_block_count = getattr(self, "_stale_block_count", 0) + 1
                    self._repeat_guidance = (
                        "模型决策后目标区域已经变化，坐标已过期；本步未发送任何输入。"
                        "请基于最新截图重新定位目标。"
                    )
                    if self._stale_block_count >= 2:
                        self.failure_reason = "坐标连续两次在执行前失效，已安全终止"
                        self._abort_requested = True
                else:
                    self._stale_block_count = 0
                    execution_screenshot = pre_action
            except Exception as exc:
                blocked_by_stale = True
                self.failure_reason = f"执行前截图校验失败：{exc}"
                self._repeat_guidance = self.failure_reason
                self._abort_requested = True

        # 4. 周期循环防护：识别 period 1/2/3，而不是只比较相邻动作。
        if blocked_by_stale or blocked_by_input:
            blocked_by_loop, loop_reason, loop_evidence = False, "", None
            print(f"  [新鲜度] {self._repeat_guidance}")
        else:
            blocked_by_loop, loop_reason, loop_evidence = self._loop_decision(
                action_data, execution_screenshot
            )
        if blocked_by_loop:
            print(f"  [循环防护] {loop_reason}，跳过输入事件")
            if action_name == "scroll":
                self._repeat_guidance = (
                    loop_reason
                    + "。上一次滚轮后弹层画面没有变化，本步未发送滚轮事件。"
                    "不得重复完全相同的 x/y/amount；若滚动条仍可向下，保持 target="
                    "\"测试类型下拉弹层内容区\"，在弹层内部选择一个明显不同的点"
                    "（与旧点至少相差 0.03）并改用 amount=-5 做唯一一次恢复；"
                    "若已经到底则停止滚动并按可见目标继续。不得用 wait 绕过循环门。"
                )
            else:
                self._repeat_guidance = (
                    loop_reason
                    + "。请重新观察并换一种有明确后验的操作；不得用 wait 穿插后重放同一循环。"
                )
                if "体验中心" in str(action_data.get("params", {}).get("target", "")):
                    self._repeat_guidance += (
                        " 体验中心是展开/收起菜单，不是页面入口。重点检查它下面是否已经"
                        "显示缩进的‘智能体应用’和‘智能体能力’；若已显示，应点击下方"
                        "‘智能体能力’，不要再次点击体验中心，也不要点击智能体应用。"
                    )
        elif not blocked_by_stale and not blocked_by_input:
            self._repeat_guidance = ""

        # 5. 查询页面的指针落点还须由独立视觉门确认，不能只信任模型自报 target。
        pointer_target_evidence = None
        blocked_by_target = False
        policy = getattr(self, "task_policy", None)
        if (
            policy is not None
            and policy.active
            and action_name in {"click", "double_click", "move", "scroll"}
            and not blocked_by_stale
            and not blocked_by_loop
            and not blocked_by_input
        ):
            pointer_target_evidence = self._verify_query_pointer_target(
                execution_screenshot, action_data
            )
            if not pointer_target_evidence["passed"]:
                blocked_by_target = True
                self._query_target_block_count = (
                    getattr(self, "_query_target_block_count", 0) + 1
                )
                suggestion = ""
                if (
                    pointer_target_evidence.get("suggested_x") is not None
                    and pointer_target_evidence.get("suggested_y") is not None
                ):
                    suggestion = (
                        " 独立验收器给出的目标中心候选为 "
                        f"x={pointer_target_evidence['suggested_x']:.4f}, "
                        f"y={pointer_target_evidence['suggested_y']:.4f}；"
                        "下一步必须把 params.x/y 更新为该候选，"
                        "候选仍会经过新的红色十字二次验收。"
                    )
                self._repeat_guidance = (
                    "独立落点验收未确认声明目标，本步未发送任何鼠标事件："
                    + pointer_target_evidence.get("reason", "目标或落点不清晰")
                    + suggestion
                    + f"。本次完整截图为 {execution_screenshot.width}×"
                    + f"{execution_screenshot.height}，归一化坐标必须使用完整截图尺寸换算。"
                    + "请根据最新截图重新定位，不得改写 target 名称绕过。"
                )
                if self._query_target_block_count >= 3:
                    self.failure_reason = "连续三次无法确认查询页面鼠标落点，任务安全终止"
                    self._abort_requested = True
            else:
                self._query_target_block_count = 0
                pending = self.pending_input if isinstance(self.pending_input, dict) else {}
                pointer_params = action_data.get("params", {})
                if (
                    action_name == "click"
                    and pending.get("verified") is not True
                    and pending.get("cause") == "focus"
                    and not pending.get("refocused")
                    and isinstance(pointer_params, dict)
                    and self._target_matches_pending_field(
                        pointer_params.get("target", ""),
                        pending.get("field_type", "auto"),
                    )
                ):
                    # The old coordinate demonstrably missed.  Only the
                    # independent red-marker verifier may authorize replacing
                    # it with a newly observed point for the same field.
                    pending["focus_coordinates"] = (
                        float(pointer_params["x"]),
                        float(pointer_params["y"]),
                    )

        # 6. 执行
        t_exec_start = time.perf_counter()
        if blocked_by_loop or blocked_by_stale or blocked_by_target or blocked_by_input:
            self._last_action_executed = False
            action_executed = False
        else:
            self._execute_action(action_data, execution_screenshot)
            action_executed = self._last_action_executed

        # 动作派发并不等于页面生效。对会改变界面的动作保存 after 帧和视觉证据。
        after_path = None
        visual_evidence = None
        visual_actions = {
            "click", "double_click", "right_click", "move", "scroll", "drag",
            "press", "maximize_window", "type", "input_text", "retry_text_input",
            "ime_operation", "skill_open_app", "skill_open_url", "skill_open_webpage",
            "skill_input_text", "skill_input_method", "skill_ime",
        }
        if action_executed and action_name in visual_actions:
            try:
                time.sleep(0.6)
                after_screenshot = self.vnc.screenshot()
                after_path = self.saveScreenShot(
                    self.step, self.rand, after_screenshot, phase="after"
                )
                region = self._target_region_for_action(
                    action_data, execution_screenshot.size
                )
                visual_result = compare_screen_state(
                    execution_screenshot, after_screenshot, target_region=region
                )
                visual_evidence = self._diff_evidence(visual_result)
                visual_evidence["status"] = (
                    "visual_change" if visual_result.changed else "no_visual_change"
                )
                query_target = str(
                    (action_data.get("params", {}) or {}).get("target", "") or ""
                ).lower()
                if (
                    policy is not None
                    and policy.active
                    and action_name == "click"
                    and any(option in query_target for option in ("智能问数", "新sql生成"))
                    and visual_result.changed
                ):
                    self._repeat_guidance = (
                        "该测试类型选项点击已产生可见变化；下一步不得重复点击同一选项。"
                        "请核对当前计划要求的勾选/已选标签：正确则调用 subtask_done，"
                        "不正确则重新定位当前计划指定的唯一选项。"
                    )
                if self._is_query_search_click(action_name, action_data.get("params", {})):
                    evidence = getattr(self, "_query_last_search_evidence", None)
                    if isinstance(evidence, dict) and evidence.get("step") == self.step:
                        evidence.update({
                            "before": before_path,
                            "pre_action": pre_action_path,
                            "after": after_path,
                            "after_captured": True,
                            "verification_frame": after_path,
                            "verification": dict(visual_evidence),
                        })
            except Exception as exc:
                visual_evidence = {"status": "verification_error", "reason": str(exc)}
                self.failure_reason = f"动作后截图验收失败：{exc}"
                self._repeat_guidance = self.failure_reason
                self._abort_requested = True
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
            "executed": action_executed,
            "duration": duration,
            "screenshots": {
                "before": before_path,
                "pre_action": pre_action_path,
                "after": after_path,
            },
        }
        if action_data.get("source"):
            step_record["source"] = action_data["source"]
        if action_data.get("requested_action"):
            step_record["requested_action"] = action_data["requested_action"]
        if action_name == "verify_text_input" and self.pending_input:
            pending = self.pending_input
            step_record["input_verification"] = {
                "passed": pending.get("verified") is True,
                "cause": pending.get("cause", "unknown"),
                "local_evidence": pending.get("ime_evidence"),
            }
        if not action_executed or (
            step_record.get("input_verification", {}).get("passed") is False
        ):
            step_record["execution_feedback"] = self._repeat_guidance or self.failure_reason
        if freshness_evidence is not None:
            step_record["freshness"] = freshness_evidence
        if visual_evidence is not None:
            step_record["verification"] = visual_evidence
        if self._last_state_verification is not None:
            step_record["state_verification"] = self._last_state_verification
        if loop_evidence is not None:
            step_record["loop_guard"] = loop_evidence
        if pointer_target_evidence is not None:
            step_record["pointer_target_verification"] = pointer_target_evidence
        if plan is not None:
            step_record["subtask"] = {
                "index": self.current_plan_idx + 1,
                "total": len(self.task_plan),
                "content": plan["content"],
                "expected_result": plan["expected_result"],
            }
        self.history.append(step_record)
        if action_executed:
            observations = getattr(self, "_executed_observations", [])
            observations.append(execution_screenshot.copy())
            self._executed_observations = observations[-12:]

        if progress_callback:
            progress_callback(step_record)

        # 记录本步观察帧，供下一步画面差异检测（判断本步动作是否引起变化）
        self._prev_screenshot = locals().get("after_screenshot", execution_screenshot)

        # 非视觉动作仍给界面一个很短的调度窗口。
        if not (action_executed and action_name in visual_actions):
            time.sleep(0.2)

        # 5. 返回阶段状态码
        if self._abort_requested:
            return "failed"
        if action_name == "done" and action_executed:
            return "done"
        if plan is None and action_name == "planning" and action_executed and self.task_plan:
            return "planned"
        if plan is not None and action_name == "subtask_done" and action_executed:
            return "subtask_done"
        if plan is not None and action_name == "discover_options" and action_executed:
            return "discover"
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
        self.pending_input = None
        self.input_retry_count = 0
        self.ime_operation_counts = {}
        self.failure_reason = ""
        self._last_action_executed = False
        self._abort_requested = False
        self.login_progress = "idle"
        self._active_login_input_stage = None
        self._active_skill_action = None
        self._last_focus_coordinates = None
        self._last_skill_expand_error = ""
        self.task_policy = QueryOnlyPolicy.for_task(task)
        if not hasattr(self, "interaction_guard"):
            self.interaction_guard = InteractionGuard(coordinate_grid=50)
        self._executed_observations = []
        self._loop_block_counts = {}
        self._scroll_counts = {}
        self._stale_block_count = 0
        self._last_state_verification = None
        self._planning_verification_failures = 0
        self._query_login_entry_verified = False
        self._query_search_click_count = 0
        self._query_last_search_evidence = None
        self._query_target_block_count = 0
        self._preplan_scope_block_count = 0
        self._query_maximize_count = 0
        self._app_launch_recovery = None
        self._app_launch_redirect_count = 0
        token_tracker.reset()
        self.vnc.connect()

        # 加载知识摘要：按任务文本匹配页面知识，注入后续每步 prompt
        self.knowledge_summary = get_summary(task)
        self._start_at_login_entry = self.task_policy.active
        business_summary = get_summary(task, phase="business")
        self._business_knowledge_block = f"\n{business_summary}\n" if business_summary else ""
        if self.knowledge_summary:
            print(f"[知识] 匹配到页面知识，已注入决策上下文")
        knowledge_block = f"\n{self.knowledge_summary}\n" if self.knowledge_summary else ""
        policy_block = f"\n{self.task_policy.prompt}\n" if self.task_policy.prompt else ""
        if self.task_policy.active:
            # 查询任务的阶段边界由代码固化，避免通用规划器把“测试”扩展成 CRUD。
            self.planning = (
                "1. 浏览器未打开时可调用 skill_open_app(application='edge')，"
                "或在独立落点验收后双击明确的 Microsoft Edge 桌面图标/"
                "单击明确的 Microsoft Edge 任务栏图标；"
                "确认浏览器位于前台；"
                "2. 只访问指定 login.html；"
                "3. 分阶段输入并验收登录信息；"
                "4. 登录成功经独立截图验收后调用 planning；"
                "5. 仅执行智能体能力页的受限查询计划。"
            )
        else:
            self.planning = chat_text(
                task + knowledge_block + policy_block,
                getattr(self, "system_prompt_planning", SYSTEM_PROMPT_PLANNING),
            )

        # 预处理：确定性地显示桌面（最小化所有窗口），避免 LLM 读到 IDE/浏览器自身页面
        # Windows: win+m（非切换式）；Linux(GNOME): super+d。macOS 没有适用于所有键盘
        # 和窗口管理设置的等价快捷键，避免误发 Cmd+D（浏览器会添加书签）。
        show_desktop_key = {"win": "win+m", "linux": "super+d"}.get(self.system)
        if show_desktop_key and not self.task_policy.active:
            self.vnc.press_key(show_desktop_key)
            time.sleep(1.0)

        try:
            # 阶段1：固定流程（打开应用/打开网址/登录），直到 planning 生成子任务计划
            while self.step < self.max_steps:
                status = self._decision_step(task, knowledge_block, None,
                                             progress_callback, control_checker)
                if status == "stop":
                    return f"任务已手动终止，共执行 {self.step} 步。"
                if status == "failed":
                    return f"任务失败：{self.failure_reason}（共执行 {self.step} 步）。"
                if status == "done":
                    return f"任务完成！共执行 {self.step} 步。"
                if status == "planned":
                    break

            # 阶段2：子任务执行，while 循环逐个推进（支持 discover_options 动态扩展子任务）
            if self.task_plan:
                plan_idx = 0
                while plan_idx < len(self.task_plan) and self.step < self.max_steps:
                    plan = self.task_plan[plan_idx]
                    self.current_plan_idx = plan_idx
                    print(f"[规划] 开始子任务 {plan_idx + 1}/{len(self.task_plan)}: {plan['content']}")
                    while self.step < self.max_steps:
                        status = self._decision_step(task, knowledge_block, plan,
                                                     progress_callback, control_checker)
                        if status == "stop":
                            return f"任务已手动终止，共执行 {self.step} 步。"
                        if status == "failed":
                            return f"任务失败：{self.failure_reason}（共执行 {self.step} 步）。"
                        if status == "done":
                            return f"任务完成！共执行 {self.step} 步。"
                        if status == "discover":
                            # discover_options 已动态替换当前及之后的子任务
                            # 不推进 plan_idx，重新获取当前子任务（已被替换为第一个新子任务）
                            plan = self.task_plan[plan_idx]
                            print(f"[规划] 子任务已动态更新，当前: {plan['content']}")
                            continue
                        if status == "subtask_done":
                            # 写入本子任务结果，break 内循环后由外层 while 推进到下一子任务
                            res = self._subtask_result or {}
                            plan["completed"] = True
                            plan["actual_result"] = res.get("result", "")
                            plan["passed"] = res.get("passed", False)
                            self._subtask_result = None
                            break
                    plan_idx += 1

                print(f"[规划] 全部 {len(self.task_plan)} 个子任务已执行完成")

            if self.step >= self.max_steps:
                result_msg = f"已达到最大步数 ({self.max_steps})，任务可能未完成。"
            else:
                result_msg = f"任务完成！共执行 {self.step} 步。"
            return result_msg

        finally:
            self.vnc.disconnect()
            SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
            token_tracker.save(str(SUMMARY_DIR / f"token_usage_{self.rand}.json"))

    def saveScreenShot(
        self,
        step: int,
        rand: int,
        screenshot: Image.Image,
        phase: str = "before",
    ) -> str:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        safe_phase = re.sub(r"[^a-z0-9_-]", "_", str(phase or "before").lower())
        filename = (
            f"sh_{rand}_{step}.png"
            if safe_phase == "before"
            else f"sh_{rand}_{step}_{safe_phase}.png"
        )
        path = SCREENSHOT_DIR / filename
        screenshot.save(path)
        return str(path.resolve())


if __name__ == "__main__":
    # 简单测试
    agent = RPAgent(vnc_host="localhost", vnc_port=5901, vnc_password="123456")
    result = agent.run_task("打开终端")
    print(result)
