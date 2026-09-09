"""临时诊断：查看当前 VNC Server 画面内容"""
import sys
sys.path.insert(0, r"e:\HyProject\rpa-llm-demo")
from vnc_client import VNCClient

v = VNCClient(host="172.20.195.63", port=5900, password="219912")
v.connect()
img = v.screenshot()
print(f"尺寸: {img.size}")
extrema = img.convert("RGB").getextrema()
print(f"RGB极值: {extrema}")
all_zero = all(max_v == 0 for _, max_v in extrema)
print(f"纯黑帧: {all_zero}")
img.save(r"e:\HyProject\rpa-llm-demo\vnc_debug.png")
print("已保存 vnc_debug.png")
v.disconnect()
