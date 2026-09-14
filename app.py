"""
Flask 后端 - RPA+LLM Demo
提供 REST API 供前端调用
"""
import os
import io
import json
import base64
import threading
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, send_file
from flask_cors import CORS
from PIL import Image

from rpa_agent import RPAgent
from vnc_client import VNCClient

app = Flask(__name__)
CORS(app)

# 全局状态
task_state = {
    "running": False,
    "paused": False,
    "stop_requested": False,
    "current_step": 0,
    "history": [],
    "result": "",
    "task": ""
}

# 全局 VNC 配置
VNC_CONFIG = {
    "host": "172.20.195.63",
    "port": 5900,
    "user": "do fish",
    "password": "219912",
    "system": "win",
}

state_lock = threading.Lock()

# 截图专用长连接缓存：避免每次请求重建 VNC 连接命中首帧黑屏
_screenshot_vnc = None
_screenshot_lock = threading.Lock()


def _get_screenshot_vnc() -> VNCClient:
    """获取截图专用 VNC 客户端（复用长连接），连接失效时自动重建"""
    global _screenshot_vnc
    if _screenshot_vnc is None:
        vnc = VNCClient(
            host=VNC_CONFIG["host"],
            port=VNC_CONFIG["port"],
            user=VNC_CONFIG["user"],
            password=VNC_CONFIG["password"],
            system=VNC_CONFIG["system"],
        )
        vnc.connect()
        _screenshot_vnc = vnc
    return _screenshot_vnc


def _reset_screenshot_vnc():
    """重置截图连接（VNC 配置变更或连接异常时调用）"""
    global _screenshot_vnc
    if _screenshot_vnc is not None:
        try:
            _screenshot_vnc.disconnect()
        except Exception:
            pass
        _screenshot_vnc = None

# 操作日志目录
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
_log_lock = threading.Lock()


