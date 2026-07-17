# AkashaBot WeFlow Bridge

这是一个面向 Windows 的本地桥接与安装项目：让 WeFlow 通过 OneBot v11 与 AstrBot 协作。仓库只包含桥接程序、安装脚本和公开文档，不包含 WeFlow 安装包、AstrBot 数据、个人配置或用户数据。

## 安装前准备

1. Windows 10/11 x64。
2. 用户自行预装 Python 3.12 x64；安装器不会下载或安装 Python。
3. 准备一个保存在本机的 WeFlow `.exe` 或 `.msi` 安装包。
4. 保持网络能够访问 PyPI，以安装已锁定的桥接依赖和 `astrbot==4.26.6`。

## 安装

下载仓库 ZIP 并完整解压，或用 Git 克隆仓库，然后双击 `安装.bat`。当安装向导要求选择文件时，只选择你本机已有的 WeFlow `.exe` 或 `.msi` 安装包；该文件不会被复制进本项目或上传到 GitHub。

如果看到 `E_WEFLOW_CONFIG_MISSING`，请先打开 WeFlow 完成首次向导，然后完全关闭 WeFlow，再次双击 `安装.bat`。

默认安装位置：`%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge`

安装器会创建两个独立环境：

- 桥接：`%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\runtime\venvs\bridge`
- AstrBot：`%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\runtime\venvs\astrbot`

安装完成后的本地端口：

- WeFlow HTTP API：`127.0.0.1:5031`
- AstrBot WebUI：`127.0.0.1:6185`
- OneBot v11 反向 WebSocket：`127.0.0.1:11229`
- 桥接面板：`127.0.0.1:8766`

首次登录信息位于 `%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\data\astrbot\FIRST_LOGIN.txt`。首次登录 AstrBot 后应立即修改密码，并在 WebUI 中配置模型供应商与 API Key。确认新密码可用后，安全删除 `FIRST_LOGIN.txt`。

## 日常使用

安装目录和桌面快捷方式都提供以下入口：

- `启动.bat`
- `停止.bat`
- `健康检查.bat`

`健康检查.bat` 是只读检查，会分别验证 WeFlow、AstrBot、OneBot 和桥接面板，不会启动或停止进程。

## 更新

Phase 1 没有自动更新器，也不提供 `更新.bat`。当前手动更新方式是：先运行 `停止.bat` 并确认 WeFlow 已完全关闭，下载或克隆较新的完整源码，在新源码目录运行新的 `安装.bat`。安装器会保留 `data` 与现有配置，并把旧桥接和被修改的配置备份写入 `data\backups`。

## 日志与安全报错

安装日志位于 `data\logs\install.log`，包含安装阶段、状态、固定错误码、版本，以及经过凭据形状脱敏的依赖安装和初始化输出。程序不会主动把微信消息、联系人或附件正文写入安装日志；但原始日志仍可能包含本机路径、用户名或第三方依赖输出，必须视为私密资料。Phase 1 尚不提供完整诊断 ZIP 导出。

公开求助时，只提供固定错误码、失败阶段、组件版本、四项健康检查结果，以及人工检查并脱敏后的少量安装日志摘录。不要上传整个 `data` 目录、原始日志、`FIRST_LOGIN.txt`、WeFlow/AstrBot 配置、API Key、令牌、数据库、聊天内容、附件或含有这些信息的截图。

详细安装步骤见 [INSTALL.md](INSTALL.md)，安全报告规则见 [SECURITY.md](SECURITY.md)。

## 本地开发

在仓库根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\Run-All.ps1
```

本项目桥接代码采用 MIT 许可证，详见 `LICENSE`。AstrBot 与 WeFlow 各自适用其上游许可和条款，详见 `THIRD_PARTY_NOTICES.md`；本项目不隶属于、不代表、也不受 WeFlow 或 AstrBot 官方背书。
