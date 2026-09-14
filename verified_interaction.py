"""Generic visual checks around input and pointer operations (no site-specific plan)."""
from __future__ import annotations

import json
import re
import time
from PIL import Image, ImageDraw

from interaction_guard import InteractionGuard, compare_screen_state, validate_normalized_coordinates
from runtime_paths import runtime_path
from pointer_context import stable_pointer_crop


class ObservationRequired(RuntimeError):
    """No further native events may be sent until the planner observes again."""


class VerifiedInteraction:
    def __init__(self, agent, vision):
        self.agent = agent
        self.vnc = agent.vnc
        self.vision = vision
        self.guard = InteractionGuard(coordinate_grid=100)
        self.records = []
        self.pending = None
        self.evidence = []
        self.serial = 0
        self._pointer_crop = None
        self._one_shot_search_sent = False

    def capture(self, label):
        frame = self.vnc.screenshot()
        self.serial += 1
        path = runtime_path('screenshots', f'sh_{getattr(self.agent, "rand", "preview")}_{self.agent.step}_{self.serial}_{label}.png')
        frame.save(path)
        self.evidence.append({'kind': label, 'screenshot': str(path)})
        return frame

    def inspect(self, frame, prompt):
        response = self.vision(
            prompt + '\n只返回一个 JSON 对象。不采信之前步骤的成功宣告，看不清时不得猜测。',
            frame, system_prompt='你是独立截图验收器，仅按本张截图提供可见证据。',
            temperature=0.0, image_format='PNG', max_tokens=700,
        )
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', response.strip())
        try:
            data = json.loads(cleaned)
        except (ValueError, TypeError):
            raise ObservationRequired('独立验收器未返回有效 JSON，请重新观察')
        if not isinstance(data, dict):
            raise ObservationRequired('独立验收结果不是对象')
        self.evidence.append({'kind': 'verification', 'result': data})
        return data

    def verify_state(self, objective):
        result = self.inspect(self.capture('state'),
            f'验收目标：{objective}\n返回 {{"passed":true/false,"evidence":["具体可见证据"],"reason":"原因"}}。')
        if result.get('passed') is not True or not result.get('evidence'):
            raise ObservationRequired('状态未通过独立验收：' + str(result.get('reason', result)))
        return result

    def browser_front(self):
        self.verify_state('前台确实是网页浏览器窗口，存在浏览器标签栏和地址栏。记事本、编辑器、终端、开始菜单都不通过。')

    @staticmethod
    def is_browser_application(application):
        return bool(re.search(r'\b(?:edge|msedge|chrome|chromium|firefox|brave|opera)\b|浏览器',
                              str(application), re.I))

    @classmethod
    def is_browser_launch_target(cls, target):
        text = str(target)
        if not cls.is_browser_application(text):
            return False
        if re.search(r'地址栏|输入框|标签页|菜单按钮|设置|关闭|最小化|后退|刷新', text):
            return False
        return bool(re.search(r'图标|快捷方式|最佳匹配|最近使用|任务栏|启动|应用|窗口', text)
                    or re.fullmatch(r'(?:Microsoft\s+)?(?:Edge|Chrome|Firefox|Chromium|Brave|Opera)|浏览器', text, re.I))

    def ensure_browser_maximized(self):
        """Maximize only a confirmed browser that is not already maximized."""
        if self.agent.system not in {'windows', 'linux'}:
            # macOS has no portable maximize-without-fullscreen shortcut.
            self.browser_front()
            return
        prompt = (
            '检查前台是否确实为网页浏览器（必须看见标签栏和地址栏，不能是开始菜单、记事本或终端），'
            '并检查浏览器是否已最大化到屏幕工作区：窗口贴齐顶部和左右边缘，底部可保留任务栏；'
            '还原图标通常为两个重叠方框。不能把仅占半屏的贴靠窗口当最大化。'
            '若窗口外仍有明显桌面留白且标题栏有单方框最大化按钮，则尚未最大化。'
            '返回 {"browser_front":true/false,"maximized":true/false/null,"evidence":"窗口边界和标题栏证据"}。'
        )
        changed = False
        for attempt in range(2):
            state = self.inspect(self.capture('browser_window'), prompt)
            if state.get('browser_front') is not True or not state.get('evidence'):
                raise ObservationRequired('尚未确认浏览器在前台，没有发送最大化快捷键。请等待浏览器启动或重新激活浏览器')
            if state.get('maximized') is True:
                self.evidence.append({'kind': 'browser_maximize', 'changed': changed, 'verified': True})
                return
            if state.get('maximized') is not False:
                raise ObservationRequired('无法确认浏览器窗口状态，没有发送最大化快捷键，请重新观察')
            if attempt:
                raise ObservationRequired('浏览器最大化尚未通过截图验收，暂不输入网址，请重新观察窗口状态')
            # Conditional Win+Up avoids both repeated snap-layout activation and
            # the IME-sensitive Alt+Space, X system-menu path.
            self.vnc.press_key('win+up' if self.agent.system == 'windows' else 'super+up')
            changed = True
            time.sleep(.8)

    def verify_completion(self):
        # A logout page is valid terminal evidence, not proof that earlier steps never happened.
        candidates = []
        for record in self.agent.history:
            if record.get('executed') is not True:
                continue
            paths = [v['screenshot'] for v in record.get('verification', [])
                     if v.get('kind') in {'pointer_after', 'browser_window'} and v.get('screenshot')]
            if paths:
                candidates.append((record['step'], record.get('params', {}).get('target', record['action']), paths[-1]))
        audited = []
        for step, target, path in candidates[-3:]:
            with Image.open(path) as saved:
                result = self.inspect(saved.copy(),
                    f'这是步骤 {step} 操作“{target}”之后保存的历史截图。用户任务为：{self.agent.task}。'
                    '只记录本张截图真实可见的页面身份、窗口边界、筛选标签/勾选、结果数量或空结果状态。'
                    '若任务涉及最大化，按浏览器是否贴齐屏幕工作区顶部和左右边缘、标题栏还原图标判断；'
                    'Windows 任务栏仍可见是正常最大化，不要求 F11 全屏。'
                    '不要把一张历史截图当作现在的页面；不要要求过去和未来状态同时出现。'
                    '返回 {"state":"本帧实际状态","evidence":["可见证据"]}。')
            if not result.get('evidence'):
                raise ObservationRequired('历史执行截图无法独立验收，不能宣告完成')
            audited.append({'step': step, 'target': target, **result})
        self.verify_state(
            f'用户任务：{self.agent.task}\n按时间顺序、由历史截图独立读取的证据：'
            f'{json.dumps(audited, ensure_ascii=False)}\n'
            f'已经完成的子任务：{json.dumps(self.agent.task_plan, ensure_ascii=False)}\n'
            '现在只需结合以上历史证据与本张最终截图判断任务是否完成。'
            '窗口大小或焦点这类当前状态必须以本张最新截图为准，不得用较早的普通窗口状态否定后来已最大化。'
            '浏览器贴齐工作区顶部与左右边缘、底部保留任务栏属于最大化；不需要进入 F11 全屏。'
            '如果最后要求是退出登录，那么登录页正是正确的最终状态；'
            '不得因为最终登录页没有历史查询控件，就否定已经有截图证据的登录和查询。'
            '若历史证据确实不满足要求则不通过，不得凭模型曾宣告成功来补全证据。')

    def read_ime(self):
        time.sleep(.25)  # The remote IME indicator updates after the key event.
        frame = self.capture('ime')
        tray = frame.crop((int(frame.width * .65), int(frame.height * .88), frame.width, frame.height))
        tray = tray.resize((tray.width * 3, tray.height * 3))
        result = self.inspect(tray, '逐字抄写 Windows 任务栏输入法状态标志，不要分类成中英文。'
            '只认实际可见的 中/英/A/ENG 字形，看不清返回 unknown，中文输入法名称不能当作模式标志。'
            '返回 {"glyph":"中/英/A/ENG/unknown","evidence":"标志的位置"}。')
        return self.mode_from_glyph(result)

    @staticmethod
    def mode_from_glyph(result):
        if not result.get('evidence'):
            return 'unknown'
        return {'中': 'chinese', '英': 'english', 'A': 'english', 'ENG': 'english'}.get(
            str(result.get('glyph', '')).strip().upper(), 'unknown')

    def input(self, text, field='auto', replace=True):
        field = str(field or 'auto').lower()
        if field == 'auto' and re.match(r'^(?:https?://|www\.)', str(text), re.I):
            field = 'url'
        value = self.vnc.ime.normalize_text(text, field)
        if field == 'url':
            self.ensure_browser_maximized()
            self.vnc.press_key('ctrl+l' if self.agent.system != 'mac' else 'cmd+l')
            time.sleep(.2)
        focus_frame = self.capture('focus')
        focus_prompt = (
            f'准备向 {field} 字段输入。确认键盘焦点确实在该输入控件内，必须有插入光标、字段内选区或明确焦点样式；'
            '不能仅凭鼠标位置、已有文本、占位符推断。整页多个标签被全选不算输入框焦点。'
            'url 必须是浏览器地址栏，app_search 必须是系统应用搜索框。'
            'Windows 11 的系统搜索框也可能在底部任务栏（Win+S 展开的搜索面板下方），不一定在面板顶部。'
            '输入框蓝色焦点边框/下划线是有效焦点证据；插入光标会闪烁，不要求每帧同时看见光标。'
            '同时读取该字段已有值（密码不读内容，只数掩码），占位符不算已有值。'
            '返回 {"focused":true/false,"field":"url/app_search/username/password/text",'
            '"observed_text":"已有值或空串","mask_length":0,"ime_visible":false,"evidence":"证据"}。')
        focus = self.inspect(focus_frame, focus_prompt)
        if field == 'app_search' and self.agent.system == 'windows' and focus.get('focused') is not True:
            # The taskbar search caret is only a few pixels in the full frame.
            detail = focus_frame.crop((0, int(focus_frame.height*.88), focus_frame.width, focus_frame.height))
            focus = self.inspect(detail.resize((detail.width*2, detail.height*2)), focus_prompt)
        if focus.get('focused') is not True or not focus.get('evidence'):
            raise ObservationRequired(f'{field} 输入前未确认焦点，未发送文字或全选。请重新点击目标输入框文字行中心。')
        if field == 'auto':
            field = focus.get('field', 'text')
        self.pending = {'field': field, 'text': value, 'verified': False}
        already_correct = (focus.get('mask_length') == len(value) if field == 'password'
                           else focus.get('observed_text') == value)
        if value and already_correct and focus.get('ime_visible') is False:
            self.pending['verified'] = True
            return
        # Number-only passwords do not require IME conversion. Letters and punctuation do.
        if self.agent.system == 'windows' and re.search(r'[A-Za-z:;/@]', value):
            if focus.get('ime_visible') is True:
                self.vnc.ime.cancel()
            self.vnc.ime.ensure_english(self.read_ime)
        for attempt in range(2):
            self.vnc.ime.type_value(value, field, replace=replace if attempt == 0 else True)
            time.sleep(.25)
            description = (f'密码目标长度 {len(value)}，不要识读密码内容，只数掩码圆点/星号'
                           if field == 'password' else f'预期字段 {field} 的完整文本为 {json.dumps(value, ensure_ascii=False)}')
            check = self.inspect(self.capture('input_after'), description +
                '\n逐字符读取实际内容，不要照抄目标值。浏览器联想建议不算字段值；输入法候选框可见则失败。'
                '返回 {"observed_text":"字段实际文本（密码留空）","mask_length":0,"readable":true/false,'
                '"ime_visible":true/false,"evidence":"可见证据"}。')
            actual = str(check.get('observed_text', '')).strip()
            same = check.get('mask_length') == len(value) if field == 'password' else actual == value.strip()
            if same and check.get('readable') is True and check.get('ime_visible') is False and check.get('evidence'):
                self.pending['verified'] = True
                return
            if attempt == 0 and check.get('ime_visible') is True:
                self.vnc.ime.cancel()
                if self.agent.system == 'windows':
                    self.vnc.ime.ensure_english(self.read_ime)
                continue
            if attempt == 0 and check.get('readable') is True:
                # A visible mismatch in the same focused field permits one replace, never append.
                continue
            break
        raise ObservationRequired(f'{field} 输入未通过验收，已阻止提交。重新观察原字段，不得继续追加文字。')

    def press(self, key):
        if (str(key).lower().replace(' ', '') in {'win+up', 'windows+up', 'super+up'}
                and self.is_browser_application(self.agent.task)):
            self.ensure_browser_maximized()
            return
        if self.pending and not self.pending['verified'] and str(key).lower() in {'enter', 'return', 'tab'}:
            raise ObservationRequired('当前字段尚未验收，禁止回车或跳到下一字段')
        self.vnc.press_key(key)
        if str(key).lower() in {'enter', 'return', 'tab', 'escape', 'esc'}:
            self.pending = None

    @staticmethod
    def control_role(label):
        """Detect contradictory control identities, not a site/button whitelist."""
        for role, pattern in (
            ('logout', r'退出登录|登出|注销|logout|sign out|开口门框|门框.*箭头'),
            ('password', r'密码|password'),
            ('username', r'用户名|账号|帐号|username'),
            ('login', r'登录|login|sign in'),
            ('search', r'搜索|查询|放大镜|search'),
        ):
            if re.search(pattern, label, re.I):
                return role
        return None

    def local_pointer_check(self, frame, target, x, y):
        px, py = round(x*(frame.width-1)), round(y*(frame.height-1))
        bounds, self._pointer_crop = stable_pointer_crop(frame, target, px, py, self._pointer_crop)
        crop = frame.crop(bounds)
        marker = ImageDraw.Draw(crop)
        cx, cy = px-bounds[0], py-bounds[1]
        marker.line((cx-5, cy, cx+5, cy), fill='red', width=1)
        marker.line((cx, cy-5, cx, cy+5), fill='red', width=1)
        detail = self.inspect(crop.resize((crop.width*2, crop.height*2)),
            f'这是局部放大图，红十字应命中“{target}”。逐字核对实际命中的菜单/选项/输入框，'
            f'只检查后加的红十字中心（本局部图归一化 x={cx/crop.width:.4f}, y={cy/crop.height:.4f}）。'
            '白色鼠标箭头和悬停高亮是上一步留下的，不代表拟点击位置；不能依据鼠标箭头否定红十字落点。'
            '尤其区分相邻的相似文字，不能把目标名称抄成观察结果。'
            '若目标明确是页面空白区域，命中对应空白本身就是通过，不要求命中菜单或按钮；'
            '但不能把旁边真实控件当空白。'
            '返回 {"hit":true/false,"observed_label":"实际命中的标签或图标","evidence":"具体证据",'
            '"bbox":[x1,y1,x2,y2]}。bbox 必须是本局部图 0到1 坐标，包围目标控件。')
        expected, observed = self.control_role(target), self.control_role(str(detail.get('observed_label', '')))
        if detail.get('hit') is not True or not detail.get('evidence') or (expected and observed and expected != observed):
            raise ObservationRequired('局部落点核对未通过：' + str(detail))
        box = detail.get('bbox')
        if self.valid_bbox(box):
            return [(bounds[0]+box[0]*crop.width)/frame.width, (bounds[1]+box[1]*crop.height)/frame.height,
                    (bounds[0]+box[2]*crop.width)/frame.width, (bounds[1]+box[3]*crop.height)/frame.height]
        raise ObservationRequired('局部核对没有有效控件边界，不能仅按 hit 标记点击')

    @staticmethod
    def valid_bbox(box):
        return (isinstance(box, list) and len(box) == 4
                and all(type(n) in (int, float) and 0 <= n <= 1 for n in box)
                and box[0] < box[2] and box[1] < box[3])

    def pointer(self, action, params, decision_frame):
        x, y = validate_normalized_coordinates(params)
        target = str(params.get('target', '')).strip()
        if not target:
            raise ObservationRequired('鼠标操作必须提供截图中可辨认的 target 名称，不能只给坐标')
        # Generic read-only intent, not a business-button whitelist. Browser preparation stays available.
        read_only = re.search(r'只.*(?:查询|测试)|仅.*查询|query only|read.only', self.agent.task, re.I)
        writes = re.search(r'新增|添加|删除|编辑|发布|导入|导出|保存|\bdelete\b', target, re.I)
        dismissal = re.search(r'不保存|取消|关闭提示|稍后', target)
        if read_only and writes and not dismissal:
            raise ObservationRequired('只读任务不能执行业务写入操作：' + target)
        one_shot_search = (re.search(r'(?:搜索|查询)一次|只(?:搜索|查询)', self.agent.task)
                           and re.search(r'搜索按钮|查询按钮|^(?:搜索|查询)$', target)
                           and not re.search(r'Windows|系统|任务栏|开始菜单|浏览器', target, re.I))
        if one_shot_search and self._one_shot_search_sent:
            raise ObservationRequired('用户只要求搜索一次，该搜索点击已执行。请验收已有结果并继续后续步骤，不得再次搜索')
        if self.pending and not self.pending['verified'] and self.control_role(target) == 'login':
            raise ObservationRequired('登录字段尚未验收，禁止点击登录提交')
        frame = self.capture('pointer_before')
        diff = compare_screen_state(decision_frame, frame)
        if diff.geometry_changed:
            raise ObservationRequired('截图尺寸已改变，请按新截图重新定位')
        proposal = {'action': action, 'params': dict(params)}
        if self.records and self.guard.detect_loop([r[0] for r in self.records], proposal).detected:
            if not compare_screen_state(self.records[-1][1], frame).changed:
                # A macro retry may only need to repair input in its already focused field.
                field_label = {'username': '用户名', 'password': '密码'}.get((self.pending or {}).get('field'))
                if action == 'click' and field_label and field_label in target and '输入框' in target:
                    self.verify_state(f'{target} 已有明确键盘焦点（本字段内插入光标或选区）。没有焦点不能通过。')
                    return
                raise ObservationRequired('检测到短周期循环且画面无进展。请重新规划当前步骤，换一个有明确目标的操作。')
        local_checked = False
        local_bbox = None
        for correction in range(2):
            marker = frame.copy()
            draw = ImageDraw.Draw(marker)
            px, py = round(x * (frame.width - 1)), round(y * (frame.height - 1))
            draw.line((px - 9, py, px + 9, py), fill='red', width=2)
            draw.line((px, py - 9, px, py + 9), fill='red', width=2)
            result = self.inspect(marker,
                f'拟进行 {action}，目标是“{target}”。只检查实际目标和落点，不判断业务任务范围。'
                '红十字是后加的定位标记，不是按钮颜色。启动浏览器、系统搜索框与业务搜索框是不同目标。'
                '只判断红十字中心；截图中的白色鼠标箭头是上一步留下的位置，与本次落点无关。'
                '相邻选项需逐字区别，滚动必须命中指定容器。'
                '返回 {"target_visible":true/false,"inside":true/false,'
                '"bbox":[x1,y1,x2,y2],"evidence":"证据","reason":"原因"}。bbox 为全图 0到1坐标。')
            bbox = result.get('bbox')
            valid = self.valid_bbox(bbox)
            if not valid or result.get('target_visible') is not True or not result.get('evidence'):
                # A faint/small icon can be unreadable in the full frame. Require
                # positive local evidence and geometry before accepting it.
                if action != 'scroll':
                    local_bbox = self.local_pointer_check(frame, target, x, y)
                    if local_bbox:
                        bbox, local_checked = local_bbox, True
                        break
                raise ObservationRequired('鼠标目标验收失败：' + str(result.get('reason', target)))
            if (bbox[2]-bbox[0]) * (bbox[3]-bbox[1]) > .8:
                raise ObservationRequired('目标边界过大，不能把整张页面当作按钮。请选明确的空白区域或用 Escape 关闭弹层')
            if bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3] and result.get('inside') is True:
                break
            if correction:
                raise ObservationRequired(f'鼠标纠偏后仍未命中目标；最近识别的候选中心为 x={(bbox[0]+bbox[2])/2:.4f}, '
                                          f'y={(bbox[1]+bbox[3])/2:.4f}。请重新观察并验收候选，不得重复旧坐标')
            x, y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        if not local_checked and action != 'scroll' and (bbox[2]-bbox[0]) * (bbox[3]-bbox[1]) < .04:
            local_bbox = self.local_pointer_check(frame, target, x, y)
        if local_bbox:
            # A model can say hit=True but provide a box that excludes the mark.
            # Trust neither alone: recenter once, re-read, and enforce geometry.
            for local_attempt in range(2):
                if local_bbox[0] <= x <= local_bbox[2] and local_bbox[1] <= y <= local_bbox[3]:
                    bbox = local_bbox
                    break
                if local_attempt:
                    raise ObservationRequired('局部纠偏后坐标仍不在控件边界内，请重新定位')
                x, y = (local_bbox[0]+local_bbox[2])/2, (local_bbox[1]+local_bbox[3])/2
                local_bbox = self.local_pointer_check(frame, target, x, y)
        # Capture again after the verifier to avoid acting on a changed layout.
        latest = self.capture('pointer_fresh')
        region = (max(0, int(bbox[0]*frame.width)), max(0, int(bbox[1]*frame.height)),
                  min(frame.width, max(1, int(bbox[2]*frame.width))), min(frame.height, max(1, int(bbox[3]*frame.height))))
        fresh = compare_screen_state(frame, latest, target_region=region)
        if fresh.geometry_changed or fresh.target_changed:
            raise ObservationRequired('验收后目标区域发生变化，本次未点击。按新截图重新定位')
        params.update(x=x, y=y)
        px, py = self.agent._to_pixels(params, latest)
        if action == 'scroll':
            self.vnc.scroll(px, py, (1 if params.get('direction') == 'up' else -1)
                            * max(1, min(5, int(params.get('amount', 3)))))
        elif action == 'double_click':
            self.vnc.double_click(px, py)
        else:
            self.vnc.click(px, py, button=3 if action == 'right_click' else 1)
        if one_shot_search:
            self._one_shot_search_sent = True
        self.records.append(({'action': action, 'params': dict(params)}, latest.copy()))
        self.records = self.records[-12:]
        if self.pending and self.pending['verified']:
            self.pending = None
        time.sleep(.2)
        self.capture('pointer_after')

    def open_url(self, url):
        # Do not type commands into whatever window happened to have focus.
        # The URL input path verifies browser geometry before focusing the bar.
        self.input(url, 'url', replace=True)

    def open_app(self, application):
        if not application:
            raise ObservationRequired('缺少应用名称')
        self.vnc.press_key({'windows': 'win+s', 'mac': 'cmd+space'}.get(self.agent.system, 'super'))
        time.sleep(.8)
        self.input(application, 'app_search', replace=True)
        self.press('enter')
        time.sleep(1)
        if self.is_browser_application(application):
            self.ensure_browser_maximized()