def _write_log(record: dict):
    """向当前任务的 JSONL 日志追加一行记录"""
    log_file = task_state.get("log_file")
    if not log_file:
        return
    record["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _log_lock:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_agent_task(task: str):
    """在后台线程中运行 RPA Agent"""
    # 创建本次任务的日志文件
    log_file = os.path.join(LOG_DIR, f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
    with state_lock:
        task_state["log_file"] = log_file

    _write_log({"type": "task_start", "task": task})

    agent = RPAgent(
        vnc_host=VNC_CONFIG["host"],
        vnc_port=VNC_CONFIG["port"],
        vnc_password=VNC_CONFIG["password"],
        vnc_user=VNC_CONFIG["user"],
        system=VNC_CONFIG["system"],
    )

    def progress_callback(step_info):
        with state_lock:
            task_state["current_step"] = step_info["step"]
            task_state["history"].append(step_info)
        _write_log({"type": "step", **step_info})

    def control_checker():
        with state_lock:
            if task_state["stop_requested"]:
                return "stop"
            if task_state["paused"]:
                return "pause"
            return None

    try:
        result = agent.run_task(task, progress_callback=progress_callback, control_checker=control_checker)
        with state_lock:
            task_state["result"] = result
        _write_log({"type": "result", "result": result})
    except Exception as e:
        with state_lock:
            task_state["result"] = f"执行出错: {str(e)}"
        _write_log({"type": "error", "error": str(e)})
    finally:
        with state_lock:
            task_state["running"] = False


@app.route("/")
def index():
    """返回前端页面"""
    return render_template_string(open("templates/index.html", "r", encoding="utf-8").read())


@app.route("/api/screenshot", methods=["GET"])
def get_screenshot():
    """获取当前 VNC 截图（base64），复用长连接避免首帧黑屏"""
    try:
        with _screenshot_lock:
            try:
                vnc = _get_screenshot_vnc()
                img = vnc.screenshot()
            except Exception:
                # 连接失效则重建一次重试
                _reset_screenshot_vnc()
                vnc = _get_screenshot_vnc()
                img = vnc.screenshot()
        img.save("screenshot.png")

        # 压缩图片
        img.thumbnail((1440, 900))
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=80)
        img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

        return jsonify({
            "success": True,
            "screenshot": f"data:image/jpeg;base64,{img_base64}"
        })
    except Exception as e:
        _reset_screenshot_vnc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/task", methods=["POST"])
def start_task():
    """启动一个新的 RPA 任务"""
    global task_state

    data = request.json
    task = data.get("task", "").strip()

    if not task:
        return jsonify({"success": False, "error": "任务内容不能为空"}), 400

    with state_lock:
        if task_state["running"]:
            return jsonify({"success": False, "error": "已有任务正在执行"}), 400

        # 重置状态
        task_state = {
            "running": True,
            "paused": False,
            "stop_requested": False,
            "current_step": 0,
            "history": [],
            "result": "",
            "task": task,
            "log_file": ""
        }

    # 启动后台线程
    thread = threading.Thread(target=run_agent_task, args=(task,), daemon=True)
    thread.start()

    return jsonify({"success": True, "message": "任务已启动"})


@app.route("/api/status", methods=["GET"])
def get_status():
    """获取任务状态"""
    with state_lock:
        return jsonify({
            "running": task_state["running"],
            "paused": task_state["paused"],
            "current_step": task_state["current_step"],
            "history": task_state["history"],
            "result": task_state["result"],
            "task": task_state["task"]
        })


@app.route("/api/pause", methods=["POST"])
def pause_task():
    """暂停或恢复当前任务"""
    data = request.json or {}
    pause = bool(data.get("pause", True))

    with state_lock:
        if not task_state["running"]:
            return jsonify({"success": False, "error": "没有正在执行的任务"}), 400
        task_state["paused"] = pause

    _write_log({"type": "pause" if pause else "resume"})
    return jsonify({"success": True, "message": "任务已暂停" if pause else "任务已恢复"})


@app.route("/api/stop", methods=["POST"])
def stop_task():
    """终止当前任务（当前步骤完成后停止）"""
    with state_lock:
        if not task_state["running"]:
            return jsonify({"success": False, "error": "没有正在执行的任务"}), 400
        task_state["stop_requested"] = True

    _write_log({"type": "stop"})
    return jsonify({"success": True, "message": "终止信号已发送，当前步骤完成后停止"})


@app.route("/api/vnc-config", methods=["GET", "POST"])
def vnc_config():
    """获取或设置 VNC 配置"""
    global VNC_CONFIG

    if request.method == "GET":
        return jsonify({
            "host": VNC_CONFIG["host"],
            "port": VNC_CONFIG["port"],
            "password": "***"  # 不返回密码明文
        })

    elif request.method == "POST":
        data = request.json
        VNC_CONFIG["host"] = data.get("host", VNC_CONFIG["host"])
        VNC_CONFIG["port"] = int(data.get("port", VNC_CONFIG["port"]))
        VNC_CONFIG["password"] = data.get("password", VNC_CONFIG["password"])
        # 配置变更后旧截图连接失效，重置缓存
        _reset_screenshot_vnc()
        return jsonify({"success": True, "message": "VNC 配置已更新"})


@app.route("/api/test-connection", methods=["POST"])
def test_connection():
    """测试 VNC 连接"""
    try:
        data = request.json or {}
        host = data.get("host", VNC_CONFIG["host"])
        port = int(data.get("port", VNC_CONFIG["port"]))
        password = data.get("password", VNC_CONFIG["password"])

        vnc = VNCClient(host=host, port=port, password=password)
        vnc.connect()
        img = vnc.screenshot()
        vnc.disconnect()

        return jsonify({
            "success": True,
            "message": "连接成功",
            "resolution": f"{img.width}x{img.height}"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    # 确保 templates 目录存在
    os.makedirs("templates", exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("RPA_HTTP_PORT", "5010")), debug=True)
