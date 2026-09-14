"""Compact decision context without changing the full audit history."""
import json
import os
import re


def lean_query_enabled(active):
    return bool(active) and os.environ.get("RPA_LEAN_QUERY", "1") != "0"


def direct_stage_check(history, plan, login_progress):
    """Propose verification, never completion: executor's independent gate decides."""
    if not history:
        return None
    last = history[-1]
    if last.get("executed") is not True:
        return None
    if plan is None:
        if (login_progress == "submitted" and last.get("action") == "skill_open_webpage"
                and (last.get("params") or {}).get("stage") == "submit"):
            return {"action": "planning", "params": {}, "source": "direct_stage_check",
                    "thought": "提交登录后直接进入已有的独立登录结果验收，未通过不能生成业务计划。"}
        return None
    if (last.get("action") == "click" and
            (last.get("subtask") or {}).get("content") == plan.get("content")):
        target = re.sub(r"\s+", "", str((last.get("params") or {}).get("target", "")))
        label = re.sub(r"(?:菜单项|选项|输入框|图标|按钮)$", "", target)
        expected = re.sub(r"\s+", "", str(plan.get("expected_result", "")))
        content = str(plan.get("content", ""))
        search = ("搜索" in content and "不执行搜索" not in content and
                  any(word in target for word in ("搜索", "查询")))
        # Intermediate controls (e.g. expanding a parent menu) do not establish
        # the stage result. Probe only an explicit result label or Search.
        if not search and not (label and label in expected):
            return None
        return {"action": "subtask_done", "params": {"passed": True, "result": "请求独立截图验收当前步骤"},
                "source": "direct_stage_check", "thought": "本阶段点击已执行，直接请求独立截图检查；是否完成由原验收器决定。"}
    return None


def query_system_prompt(original, os_hint, active):
    if not lean_query_enabled(active):
        return original
    return """你是通过截图和VNC键鼠操作的GUI智能体。只执行用户授权的只读查询流程，每步只输出一个JSON：
{"action":"动作名","params":{},"thought":"简短的可见依据及本次目的"}。
以本步新截图、执行器状态、当前子任务为准。历史中executed=false的动作从未执行，不能当成成功证据。

动作与参数：
- click/double_click/move(x,y,target)：目标须截图可见；按本步完整截图尺寸归一化坐标到0..1，取真实控件中心，不使用假定分辨率。执行前仍有独立落点和画面新鲜度检查。
- scroll(x,y,amount,target)：在可见滚动容器内部，amount为-5..5非零整数，负数向下。滚后重新观察，不重复无效滚动。
- press(key)：单键或以+连接的组合键。只用当前系统、当前焦点和任务策略允许的键。不得用键盘绕过控件白名单。
- wait(seconds)：0.5..3秒，仅等待加载或不清晰截图；无进展不能无限等待。
- maximize_window()：浏览器未最大化时使用。
- skill_open_app(application)：打开系统应用搜索并输入名称，不自动回车。先验收，再press(enter)启动；未看到浏览器窗口不能输入网址。
- skill_open_url(url,browser)：仅当前台浏览器已确认时，聚焦地址栏并替换网址，不自动提交。验收通过后直接press(enter)，搜索建议无需先关闭；不得交给Win+R或终端。
- skill_open_webpage(needlogin,stage,username,password,loginCoordinates,pdCoordinates,buttonCoordinates)：分阶段登录，stage=username/password/submit。username用username与loginCoordinates；password用password与pdCoordinates；submit用buttonCoordinates。坐标对象为{x,y}。严格依次输入账号→验收→密码→验收→提交；预填文字不是本次登录证据。
- skill_input_text(text,field_type,replace)：已聚焦字段输入一次，不提交；replace默认true。field_type为text/url/username/password/number/code/email/app_search等。
- verify_text_input(observed_text,readable,cause,ime_visible)：按截图抄录实际文字，密码只报告可见掩码字符；cause为unknown/ime/focus/partial/format。不得复述预期文本当作识读。光标、选区、占位符、其他字段不是实际输入；看不清必须readable=false。
- retry_text_input(text,field_type,cause)：仅纠正当前字段，原因必须来自验收；每字段最多一次。焦点错误先重新定位原输入框，验收后再纠错。只有真实候选框/组合串等证据才能使用ime，禁止盲切输入法。
- skill_input_method(operation,candidate)：具名输入法操作；仅按已诊断原因使用，不重复切换，不改用剪贴板。
- abort_input(reason)：一次纠错后仍无法可靠验收时停止输入，不强行提交。
- planning()：本次登录完成后生成受限计划；已有计划不得重建或动态扩展。
- subtask_done(result,passed)：当前步骤达到可见预期才请求独立验收；没有勾选/标签不能宣称已选，没有结果证据不能宣称已搜索。
- done()：所有子任务均通过后才能结束。pause/stop响应外部控制。

交互要求：
1. 输入未验收前，不得点击其他字段、Enter/Tab提交、规划或宣告完成。首次不猜测输入法状态，使用既有技能；一次纠错仍失败按执行器反馈处理。网址ASCII标点须精确，特别区分冒号与分号。
2. 输入框以文字行/图标的垂直中心为准，不点击相邻行留白。浏览器保存密码提示只能关闭，不保存。
3. 菜单已展开时点击目标子项，不反复切换父菜单。多选先读当前勾选/标签，已正确的值不再次点击；相邻选项必须准确区分。
4. 下拉列表只在自身容器滚动。搜索按钮可见且无遮挡时直接按计划搜索，不为收起下拉框增加多余动作；确需收起时Escape一次或点击已验收的原框头部，再观察已选值保留。
5. 严格遵守附带业务范围和固定步骤，禁止新增/修改/删除/保存等写操作、其他业务页面和无关测试。搜索次数由执行器控制，不重复提交来验证结果。
6. 被拦截后遵循明确原因与修正建议，重新观察，不改目标名称绕过检查。循环时只重新规划当前未完成步骤，不重置已完成步骤。
7. 以最终可见状态判断完成，不靠模型自述。独立验收未通过须继续处理或如实失败。
当前系统操作方式：
""" + os_hint


