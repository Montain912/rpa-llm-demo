---
name: "rpa-macro-skill"
description: "为 rpa-llm-demo 的 GUI Agent 添加确定性宏动作技能（macro skill）。当需要把多步固定流程（如打开应用、网页登录、填表、导航）封装成 LLM 一次调用即可顺序执行的技能时调用；规范 skills.py 函数定义、SYSTEM_PROMPT 动作说明、rpa_agent 宏展开与坐标换算的完整接法。"
---

# RPA Agent 确定性宏动作技能开发规范

适用于 `rpa-llm-demo` 项目。把"多步固定操作流程"封装成一个 **宏技能（macro skill）**：视觉 LLM 只做一次判断（给应用名 / 识别少量坐标），技能在**一个决策步内**展开为一串确定性子动作顺序执行，避免在多个微操作间反复截图决策导致的坐标漂移、重复误判和空转。

## 何时使用本规范

- 用户要求把一段固定 GUI 操作流程（打开应用、网页登录、表单填写、固定导航路径等）抽象成"技能 / skill / 宏"
- 发现 LLM 在某类多步操作上反复出错（如登录填表：点框→输入→点框→输入→点按钮），且流程本身是确定的
- 需要新增一个 `skill_*` 动作类型给 Agent 调用

## 核心架构（四处改动，缺一不可）

```
skills.py (新增纯函数)  ──返回──►  action dict 列表
        ▲
        │ 调用
rpa_agent.py
  ├─ _expand_skill()      把 skill_* 动作+参数 → skills.py 函数
  ├─ _execute_action()    skill_* 分支：循环执行子动作
  └─ SYSTEM_PROMPT        声明新动作 + JSON 调用示例给 LLM
```

## 1. action dict 格式（全局统一）

所有动作（基础动作与技能子动作）都是同一结构，可直接喂给 `_execute_action`：

```python
{"action": "<动作类型>", "params": {...}, "thought": "<可选说明>"}
```

基础动作类型：`click`(params: x,y)、`double_click`(x,y)、`right_click`(x,y)、`type`(text)、`press`(key，支持 `ctrl+n` 组合键)、`scroll`(direction)、`wait`(seconds 0.5~3)、`done`。

**坐标约定（铁律）**：click 类的 `x/y` 必须是 **0~1 归一化比例**，由视觉 LLM 识别；像素换算由 `_to_pixels(params, screenshot)` 基于当步截图尺寸完成。技能函数内部**绝不做分辨率/缩放换算**。

## 2. skills.py 函数写法

- 纯函数，`from __future__ import annotations`（兼容低版本 Python 的类型注解）
- 入参为业务参数（应用名、坐标、文本等），**返回 `list[dict]` 动作步骤列表**
- 提供 `_wait/_press/_type/_click` 等小工厂函数构造 action dict，保持简洁
- 参数非法（如缺坐标）时 **抛 `ValueError`**，由 agent 层兜底
- 纯键盘流程（如开始菜单搜索）优先于依赖坐标的流程，更稳

参考实现（打开应用）：
```python
def open_application(application: str, system: str = "win") -> list[dict]:
    if not application:
        return []
    if system == "mac":
        return [_press("cmd+space"), _wait(1), _type(application),
                _wait(1), _press("enter"), _wait(2)]
    return [_press("win"), _wait(1), _type(application), _wait(1),
            _press("enter"), _wait(2), _press("win+up"), _wait(1)]
```

参考实现（网页登录，坐标由 LLM 一次性传入）：
```python
def open_webpage(needlogin=False, username="", password="",
                 loginCoordinates=None, pdCoordinates=None, buttonCoordinates=None):
    if not needlogin:
        return []
    for name, coord in (("loginCoordinates", loginCoordinates),
                        ("pdCoordinates", pdCoordinates),
                        ("buttonCoordinates", buttonCoordinates)):
        if not coord or "x" not in coord or "y" not in coord:
            raise ValueError(f"open_webpage 缺少必要坐标参数: {name}")
    return [
        _click(loginCoordinates["x"], loginCoordinates["y"]), _wait(0.5),
        _type(username),
        _click(pdCoordinates["x"], pdCoordinates["y"]), _wait(0.3),
        _type(password),
        _click(buttonCoordinates["x"], buttonCoordinates["y"]), _wait(2),
    ]
```

