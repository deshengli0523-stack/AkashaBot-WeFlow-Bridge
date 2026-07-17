# AkashaBot WeFlow Bridge

这是一个面向 Windows 的本地桥接与安装项目，让 WeFlow 通过 OneBot v11 与 AstrBot 协作。公开仓库只包含桥接程序、安装脚本、测试和公开文档，不包含 WeFlow 安装包、AstrBot 数据、个人配置或用户数据。

## 安装前准备

- Windows 10/11 x64。
- 用户预先安装 Python 3.12 x64；安装器不会下载或安装 Python。
- 准备保存在本机的 WeFlow `.exe` 或 `.msi` 安装包。
- 网络可以访问 PyPI，以安装已锁定的桥接依赖和 `astrbot==4.26.6`。

## 首次安装与校准

下载仓库 ZIP 并完整解压，或使用 Git 克隆仓库，然后双击 `安装.bat`。向导要求选择文件时，只选择本机已有的 WeFlow `.exe` 或 `.msi` 安装包；该文件不会被复制进项目或上传到 GitHub。

请严格按以下顺序完成首次使用：

1. 等待向导显示安装成功。未校准时，安装成功只表示文件和环境已经就绪，服务不会自动启动。
2. 双击安装目录中的 `校准.bat`。
3. 登录并最大化微信，校准期间保持窗口大小和显示缩放不变。
4. 按向导依次点击搜索框、第一条搜索结果或会话、消息输入框、发送按钮；程序只保存相对于微信客户区的比例，不在文档或日志中输出点位。
5. 校准成功后双击 `启动.bat`。

默认安装位置：`%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge`。

安装器会创建两个独立环境：

- 桥接：`runtime\venvs\bridge`
- AstrBot：`runtime\venvs\astrbot`

如果更换显示器、调整 DPI、改变微信窗口宽高比，或者微信界面布局明显变化，请先停止服务，再运行 `校准.bat` 重新校准，然后再启动。

## 本地端口与日常入口

- WeFlow HTTP API：`127.0.0.1:5031`
- AstrBot WebUI：`127.0.0.1:6185`
- OneBot v11 反向 WebSocket：`127.0.0.1:11229`
- 桥接面板：`127.0.0.1:8766`

安装目录提供 `校准.bat`、`启动.bat`、`停止.bat` 和 `健康检查.bat`。桌面只创建启动、停止、健康检查三个快捷方式。

`健康检查.bat` 是只读检查，不会启动或停止进程。首次登录信息位于 `%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\data\astrbot\FIRST_LOGIN.txt`；登录 AstrBot 后立即修改密码，确认新密码可用后安全删除该文件。

## 更新

当前采用安全的手动更新流程：先运行现有安装目录中的 `停止.bat` 并完全关闭 WeFlow，再下载或克隆新版完整源码，从新版源码目录运行 `安装.bat`。安装器保留 `data` 和现有配置；已有有效校准可以继续使用，但显示环境或微信布局变化后仍应重新校准。

## 校准错误码

- `E_UIA_CALIBRATION_REQUIRED`：尚未完成校准，运行 `校准.bat`。
- `E_UIA_CALIBRATION_INVALID`：校准数据结构无效，重新校准。
- `E_UIA_CALIBRATION_WINDOW`：未找到合适的微信窗口或窗口状态不符合要求。
- `E_UIA_CALIBRATION_BUSY`：已有校准或生命周期操作正在进行，等待其结束后重试。
- `E_UIA_RECALIBRATION_REQUIRED`：当前 DPI 或宽高比与校准参考不兼容，需要重新校准。

其他安装错误及处理方法见 [INSTALL.md](INSTALL.md)。

## 日志、安全与排障

安装日志位于 `data\logs\install.log`，运行状态记录位于 `data\state`。求助时可以提供 `data\logs` 和 `data\state` 中经人工检查、确认不含秘密的诊断文件，并说明固定错误码、失败阶段和组件版本。

绝不要发送 `data\bridge\config.json`。也不要发送 `FIRST_LOGIN.txt`、API Key、令牌、数据库、聊天内容、附件、整个 `data` 目录或未经检查的原始日志。详细规则见 [SECURITY.md](SECURITY.md)。

## 本地开发验证

在仓库根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\Run-All.ps1
```

桥接代码采用 MIT 许可证。AstrBot 与 WeFlow 分别适用其上游许可证和条款；本项目不隶属于、不代表、也不受 WeFlow 或 AstrBot 官方背书。
