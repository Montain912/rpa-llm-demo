"""
VNC 客户端封装 - 用于截图、鼠标点击、键盘输入等操作
基于 vncdotool
"""
import io
import time
from PIL import Image
from vncdotool import api


class VNCClient:
    """VNC 客户端，封装截图和键鼠操作"""

    def __init__(self, host: str = "localhost", port: int = 5901, password: str = "123456", user: str = ""):
        self.host = host
        self.port = port
        self.password = password
        self.user = user
        self._client = None

    def connect(self):
        """连接 VNC 服务器"""
        if self.user:
            self._client = api.connect(f"{self.host}::{self.port}", password=self.password, username=self.user)
        else:
            self._client = api.connect(f"{self.host}::{self.port}", password=self.password)
        self._client.timeout = 10

    def disconnect(self):
        """断开连接"""
        if self._client:
            self._client.disconnect()
            self._client = None

    def screenshot(self) -> Image.Image:
        """截取当前屏幕，返回 PIL Image"""
        if not self._client:
            self.connect()
        # 首帧竞态防护：新连接后首帧可能尚未到达（纯黑），检测到则重试刷新
        img = None
        for _ in range(5):
            self._client.refreshScreen()
            img = self._client.screen.copy()
            # 纯黑判定：RGB 三通道 extrema 最大值均为 0
            if any(max_v > 0 for _, max_v in img.convert("RGB").getextrema()):
                return img
            time.sleep(0.3)
        return img

    def click(self, x: int, y: int, button: int = 1):
        """
        鼠标点击
        button: 1=左键, 2=中键, 3=右键
        """
        if not self._client:
            self.connect()
        self._client.mouseMove(x, y)
        time.sleep(0.05)
        self._client.mouseDown(button)
        time.sleep(0.05)
        self._client.mouseUp(button)
        time.sleep(0.1)

    def double_click(self, x: int, y: int):
        """鼠标双击"""
        self.click(x, y)
        time.sleep(0.1)
        self.click(x, y)

    def type_text(self, text: str):
        """输入文本"""
        if not self._client:
            self.connect()
        for char in text:
            self._client.keyPress(char)
            time.sleep(0.02)

    # 键名别名映射：统一归一到 vncdotool KEYMAP 中的小写键名
    # 多字符未知键名会触发 ord() 报错，必须在此显式映射或拦截
    KEY_MAP = {
        'enter': 'enter',
        'return': 'enter',
        'escape': 'esc',
        'esc': 'esc',
        'tab': 'tab',
        'backspace': 'bsp',
        'bsp': 'bsp',
        'delete': 'delete',
        'del': 'delete',
        'space': 'space',
        'left': 'left',
        'right': 'right',
        'up': 'up',
        'down': 'down',
        'ctrl': 'ctrl',
        'control': 'ctrl',
        'alt': 'alt',
        'shift': 'shift',
        'super': 'super',
        'win': 'super',
        'meta': 'super',
    }

    def _resolve_key(self, key: str) -> str:
        """将用户键名归一到 vncdotool KEYMAP 键名；未知多字符键名抛错避免 ord() 报错"""
        k = key.lower().strip()
        mapped = self.KEY_MAP.get(k)
        if mapped:
            return mapped
        # 单字符（含字母/数字/符号）直接交给 vncdotool 处理
        if len(k) == 1:
            return k
        raise ValueError(f"未知按键名: '{key}'（需为单字符或 KEY_MAP 中已定义的键名）")

    def press_key(self, key: str):
        """
        按下并释放按键，支持组合键
        - 单键: 'enter', 'escape', 'tab', 'backspace', 'space', 'up' 等
        - 组合键: 'ctrl+n', 'alt+f4', 'shift+tab'（用 '+' 分隔）
        """
        if not self._client:
            self.connect()
        # 含 '+' 视为组合键，交给 key_combination 处理
        if '+' in key:
            parts = [p.strip() for p in key.split('+') if p.strip()]
            if len(parts) < 2:
                raise ValueError(f"组合键格式非法: '{key}'")
            self.key_combination(*parts)
            return
        vnc_key = self._resolve_key(key)
        self._client.keyPress(vnc_key)

    def key_combination(self, *keys):
        """
        组合键，如 key_combination('ctrl', 'c')
        每个 key 都走 _resolve_key 别名归一，避免 ctrl+return 这类别名报错
        """
        if not self._client:
            self.connect()
        resolved = [self._resolve_key(k) for k in keys]
        # 按下所有修饰键
        for k in resolved[:-1]:
            self._client.keyDown(k)
            time.sleep(0.02)
        # 按下并释放最后一个键
        self._client.keyPress(resolved[-1])
        time.sleep(0.02)
        # 释放所有修饰键（逆序）
        for k in reversed(resolved[:-1]):
            self._client.keyUp(k)
            time.sleep(0.02)

    def maximize_window(self, system: str = "win"):
        """最大化当前活动窗口。窗口已最大化时无副作用，适合打开应用后确定性调用

        system: "win" 用 Win+Up；"linux"/"mac" 用 Super+Up（GNOME 默认最大化快捷键）
        """
        if not self._client:
            self.connect()
        if (system or "win").strip().lower() in ("linux", "mac"):
            self.key_combination('super', 'up')
        else:
            self.key_combination('win', 'up')
        time.sleep(0.8)  # 等最大化动画完成

    def move_mouse(self, x: int, y: int):
        """移动鼠标到指定位置"""
        if not self._client:
            self.connect()
        self._client.mouseMove(x, y)

    def scroll(self, x: int, y: int, direction: int = 1):
        """
        鼠标滚轮
        direction: 1=向上, -1=向下
        """
        if not self._client:
            self.connect()
        self._client.mouseMove(x, y)
        button = 4 if direction > 0 else 5
        self._client.mouseDown(button)
        time.sleep(0.05)
        self._client.mouseUp(button)


if __name__ == "__main__":
    # 测试 VNC 连接
    vnc = VNCClient(host="localhost", port=5901, password="123456")
    try:
        vnc.connect()
        print("VNC 连接成功！")
        img = vnc.screenshot()
        print(f"截图尺寸: {img.size}")
        img.save("vnc_test.png")
        print("截图已保存为 vnc_test.png")
    except Exception as e:
        print(f"VNC 连接失败: {e}")
    finally:
        vnc.disconnect()
