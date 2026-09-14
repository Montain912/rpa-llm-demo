# rpa-llm-demo

## 本地启动与端口

在本项目目录运行 `python -B main.py`，打开 [本地前端](http://127.0.0.1:5011/)。
远程桌面功能使用 FastAPI 的 `main.py`，不要同时启动旧 Flask 入口 `app.py`。

| 用途 | 默认地址或端口 | 环境变量 |
| --- | --- | --- |
| 前端和 API | 5011 | `RPA_HTTP_PORT` |
| noVNC WebSocket 代理 | 6082 | `RPA_WS_PORT` |
| 测试桌面的 VNC 地址 | 127.0.0.1 | `RPA_VNC_HOST` |
| 测试桌面的 VNC 端口 | 5901 | `RPA_VNC_PORT` |

前端连接时自动从后端读取代理端口，无需修改 HTML。设置窗口中填的是
VNC 服务端的地址、端口和密码，不是前端端口，也不是业务网站的地址。
密码留空表示保持服务中现有密码；设置窗口保存的配置仅在当前进程内有效。

需要调整端口时，可在 PowerShell 中设置后启动：

```powershell
$env:RPA_HTTP_PORT = "5011"
$env:RPA_WS_PORT = "6082"
$env:RPA_VNC_HOST = "127.0.0.1"
$env:RPA_VNC_PORT = "5901"
python -B main.py
```

如果改用 `uvicorn main:app` 启动，请明确传入 `--port 5011`（或所选 HTTP
端口）；Uvicorn 命令不会自动采用 `RPA_HTTP_PORT`。
如果后端运行在 Docker 内，连接宿主机的 VNC 时将 `RPA_VNC_HOST` 改为
`host.docker.internal`，并映射所选 HTTP 和 WebSocket 代理端口。
端口被占用时请换用空闲端口，不要停止不属于本项目的服务。

## 回归测试（无需模型或真实桌面操作）

```powershell
python -m pip install pytest
python -B -m pytest -q -p no:cacheprovider tests
node tests/frontend_connection.test.cjs
```

运行截图、模型输出和消耗记录分别保存到本项目的 `screenshots/`、
`llm_output/` 和 `summary/`，缺少目录时由程序自动创建，不依赖 Git 保存空目录。
FastAPI 会将 VNC 配置的 `system` 传入每个任务；当前测试桌面使用 `win`，
不能因为运行在 Linux 服务器上就把远程 Windows 桌面当成 Linux 操作。

## 合并后的执行链

文字输入统一经过 `verified_interaction.py` → `input_method.py` → VNC 键盘通道。
网址技能只在浏览器地址栏输入并验收，不再向当前窗口发送 Firefox/终端命令，
也不自动回车。登录字段先确认焦点再替换，防止 Ctrl+A 选中整页。
浏览器经启动技能或启动图标打开后会自动最大化；网址输入前也会检查窗口状态。
先确认浏览器前台和窗口边界，未最大化才发送快捷键，验收后再继续；已最大化时不重复发送。

鼠标操作要求目标名称，并在执行前核对落点、必要的局部放大和画面变化。
红十字表示下一次点击的候选位置，截图原有鼠标箭头不代表本次落点。
失败动作会记录原因和截图，交给模型重新观察、规划；不会显示为已执行。
完成验收结合历史执行截图与当前状态，避免把退出后的登录页误认为未执行过查询。
