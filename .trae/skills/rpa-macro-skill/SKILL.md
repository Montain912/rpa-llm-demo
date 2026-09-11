---
name: "rpa-macro-skill"
description: "为 rpa-llm-demo 的 GUI Agent 添加确定性宏动作技能（macro skill）。规范 tools.py 函数定义、输入后验收边界、SYSTEM_PROMPT 动作说明、rpa_agent 宏展开与坐标换算的完整接法。"
---

# RPA Agent 确定性宏动作技能开发规范

适用于 `rpa-llm-demo` 项目。把"多步固定操作流程"封装成一个 **宏技能（macro skill）**。纯点击/按键流程可以在一个决策步内展开；任何文本字段都必须把“输入完成”作为宏边界，返回观察循环逐字符验收后，才能继续切换字段、回车或提交。

> 输入安全边界：禁止把“账号输入 → 密码输入 → 点击提交”放在同一宏中。使用 `input_text` 后必须等待 `verify_text_input`，失败时最多调用一次 `retry_text_input`。

## 何时使用本规范

- 用户要求把一段固定 GUI 操作流程（打开应用、网页登录、表单填写、固定导航路径等）抽象成"技能 / skill / 宏"
- 发现 LLM 在某类多步操作上反复出错（如登录填表：点框→输入→点框→输入→点按钮），且流程本身是确定的
- 需要新增一个 `skill_*` 动作类型给 Agent 调用

## 核心架构（四处改动，缺一不可）

```
tools.py (新增纯函数)   ──返回──►  action dict 列表
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

基础动作类型：`click`(params: x,y)、`double_click`(x,y)、`right_click`(x,y)、`type`(兼容旧追加输入)、`input_text`(text, field_type, replace)、`verify_text_input`、`retry_text_input`、`press`、`scroll`、`wait`、`done`。

**坐标约定（铁律）**：click 类的 `x/y` 必须是 **0~1 归一化比例**，由视觉 LLM 识别；像素换算由 `_to_pixels(params, screenshot)` 基于当步截图尺寸完成。技能函数内部**绝不做分辨率/缩放换算**。

## 2. tools.py 函数写法

- 纯函数，`from __future__ import annotations`（兼容低版本 Python 的类型注解）
- 入参为业务参数（应用名、坐标、文本等），**返回 `list[dict]` 动作步骤列表**
- 提供 `_wait/_press/_type/_input_text/_click` 等小工厂函数构造 action dict；表单字段必须用 `_input_text`
- 参数非法（如缺坐标）时 **抛 `ValueError`**，由 agent 层兜底
- 纯键盘流程（如开始菜单搜索）优先于依赖坐标的流程，更稳

参考实现（打开应用）：
```python
def open_application(application: str, system: str = "win") -> list[dict]:
    if not application:
        return []
    if system == "mac":
        return [_press("cmd+space"), _wait(1),
                _input_text(application, "app_search"), _wait(0.5)]
    return [_press("win"), _wait(1),
            _input_text(application, "app_search"), _wait(0.5)]
```

参考实现（网页登录，按可验收状态分阶段）：
```python
def open_webpage(needlogin=False, username="", password="",
                 loginCoordinates=None, pdCoordinates=None,
                 buttonCoordinates=None, stage="username"):
    if not needlogin:
        return []
    if stage == "username":
        return [_click(loginCoordinates["x"], loginCoordinates["y"]), _wait(0.3),
                _input_text(username, "username", replace=True), _wait(0.5)]
    if stage == "password":
        return [_click(pdCoordinates["x"], pdCoordinates["y"]), _wait(0.3),
                _input_text(password, "password", replace=True), _wait(0.5)]
    if stage == "submit":
        return [_click(buttonCoordinates["x"], buttonCoordinates["y"]), _wait(2)]
    raise ValueError("stage 必须是 username/password/submit")
```

## 3. rpa_agent.py 三处接法

**(a) 导入**：`from tools import input_text, open_application, open_webpage`

**(b) `_expand_skill(skill_action, params)`**：动作名 → `tools.py` 函数；做参数兼容（如 bool/字符串 `"true"` 互转）；参数异常必须记录为技能失败并停止，不能伪装成 wait 成功：
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
        if not self._execute_action(sub, screenshot):
            return False  # 输入失败时不得继续回车或提交
```

## 4. SYSTEM_PROMPT 动作声明

在操作类型列表追加编号条目，**必须带 JSON 调用示例**，并明确输入后的验收边界：
```
9. skill_open_app(application) - 打开应用搜索并输入名称；验收后再单独按 Enter。
   例：{"action": "skill_open_app", "params": {"application": "edge"}}
10. skill_open_webpage(needlogin, stage, ...) - username/password/submit 三阶段调用；
    每个输入阶段之后必须 verify_text_input，验收前不得进入下一阶段。
```

## 关键注意事项

1. **宏只执行到下一个可验收状态**：纯按键/点击子动作可连续执行；一旦输入字段，就 wait 并返回观察循环，禁止盲目提交。
2. **`skill_*` 不参与重复动作拦截**：`_is_repeat` 对未知/skill 类型返回 False，宏不会被重复防护逻辑打断。
3. **坐标只信视觉 LLM 的归一化值**：技能函数不要写死像素坐标，也不要做 DPI/分辨率换算。
4. **等待要内置**：点击聚焦后等待 0.3s，输入后等待 0.5s 以便截图验收；验收通过后的加载等待由下一动作负责。
5. **参数容错在 agent 层**：bool/字符串兼容；缺参记录明确失败并停止该宏，不能把未执行的技能记成成功。
6. **新增技能后自测**：语法检查 + 直接调用 tools.py 函数确认步骤；还必须验证未验收字段会阻止 Enter/Tab/点击/提交，子动作失败能向上传播。

## 已验证页面经验：智能体能力页退出登录

适用于“体验中心 → 智能体能力”只读查询任务。用户提供的局部截图已确认，该页面不是通过用户名下拉菜单退出，而是直接点击顶部栏图标：

- 视觉锚点：绿色顶部栏最右侧、用户姓名“周昊”及其下拉箭头的右边；按钮没有文字，图形是浅色/白色开口门框加向右箭头，箭头朝右并穿出门框。
- 动作语义：对模型统一声明 `target="退出登录图标"`，不要使用“用户菜单”“头像”或仅写“箭头”等含糊目标。
- 执行边界：只有查询结果及前置步骤均已验收后，才独立校验图标落点并点击一次；不得先点击用户姓名，也不得反复点击退出图标。
- 成功后验：重新出现指定 `login.html` 的登录表单，且智能体能力查询页不再可见；仅发生视觉变化或图标被点击都不等于退出成功。
- 稳定性：经验只保存图标外观和相对位置，不保存绝对像素或归一化坐标。分辨率、缩放或顶部栏布局变化后必须重新定位。
