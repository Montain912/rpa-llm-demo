# rpa-llm-demo
rpa-llm-demo

## 文本与中文输入

- ASCII 文本通过 VNC 逐键输入，不使用剪贴板。
- URL、邮箱和数字字段会把全角字符规范为半角并移除空白；密码、用户名、命令、代码和普通文本保持原值。
- 非 ASCII 文本（中文、emoji 等）只支持与 RPA 进程共享同一个 Windows 交互桌面的回环 VNC。确认满足这个条件后，启动服务前设置：

```powershell
$env:RPA_LOCAL_SENDINPUT = "1"
```

该开关默认关闭。若 `localhost` 实际是 SSH/Docker 转发、独立 VNC 会话或远程主机，请勿启用；当前 `vncdotool` 通道无法可靠保证这些场景的 Unicode 输入，Agent 会明确报错并停止重复输入。
