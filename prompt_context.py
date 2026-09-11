"""Compact decision context without changing the full audit history."""
import json
import os


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


def history_for_prompt(history):
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

    return json.dumps(compact(recent), ensure_ascii=False, separators=(",", ":")) if recent else "（无，这是第一步）"