def pointer_relocation(history, plan):
    """Reuse a verifier suggestion once; the normal fresh-frame gates still run."""
    if not history or os.environ.get("RPA_POINTER_RELOCATION", "1") == "0":
        return None
    last = history[-1]
    evidence = last.get("pointer_target_verification") or {}
    if (last.get("executed") is not False or evidence.get("passed") is not False
            or last.get("source") == "verified_pointer_relocation"
            or last.get("action") not in {"click", "double_click", "move"}):
        return None
    if plan is not None and (last.get("subtask") or {}).get("content") != plan.get("content"):
        return None
    x, y = evidence.get("suggested_x"), evidence.get("suggested_y")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in (x, y)):
        return None
    params = dict(last.get("params") or {})
    params.update(x=x, y=y)
    return {"action": last["action"], "params": params,
            "thought": "沿用独立验收器对同一目标的修正坐标，重新截图核验落点后才允许执行。",
            "source": "verified_pointer_relocation"}


def use_business_knowledge(plan, pending, guidance):
    return (os.environ.get("RPA_PHASE_KNOWLEDGE", "1") != "0"
            and plan is not None and not guidance
            and not (pending and not pending.get("verified")))


def login_for_prompt(progress, entry_verified, login_url):
    """Expose executor state, never infer verification from prefilled values."""
    next_action = {
        "idle": "在指定登录页调用 skill_open_webpage(stage='username')，替换输入本次账号",
        "username_pending": "先 verify_text_input 验收本次用户名输入",
        "username_verified": "调用 skill_open_webpage(stage='password') 输入密码",
        "password_pending": "先 verify_text_input 验收本次密码输入",
        "password_verified": "调用 skill_open_webpage(stage='submit') 提交登录",
        "submitted": "观察登录结果，确认平台页面后调用 planning",
    }.get(progress, "按当前待验收字段和执行器反馈继续")
    return (f"\n【执行器登录状态】{progress}；本次入口已确认：{entry_verified}。"
            f"本阶段下一步：{next_action}。登录入口：{login_url}。"
            "页面预填账号或密码不代表本次输入已经验收，不得跳过阶段；"
            "输入框坐标取对应文字行和图标的垂直中心，不要点击两行间的空白。"
            "仅在尚未开始本次登录且当前不在登录入口时，先用 skill_open_url 打开入口并验收网址；已提交登录后不要重新导航入口。"
            "不要把历史已登录状态当成本次登录完成。\n")


def direct_url_verification(pending):
    if (os.environ.get("RPA_DIRECT_URL_VERIFY", "1") != "0"
            and pending and pending.get("field_type") == "url"
            and pending.get("source_skill") == "skill_open_url"
            and not pending.get("verified") and not pending.get("verification_attempted")):
        # The existing verifier reads the actual screenshot, without target text.
        return {"action": "verify_text_input", "params": {},
                "thought": "网址输入后直接进入独立地址栏识读验收，不重复调用通用决策模型。",
                "source": "direct_url_verification"}
    return None


def history_for_prompt(history, lean=False):
    recent = history[-5:]
    if os.environ.get("RPA_COMPACT_HISTORY", "1") == "0":
        return json.dumps(recent, ensure_ascii=False, indent=2) if recent else "（无，这是第一步）"
    # Drop transport/measurement details only. Preserve action parameters,
    # execution status, independent evidence, and failure/recovery feedback.
    omitted = {"screenshots", "duration", "time", "verification_frame",
               "full_score", "max_tile_score", "target_score", "target_region"}

    def compact(value):
        if isinstance(value, dict):
            return {key: compact(item) for key, item in value.items() if key not in omitted}
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value

    if lean:
        concise = []
        for index, record in enumerate(recent):
            row = {key: record[key] for key in ("step", "action", "params", "executed", "source") if key in record}
            for key in ("input_verification", "execution_feedback", "loop_guard"):
                if record.get(key) is not None:
                    row[key] = compact(record[key])
            state = record.get("state_verification") or {}
            if state:
                row["state_verification"] = {key: state[key] for key in ("passed", "state", "reason") if key in state}
            pointer = record.get("pointer_target_verification") or {}
            if pointer:
                row["pointer_target_verification"] = {key: pointer[key] for key in (
                    "passed", "reason", "target_bbox", "suggested_x", "suggested_y") if key in pointer}
            if index == len(recent) - 1 and record.get("thought"):
                row["thought"] = record["thought"]
            concise.append(row)
        return json.dumps(concise, ensure_ascii=False, separators=(",", ":")) if concise else "（无，这是第一步）"
    return json.dumps(compact(recent), ensure_ascii=False, separators=(",", ":")) if recent else "（无，这是第一步）"
