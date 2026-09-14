"""
FastAPI 后端 - RPA+LLM Demo
提供 REST API 与 WebSocket 推送（替代 Flask + 前端轮询）
启动：python -B main.py（默认前端 5011，桌面代理 6082）
"""
import os
import io
import sys
import json
import base64
import subprocess
import asyncio
import threading
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from service_config import HTTP_PORT, WS_PORT, VNC_HOST, VNC_PORT
from runtime_paths import PROJECT_ROOT, runtime_path

from rpa_agent import RPAgent
from vnc_client import VNCClient
from knowledge_loader import warmup_cache

# 启动时预加载所有知识摘要到内存缓存，避免首次任务延迟
warmup_cache()

app = FastAPI(title="GUI AGENT Demo")

# 静态资源目录（noVNC 等）；目录不存在时创建避免 StaticFiles 抛错
(PROJECT_ROOT / "static").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static"), name="static")

# 全局状态
task_state = {
    "running": False,
    "paused": False,
    "stop_requested": False,
    "current_step": 0,
    "history": [],
    "result": "",
    "task": "",
    "log_file": ""
}

# 全局 VNC 配置
VNC_CONFIG = {
    "host": VNC_HOST,
    "port": VNC_PORT,
    "user": "",
    "password": "123456",
    "system": "win"
}

# HTTP 与 noVNC 代理端口统一由 service_config 配置，VNC 目标端口独立。

state_lock = threading.Lock()

# 截图专用长连接缓存：避免每次请求重建 VNC 连接命中首帧黑屏
_screenshot_vnc: Optional[VNCClient] = None
_screenshot_lock = threading.Lock()