## 3. rpa_agent.py 三处接法

**(a) 导入**：`from skills import open_application, open_webpage`

**(b) `_expand_skill(skill_action, params)`**：动作名 → skills.py 函数；做参数兼容（如 bool/字符串 `"true"` 互转）；`try/except` 兜底，异常时返回单步 wait 提示 LLM 改用基础动作：
```python
def _expand_skill(self, skill_action, params):
    try:
        if skill_action == "skill_open_app":
            return open_application(application=params.get("application",""),
                                    system=params.get("system","win"))
        if skill_action == "skill_open_webpage":
            needlogin = params.get("needlogin", False)
            if isinstance(needlogin, str):
                needlogin = needlogin.strip().lower() in ("true","1","yes","是")
            return open_webpage(needlogin=bool(needlogin), ...)
    except Exception as e:
        return [{"action":"wait","params":{"seconds":1},
                 "thought":f"技能参数非法: {e}，请改用基础动作分步操作"}]
    return []
```

**(c) `_execute_action` 新增 skill 分支**：在 `done` 分支之后、`else 未知动作` 之前。展开后循环执行，**子动作统一用宏触发时的当步截图换算坐标**（表单字段在流程中不移动，安全）：
```python
elif action in ("skill_open_app", "skill_open_webpage"):
    sub_steps = self._expand_skill(action, params)
    for i, sub in enumerate(sub_steps, 1):
        print(f"  [技能 {i}/{len(sub_steps)}] {sub.get('action')} {sub.get('params')}")
        self._execute_action(sub, screenshot)   # 递归复用，子动作不含 done
```

## 4. SYSTEM_PROMPT 动作声明

在操作类型列表追加编号条目，**必须带 JSON 调用示例**，并明确"无需分步操作"：
```
9. skill_open_app(application) - 【固定流程技能】打开指定应用，自动搜索启动并最大化，无需分步点击。
   例：{"action": "skill_open_app", "params": {"application": "edge"}}
10. skill_open_webpage(needlogin, username, password, loginCoordinates, pdCoordinates, buttonCoordinates)
   - 【固定流程技能】看到登录表单时，一次识别用户名框/密码框/登录按钮三个归一化坐标传入，
     系统自动按固定流程填表登录，无需分步。
   例：{"action": "skill_open_webpage", "params": {"needlogin": true, "username": "admin",
        "password": "123456", "loginCoordinates": {"x":0.5,"y":0.24},
        "pdCoordinates": {"x":0.5,"y":0.30}, "buttonCoordinates": {"x":0.46,"y":0.40}}}
```

## 关键注意事项

1. **技能在一个 LLM 决策步内执行完**：子动作不重新截图、不重新问 LLM，因此技能内部流程必须确定、且目标元素在流程中不发生位移。
2. **`skill_*` 不参与重复动作拦截**：`_is_repeat` 对未知/skill 类型返回 False，宏不会被重复防护逻辑打断。
3. **坐标只信视觉 LLM 的归一化值**：技能函数不要写死像素坐标，也不要做 DPI/分辨率换算。
4. **等待要内置**：每个可能触发加载/动画的子动作后，用 `_wait()` 留足响应时间（应用启动 2s、窗口最大化 0.8~1s、点击聚焦 0.3~0.5s）。
5. **参数容错在 agent 层**：bool/字符串兼容、缺参兜底为 wait，不要让技能异常中断整个任务。
6. **新增技能后自测**：语法检查 + 直接调用 skills.py 函数打印步骤列表，确认步数与参数正确；缺参场景确认抛错/兜底生效。
