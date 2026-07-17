# Windows 安装与更新向导

## 1. 前置检查

仅支持 Windows 10/11 x64。请先自行安装 Python 3.12 x64；安装器会严格检查版本与 64 位架构，但不会安装 Python。

可在 Windows PowerShell 中检查：

```powershell
py -3.12 -c "import platform,sys; print(sys.version); print(platform.architecture()[0])"
```

输出应为 Python 3.12.x 和 `64bit`。安装过程需要访问 PyPI，以安装锁定的桥接依赖和 `astrbot==4.26.6`。

## 2. 准备 WeFlow

把你已有的 WeFlow Windows `.exe` 或 `.msi` 安装包保存在本机。不要把它放进本仓库；安装向导只把所选路径交给 Windows 安装程序，不会复制或上传安装包。

## 3. 获取并运行安装向导

下载仓库 ZIP 后完整解压，或克隆仓库。不要直接在压缩包预览中运行文件。双击仓库根目录的 `安装.bat`。

安装器会检查 Windows 10/11 x64 和 Python 3.12 x64。未发现 WeFlow 时会弹出文件选择框；只选择本机 WeFlow `.exe` 或 `.msi`。取消选择会得到 `E_WEFLOW_CANCELLED`，安装程序返回非零会得到 `E_WEFLOW_INSTALL_FAILED`。

如果安装后仍无法发现 WeFlow，会显示 `E_WEFLOW_NOT_DETECTED`。完成 WeFlow 安装后重新运行 `安装.bat`。

默认安装根目录是：

`%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge`

两个 Python 环境相互隔离：

- `%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\runtime\venvs\bridge`
- `%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\runtime\venvs\astrbot`

## 4. 完成 WeFlow 首次向导

第一次打开 WeFlow，按其界面完成首次设置。安装器不会读取、移动或删除你的微信数据库。

如果安装器显示 `E_WEFLOW_CONFIG_MISSING`，这是可恢复状态：完成 WeFlow 首次向导，完全关闭 WeFlow，再次双击 `安装.bat`。如果 WeFlow 仍在运行，配置保护会返回 `E_WEFLOW_RUNNING`，此时也应先关闭 WeFlow再重试。

配置完成后，WeFlow 本地 HTTP API 使用 `127.0.0.1:5031`，OneBot v11 反向 WebSocket 使用 `127.0.0.1:11229`。桥接和 WeFlow 使用安装时生成的共享随机令牌；不要复制或公开配置文件。

## 5. AstrBot 首次登录

安装器会创建 AstrBot 独立环境与数据目录。首次登录文件位于：

`%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\data\astrbot\FIRST_LOGIN.txt`

打开 `http://127.0.0.1:6185`，使用该文件中的初始信息登录，立即修改 AstrBot 密码，并在 AstrBot WebUI 中配置模型供应商和 API Key。确认新密码可以登录后，安全删除 `FIRST_LOGIN.txt`；不要把它截图或发送给任何人。

桥接面板位于 `http://127.0.0.1:8766`。

## 6. 日常启动、停止和健康检查

安装目录中有 `启动.bat`、`停止.bat`、`健康检查.bat`，安装完成时还会在桌面创建启动、停止、检查三个快捷方式。

双击 `健康检查.bat` 会只读检查以下四项：

| 组件 | 检查目标 |
| --- | --- |
| WeFlow | `http://127.0.0.1:5031/health` |
| AstrBot | `http://127.0.0.1:6185/` |
| OneBot | TCP `127.0.0.1:11229` |
| Bridge | `http://127.0.0.1:8766/status` |

健康检查不会启动、停止或修改任何服务。全部成功才返回 0；任一 `[FAIL]` 都会返回非零。安装结束时的聚合失败码是 `E_HEALTH_FAILED`。

## 7. 手动更新

Phase 1 没有自动更新功能，也没有 `更新.bat`。按以下步骤手动更新：

1. 运行当前安装目录的 `停止.bat`，并确认 WeFlow 已完全关闭；外部启动且未由本项目拥有的 WeFlow 不会被停止脚本强行关闭。
2. 下载并完整解压较新的源码，或在另一个源码目录执行 Git 更新。
3. 从较新源码目录运行新的 `安装.bat`。
4. 安装结束后运行 `健康检查.bat`，确认四项均为 `[OK]`。

安装器拒绝在记录的服务仍运行时覆盖文件，并返回 `E_INSTALL_RUNNING`。正常重装会保留安装根目录下的 `data` 和现有配置；旧桥接目录以及修改前的 WeFlow/AstrBot 配置会备份到 `%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\data\backups`。

## 8. 常见错误

| 错误或现象 | 含义与处理 |
| --- | --- |
| `E_PYTHON_312_X64` | 未找到严格匹配的 Python 3.12 x64。安装正确架构并启用 `py` 启动器或 PATH。 |
| `E_WEFLOW_CANCELLED` | 取消了本地 WeFlow 安装包选择；重新运行并选择 `.exe` 或 `.msi`。 |
| `E_WEFLOW_INSTALL_FAILED` | WeFlow 安装程序返回非零；先处理其安装错误，再重试。 |
| `E_WEFLOW_NOT_DETECTED` | 安装后仍未发现 WeFlow；完成安装并重新运行向导。 |
| `E_WEFLOW_CONFIG_MISSING` | WeFlow 首次配置尚未生成；完成首次向导并重试。 |
| `E_WEFLOW_RUNNING` | WeFlow 正在运行，安装器拒绝改写配置；完全关闭后重试。 |
| `E_LIFECYCLE_BUSY` | 另一个安装、启动或停止操作正在占用生命周期锁；等待其完成，不要并行运行入口。 |
| `E_INSTALL_RUNNING` | 进程状态仍记录服务；先使用 `停止.bat`，确认停止后再更新。 |
| `E_PROCESS_STATE` | `data\state\processes.json` 缺失可信结构或已损坏；不要手工结束不相关进程，保留错误码并求助。 |
| `E_HEALTH_FAILED` 或 `[FAIL]` | 四项健康检查至少一项失败；记录每一项结果并检查对应端口/服务。 |

## 9. 日志、检测与报错材料

`%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\data\logs\install.log` 包含安装阶段、状态、固定错误码、组件版本，以及经过凭据形状脱敏的依赖安装和 AstrBot 初始化输出。程序不会主动写入微信消息、联系人或附件正文，但原始日志仍可能带有本机路径、用户名或第三方依赖输出，必须作为私密资料处理。`健康检查.bat` 是只读检测。Phase 1 不包含完整诊断 ZIP 导出。

如果需要提交错误，请只整理以下安全材料：

1. 固定错误码，例如 `E_WEFLOW_CONFIG_MISSING`。
2. 失败阶段，例如 prerequisite、payload、configuration、start 或 health。
3. Windows、Python、桥接、AstrBot 和 WeFlow 的组件版本。
4. WeFlow、AstrBot、OneBot、Bridge 四项健康检查结果。
5. 从 `data\logs\install.log` 复制的少量摘录，并在发送前人工检查和脱敏。

不得发布整个 `data` 目录、原始日志、`FIRST_LOGIN.txt`、WeFlow/AstrBot 配置、API Key、令牌、数据库、聊天内容、附件、用户名路径，或包含这些内容的截图。

## 10. 本地开发验证

从仓库根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\Run-All.ps1
```

项目桥接代码采用 MIT 许可证。第三方许可见 `THIRD_PARTY_NOTICES.md`；本项目与 WeFlow、AstrBot 官方没有从属、代理或背书关系。
