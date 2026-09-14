"""
LLM 客户端封装 - 支持 DeepSeek V4 Flash（文本）和 DeepSeek V4 Flash VL（视觉）
DeepSeek API 兼容 OpenAI 格式
"""
import base64
import io
import json
import os
import time
from openai import OpenAI
from PIL import Image
import random


class TokenTracker:
    """Token 消耗追踪器：记录每次 LLM 调用的 token 用量，支持保存为 JSON"""

    def __init__(self):
        self.records = []
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0

    def record(self, call_type: str, model: str, usage, extra: dict = None):
        """记录一次 API 调用的 token 消耗
        call_type: 调用类型（text/vision/planning）
        model: 模型名
        usage: OpenAI response.usage 对象
        extra: 额外元数据（如 step、thought 摘要）
        """
        prompt_t = getattr(usage, "prompt_tokens", 0) if usage else 0
        completion_t = getattr(usage, "completion_tokens", 0) if usage else 0
        total_t = getattr(usage, "total_tokens", 0) if usage else 0

        entry = {
            "index": len(self.records) + 1,
            "call_type": call_type,
            "model": model,
            "prompt_tokens": prompt_t,
            "completion_tokens": completion_t,
            "total_tokens": total_t,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if extra:
            entry.update(extra)

        self.records.append(entry)
        self.total_prompt_tokens += prompt_t
        self.total_completion_tokens += completion_t
        self.total_tokens += total_t

    def reset(self):
        """重置追踪器（每次任务开始前调用）"""
        self.records = []
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0

    def summary(self) -> dict:
        """返回汇总字典"""
        return {
            "total_calls": len(self.records),
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.records,
        }

    def save(self, filepath: str):
        """保存到 JSON 文件"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        data = self.summary()
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[Token] 已保存 token 消耗记录到 {filepath}")
        print(f"[Token] 共 {data['total_calls']} 次调用，"
              f"总消耗: prompt={data['total_prompt_tokens']}, "
              f"completion={data['total_completion_tokens']}, "
              f"total={data['total_tokens']}")


# 模块级单例，chat_text/chat_vision 自动记录，rpa_agent 在任务结束时保存
token_tracker = TokenTracker()
# DeepSeek API 配置
API_KEY = "sk-bb4bc18ea30e4ae390b49fd2e6c15d2f"
BASE_URL = "https://api.deepseek.com/v1"
BASE_URL_DICT = {
    "deepseekv4": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-flash","api_key":API_KEY},
    "qwen3.8": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3.8-flash","api_key":"sk-ws-H.PDLLPLE.gJLD.MEQCIHoaL9eMOcvp-7ARkEn5lemmtBh0FYq1fyfolFjuyAJQAiALFAS5iyjGP5d1XPn_sREbGFyIergae8LE4Al3WbdRfQ"}
}


# 模型名称
TEXT_MODEL = "deepseek-v4-flash"      # DeepSeek V4 Flash 文本模型
VISION_MODEL = "deepseek-v4-flash-vision-exp"    # DeepSeek V4 Flash VL 视觉模型（同一接口，支持图片输入）

client = OpenAI(api_key=BASE_URL_DICT["deepseekv4"]["api_key"], base_url=BASE_URL_DICT["deepseekv4"]["base_url"])
# client = OpenAI(api_key=BASE_URL_DICT["qwen3.8"]["api_key"], base_url=BASE_URL_DICT["qwen3.8"]["base_url"])


def image_to_base64(image: Image.Image, format: str = "PNG") -> str:
    """将 PIL Image 转换为 base64 字符串"""
    buffered = io.BytesIO()
    image.save(buffered, format=format)
    img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return img_base64


# 视觉模型建议上限：最长边 1920（兼顾细节和 token 成本）
VISION_MAX_LONG_SIDE = 1920
# 短边低于此值不缩放（1080p/720p 原图够用）
VISION_MIN_SHORT_SIDE = 1080


def shrink_for_llm(image: Image.Image) -> Image.Image:
    """短边自适应缩放：短边 < 1080 不缩；短边 ≥ 1080 时最长边限制在 1920 以内"""
    w, h = image.size
    short_side = min(w, h)
    long_side = max(w, h)
    if short_side < VISION_MIN_SHORT_SIDE or long_side <= VISION_MAX_LONG_SIDE:
        return image
    ratio = VISION_MAX_LONG_SIDE / long_side
    new_w = int(w * ratio)
    new_h = int(h * ratio)
    return image.resize((new_w, new_h), Image.Resampling.LANCZOS)


def chat_text(prompt: str, system_prompt: str = "你是一个有帮助的助手。", temperature: float = 0.7) -> str:
    """
    调用文本模型
    """
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        temperature=temperature,
        max_tokens=4096
    )
    token_tracker.record("text", TEXT_MODEL, response.usage)
    return response.choices[0].message.content


def chat_vision(prompt: str, image: Image.Image, system_prompt: str = "你是一个视觉助手，能够分析截图并描述界面元素。", temperature: float = 0.5, max_tokens: int = 1024) -> str:
    """
    调用视觉模型，传入截图进行分析
    默认低温度 + 小 max_tokens：决策类输出只需一个小 JSON，缩短生成时间
    """
    r = random.randint(0, 1000000)
    img = shrink_for_llm(image)
    img_base64 = image_to_base64(img, format="JPEG")
    image.save(f"./llm_output/screenshot_{r}.png")
    
    
    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_base64}",
                            "detail": "auto"
                        }
                    }
                ]
            }
        ],
        extra_body={"thinking": {"type": "disabled"}},
        temperature=temperature,
        max_tokens=max_tokens
    )
    token_tracker.record("vision", VISION_MODEL, response.usage)
    # print(response.choices[0].message)
    with open(f"./llm_output/vision_response_{r}.txt", "w") as f:
        f.write(response.choices[0].message.content)
    # print("----------",response.choices[0].message.content)
    return response.choices[0].message.content


if __name__ == "__main__":
    # 测试文本模型
    res = {"type": "step", "step": 1, "action": "click", "params": {"x": 0.527, "y": 0.975}, "thought": "当前是桌面环境，需要打开浏览器访问目标网址。点击任务栏中的Edge浏览器图标。", "duration": {"observe": 0.98, "llm": 1.7, "execute": 0.22, "total": 2.91}, "time": "2026-09-09 09:35:06"}

    result = chat_vision("当前截图和你返回的浏览器坐标是否一致,为什么根据你的坐标老是点击到旁边的图标，是否和屏幕分辨率相关，当前屏幕分表率为2560*1600", Image.open("E:/HyProject/rpa-llm-demo/screenshots/sh_232527_1.png"))
    print("视觉模型输出:", result)
