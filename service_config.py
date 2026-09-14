"""Local service ports, separate from the remote desktop's VNC port."""
import os


def env_port(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} 必须是 1–65535 之间的整数") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} 必须是 1–65535 之间的整数")
    return value


HTTP_PORT = env_port("RPA_HTTP_PORT", 5011)
WS_PORT = env_port("RPA_WS_PORT", 6082)
VNC_HOST = os.environ.get("RPA_VNC_HOST", "127.0.0.1")
VNC_PORT = env_port("RPA_VNC_PORT", 5901)

if HTTP_PORT == WS_PORT:
    raise ValueError("RPA_HTTP_PORT 与 RPA_WS_PORT 不能使用同一个端口")