def _get_screenshot_vnc() -> VNCClient:
    """获取截图专用 VNC 客户端（复用长连接），连接失效时自动重建"""
    global _screenshot_vnc
    if _screenshot_vnc is None:
        vnc = VNCClient(
            host=VNC_CONFIG["host"],
            port=VNC_CONFIG["port"],
            user=VNC_CONFIG["user"],
            password=VNC_CONFIG["password"]
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


# ─── websockify 代理（noVNC 前端经 WebSocket 直连 VNC Server）─────────────

class WebsockifyRunner:
    """以子进程方式管理 websockify（VNC TCP → WebSocket 代理）

    用子进程隔离 websockify 的信号处理，避免与主进程冲突；
    配置变更时通过 stop/start 重建。
    """
    def __init__(self, listen_port: int, target_host: str, target_port: int):
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.process = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return  # 已在运行
            # python -m websockify [source_port] target_host:target_port
            cmd = [
                sys.executable, "-m", "websockify",
                str(self.listen_port),
                f"{self.target_host}:{self.target_port}",
            ]
            # stdout/stderr 合并输出，便于排查启动失败
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            # 短暂等待启动；端口占用或参数错误会快速失败
            try:
                self.process.wait(timeout=0.5)
                # 0.5s 内退出说明启动失败，读取输出
                out = self.process.stdout.read() if self.process.stdout else ""
                self.process = None
                raise RuntimeError(f"websockify 启动失败: {out[:500]}")
            except subprocess.TimeoutExpired:
                # 仍在运行 = 启动成功
                pass

    def stop(self):
        with self._lock:
            if self.process is None:
                return
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.process = None

    def restart(self):
        self.stop()
        self.start()

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None


# 全局 websockify 实例（VNC 配置变更后调用 restart）
_websockify: Optional[WebsockifyRunner] = None
_websockify_lock = threading.Lock()


def _get_websockify() -> WebsockifyRunner:
    """获取全局 websockify 实例（懒启动，按当前 VNC_CONFIG 配置）"""
    global _websockify
    with _websockify_lock:
        if _websockify is None:
            _websockify = WebsockifyRunner(
                listen_port=WS_PORT,
                target_host=VNC_CONFIG["host"],
                target_port=VNC_CONFIG["port"],
            )
        # 启动失败或子进程退出后，下次连接可重试，不复用已失效的代理。
        _websockify.start()
        return _websockify


def _restart_websockify():
    """VNC 配置变更后按新目标重启 websockify"""
    global _websockify
    with _websockify_lock:
        if _websockify is not None:
            _websockify.target_host = VNC_CONFIG["host"]
            _websockify.target_port = VNC_CONFIG["port"]
            _websockify.restart()


# 操作日志目录
LOG_DIR = str(PROJECT_ROOT / "logs")
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
    def progress_callback(step_info):
        with state_lock:
            task_state["current_step"] = step_info["step"]
            task_state["history"].append(step_info)
        _write_log({"type": "step", **step_info})
        _notify_ws({"type": "step", **step_info})

    def control_checker():
        with state_lock:
            if task_state["stop_requested"]:
                return "stop"
            if task_state["paused"]:
                return "pause"
            return None

    try:
        # 初始化错误也必须回收 running 状态，避免前端一直显示“正在执行”。
        log_file = str(runtime_path("logs", f"task_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jsonl"))
        with state_lock:
            task_state["log_file"] = log_file
        _write_log({"type": "task_start", "task": task})
        agent = RPAgent(
            vnc_host=VNC_CONFIG["host"],
            vnc_port=VNC_CONFIG["port"],
            vnc_password=VNC_CONFIG["password"],
            system=VNC_CONFIG.get("system", "win"),
        )
        result = agent.run_task(task, progress_callback=progress_callback, control_checker=control_checker)
        with state_lock:
            task_state["result"] = result
        _write_log({"type": "result", "result": result})
    except Exception as e:
        with state_lock:
            task_state["result"] = f"执行出错: {str(e)}"
        try:
            _write_log({"type": "error", "error": str(e)})
        except OSError:
            pass  # 日志目录不可写时，仍通过状态接口和 WebSocket 返回原始错误。
    finally:
        with state_lock:
            task_state["running"] = False
        _notify_ws({"type": "result", "result": task_state["result"], "running": False})


# ─── WebSocket 推送 ───────────────────────────────────────────────────

_ws_clients: set = set()
_ws_loop: Optional[asyncio.AbstractEventLoop] = None


def _notify_ws(payload: dict):
    """从任意线程向所有 WS 客户端推送消息（借助主事件循环）"""
    if not _ws_clients or _ws_loop is None:
        return
    asyncio.run_coroutine_threadsafe(_broadcast(payload), _ws_loop)


async def _broadcast(payload: dict):
    """向所有已连接的 WS 客户端异步广播"""
    dead = []
    data = json.dumps(payload, ensure_ascii=False)
    for ws in list(_ws_clients):
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


@app.websocket("/ws/status")
async def ws_status(ws: WebSocket):
    """状态推送通道：步骤、结果实时推送到前端"""
    global _ws_loop
    await ws.accept()
    _ws_clients.add(ws)
    if _ws_loop is None:
        _ws_loop = asyncio.get_event_loop()
    try:
        # 连接建立即推送当前快照
        with state_lock:
            snapshot = {
                "type": "snapshot",
                "running": task_state["running"],
                "paused": task_state["paused"],
                "current_step": task_state["current_step"],
                "history": task_state["history"],
                "result": task_state["result"],
                "task": task_state["task"]
            }
        await ws.send_text(json.dumps(snapshot, ensure_ascii=False))

        # 保活接收：前端发来的 ping 或断开信号
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


# ─── REST API ─────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    task: str


class PauseRequest(BaseModel):
    pause: bool = True


@app.get("/", response_class=HTMLResponse)
def index():
    """返回前端页面"""
    return (PROJECT_ROOT / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/api/screenshot")
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
        img.save(PROJECT_ROOT / "screenshot.png")

        # 压缩图片
        img.thumbnail((1440, 900))
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=80)
        img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

        return {"success": True, "screenshot": f"data:image/jpeg;base64,{img_base64}"}
    except Exception as e:
        _reset_screenshot_vnc()
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/task")
def start_task(req: TaskRequest):
    """启动一个新的 RPA 任务"""
    task = req.task.strip()
    if not task:
        return JSONResponse({"success": False, "error": "任务内容不能为空"}, status_code=400)

    with state_lock:
        if task_state["running"]:
            return JSONResponse({"success": False, "error": "已有任务正在执行"}, status_code=400)

        # 重置状态
        task_state.update({
            "running": True,
            "paused": False,
            "stop_requested": False,
            "current_step": 0,
            "history": [],
            "result": "",
            "task": task,
            "log_file": ""
        })

    _notify_ws({"type": "task_started", "task": task, "running": True})

    # 启动后台线程
    thread = threading.Thread(target=run_agent_task, args=(task,), daemon=True)
    try:
        thread.start()
    except Exception as exc:
        with state_lock:
            task_state["running"] = False
            task_state["result"] = f"任务线程启动失败: {exc}"
        _notify_ws({"type": "result", "result": task_state["result"], "running": False})
        return JSONResponse({"success": False, "error": task_state["result"]}, status_code=500)

    return {"success": True, "message": "任务已启动"}


@app.get("/api/status")
def get_status():
    """获取任务状态（保留 REST 兼容）"""
    with state_lock:
        return {
            "running": task_state["running"],
            "paused": task_state["paused"],
            "current_step": task_state["current_step"],
            "history": task_state["history"],
            "result": task_state["result"],
            "task": task_state["task"]
        }


@app.post("/api/pause")
def pause_task(req: PauseRequest):
    """暂停或恢复当前任务"""
    with state_lock:
        if not task_state["running"]:
            return JSONResponse({"success": False, "error": "没有正在执行的任务"}, status_code=400)
        task_state["paused"] = req.pause

    _write_log({"type": "pause" if req.pause else "resume"})
    _notify_ws({"type": "paused", "paused": req.pause})
    return {"success": True, "message": "任务已暂停" if req.pause else "任务已恢复"}


@app.post("/api/stop")
def stop_task():
    """终止当前任务（当前步骤完成后停止）"""
    with state_lock:
        if not task_state["running"]:
            return JSONResponse({"success": False, "error": "没有正在执行的任务"}, status_code=400)
        task_state["stop_requested"] = True

    _write_log({"type": "stop"})
    return {"success": True, "message": "终止信号已发送，当前步骤完成后停止"}


class VNCConfigRequest(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    password: Optional[str] = None


@app.get("/api/vnc-config")
def get_vnc_config():
    """获取 VNC 配置"""
    return {
        "host": VNC_CONFIG["host"],
        "port": VNC_CONFIG["port"],
        "password": "***"  # 不返回密码明文
    }


@app.post("/api/vnc-config")
def set_vnc_config(req: VNCConfigRequest):
    """设置 VNC 配置"""
    if req.host is not None:
        VNC_CONFIG["host"] = req.host
    if req.port is not None:
        VNC_CONFIG["port"] = req.port
    if req.password is not None:
        VNC_CONFIG["password"] = req.password
    # 配置变更后旧截图连接失效，重置缓存
    _reset_screenshot_vnc()
    # 同步重启 websockify 代理到新目标
    _restart_websockify()
    return {"success": True, "message": "VNC 配置已更新"}


class ConnectionTestRequest(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    password: Optional[str] = None


@app.post("/api/test-connection")
def test_connection(req: ConnectionTestRequest):
    """测试 VNC 连接"""
    vnc = None
    try:
        host = req.host or VNC_CONFIG["host"]
        port = req.port if req.port is not None else VNC_CONFIG["port"]
        password = req.password if req.password is not None else VNC_CONFIG["password"]

        vnc = VNCClient(host=host, port=port, password=password)
        vnc.connect()
        img = vnc.screenshot()

        return {
            "success": True,
            "message": "连接成功",
            "resolution": f"{img.width}x{img.height}"
        }
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    finally:
        if vnc is not None:
            try:
                vnc.disconnect()
            except Exception:
                pass


@app.get("/api/novnc-info")
def novnc_info():
    """返回 websockify 代理端口与目标信息，供前端拼 WebSocket URL"""
    try:
        runner = _get_websockify()
    except Exception as exc:
        return JSONResponse(
            {"running": False, "error": f"桌面代理端口 {WS_PORT} 启动失败: {exc}"},
            status_code=503,
        )
    return {
        "ws_port": WS_PORT,
        "target_host": VNC_CONFIG["host"],
        "target_port": VNC_CONFIG["port"],
        "vnc_user": VNC_CONFIG.get("user", ""),
        "vnc_password": VNC_CONFIG.get("password", ""),
        "running": runner.is_running(),
    }


if __name__ == "__main__":
    import uvicorn
    # 桌面代理在连接时启动；即使代理不可用，仍可打开前端修改 VNC 设置。
    try:
        uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT)
    finally:
        # 主进程退出时关闭 websockify 子进程
        if _websockify is not None:
            _websockify.stop()
