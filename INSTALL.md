# Windows 安装、校准与更新向导

## 1. 前置检查

仅支持 Windows 10/11 x64。请先自行安装 Python 3.12 x64；安装器严格检查版本与 64 位架构，但不会安装 Python。

在 Windows PowerShell 中检查：

```powershell
py -3.12 -c "import platform,sys; print(sys.version); print(platform.architecture()[0])"
```

输出应为 Python 3.12.x 和 `64bit`。安装过程需要访问 PyPI，以安装锁定的桥接依赖和 `astrbot==4.26.6`。

## 2. 准备 WeFlow 安装包

把已有的 WeFlow Windows `.exe` 或 `.msi` 安装包保存在本机，不要把它加入仓库。安装向导只把所选路径交给 Windows 安装程序，不会复制或上传安装包。

## 3. 运行安装向导

下载仓库 ZIP 后完整解压，或克隆仓库；不要在压缩包预览中直接运行文件。双击仓库根目录的 `安装.bat`，按提示选择本机 WeFlow 安装包。

默认安装根目录是 `%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge`，其中两个 Python 环境相互隔离：

- `runtime\venvs\bridge`
- `runtime\venvs\astrbot`

取消选择会得到 `E_WEFLOW_CANCELLED`；WeFlow 安装程序失败会得到 `E_WEFLOW_INSTALL_FAILED`；安装后仍未发现 WeFlow 会得到 `E_WEFLOW_NOT_DETECTED`。

## 4. 首次校准并启动

首次使用必须严格按以下顺序操作：

1. 等待向导显示安装成功。对于尚未校准的全新安装，这是正常的成功状态，安装器不会越过校准直接启动服务或执行健康检查。
2. 双击安装目录中的 `校准.bat`。
3. 登录并最大化微信，保持微信窗口可见，不要在校准过程中改变窗口大小或显示缩放。
4. 按校准向导依次点击搜索框、第一条搜索结果或会话、消息输入框、发送按钮。按 `Esc` 可以安全取消；只有四步全部完成并确认后才会原子保存校准。
5. 校准成功后双击 `启动.bat`，再运行 `健康检查.bat`。

校准点使用微信客户区比例保存。更换显示器、调整 DPI、改变微信窗口宽高比或界面布局后，先停止服务，运行 `校准.bat` 重新校准，再启动。校准数据不应手工编辑。

## 5. 完成 WeFlow 首次向导

第一次打开 WeFlow 时，按其界面完成首次设置。安装器不会读取、移动或删除微信数据库。

如果看到 `E_WEFLOW_CONFIG_MISSING`，先完成 WeFlow 首次向导并完全关闭 WeFlow，再重新运行 `安装.bat`。如果 WeFlow 仍在运行，配置保护会返回 `E_WEFLOW_RUNNING`。

配置完成后，WeFlow HTTP API 使用 `127.0.0.1:5031`，OneBot v11 反向 WebSocket 使用 `127.0.0.1:11229`。不要复制或公开配置文件。

## 6. AstrBot 首次登录

安装器创建 AstrBot 独立环境与数据目录。首次登录文件位于：

`%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\data\astrbot\FIRST_LOGIN.txt`

打开 `http://127.0.0.1:6185`，使用文件中的初始信息登录，立即修改 AstrBot 密码，并配置模型提供商与 API Key。确认新密码可以登录后安全删除 `FIRST_LOGIN.txt`。桥接面板位于 `http://127.0.0.1:8766`，会显示最近的完整联系人名称、群聊成员、收发方向和聊天正文。

### 配置联系人记忆

安装器已经部署联系人记忆插件，但首次安装和更新都保守地保持 `shadow` 模式。按以下顺序启用：

1. 在桥接面板填写当前机器人微信账号的真实 `bot_wxid`。它用于区分不同微信账号；留空时私聊身份、记忆与发送均会失败关闭。
2. 在 AstrBot 中创建或确认一个 OpenAI 兼容的 Qwen Provider，模型填写 `qwen3.7-max`，API Base 使用对应阿里云工作空间的 `/compatible-mode/v1` 根路径。
3. 打开 `Akasha 联系人记忆` 插件配置，将 `source_provider_id` 选择为上述 Provider。正常安装无需填写桥接配置路径，启动脚本会在进程环境中传入。
4. 通过管理员会话运行 `/akasha_memory status`，确认 `qwen_ready: True`。`shadow` 会同步和归档，但不会改变现有回复。
5. 将 `mode` 改为 `active` 并重载插件。之后每个微信私聊联系人使用独立 Qwen conversation；群聊不会进入记忆系统。

如果现有 Provider 的 `api_base` 不是 Responses API 根路径，只覆盖插件的 `qwen_responses_base_url`，API Key 仍从 `source_provider_id` 读取。不要在插件配置中复制 API Key。
AstrBot 的 `tool_schema_mode` 请保持默认的 `full`；改成 `skills_like` 后，带工具的轮次会回退到源 Provider，并在下次回复前重建该联系人的云端会话。

`/akasha_memory rebuild` 会让当前私聊联系人在下次回复时从本地记录重新播种；`/akasha_memory forget CONFIRM` 会删除当前联系人的云端会话和本地记忆。这两个命令只能从目标 Akasha 私聊执行，并要求管理员权限。

私聊回复仍通过昵称或备注名搜索微信，但发送前必须从 WeFlow 联系人接口唯一匹配到本轮稳定 `sessionId`。同名、目标不符或接口不可验证时不会调用 UIA 发送；请为同名联系人设置唯一备注名后再试。

## 7. 日常启动、停止与检查

安装目录中有 `校准.bat`、`启动.bat`、`停止.bat`、`健康检查.bat`。桌面只创建启动、停止、健康检查三个快捷方式。

健康检查是只读操作：

| 组件 | 检查目标 |
| --- | --- |
| WeFlow | `http://127.0.0.1:5031/health` |
| AstrBot | `http://127.0.0.1:6185/` |
| OneBot | TCP `127.0.0.1:11229` |
| Bridge | `http://127.0.0.1:8766/status` |

全部成功才返回 0；任一 `[FAIL]` 都返回非零。安装结束时的聚合失败码是 `E_HEALTH_FAILED`。

## 8. 手动更新

1. 运行当前安装目录的 `停止.bat`，确认 WeFlow 已完全关闭。
2. 下载并完整解压新版源码，或在另一个源码目录执行 Git 更新。
3. 从新版源码目录运行新的 `安装.bat`。
4. 如果显示环境或微信布局变化，运行安装目录的 `校准.bat` 重新校准。
5. 运行 `启动.bat` 和 `健康检查.bat`。

安装器拒绝在已记录服务仍运行时覆盖文件，并返回 `E_INSTALL_RUNNING`。正常重装保留安装根目录下的 `data`、联系人记忆数据库和现有配置；旧桥接目录以及修改前的 WeFlow/AstrBot 配置会备份到 `data\backups`。联系人记忆插件代码在 AstrBot 初始化完成后单独暂存并原子替换，失败会恢复旧插件代码，不会删除 `data\astrbot\data\plugin_data`。

## 9. 常见错误

| 错误码 | 含义与处理 |
| --- | --- |
| `E_PYTHON_312_X64` | 未找到严格匹配的 Python 3.12 x64；安装正确架构并启用 `py` 启动器或 PATH。 |
| `E_WEFLOW_CANCELLED` | 取消了本地 WeFlow 安装包选择；重新运行并选择 `.exe` 或 `.msi`。 |
| `E_WEFLOW_INSTALL_FAILED` | WeFlow 安装程序返回非零；先处理其安装错误。 |
| `E_WEFLOW_NOT_DETECTED` | 安装后仍未发现 WeFlow；完成安装并重新运行向导。 |
| `E_WEFLOW_CONFIG_MISSING` | WeFlow 首次配置尚未生成；完成首次向导并重试。 |
| `E_WEFLOW_RUNNING` | WeFlow 正在运行，安装器拒绝改写配置；完全关闭后重试。 |
| `E_LIFECYCLE_BUSY` | 另一安装、校准、启动或停止操作占用生命周期锁；等待完成。 |
| `E_INSTALL_RUNNING` | 进程状态仍记录服务；先停止并确认后再更新。 |
| `E_PROCESS_STATE` | `data\state\processes.json` 缺少可信结构或已损坏；保留错误码求助。 |
| `E_HEALTH_FAILED` | 四项健康检查至少一项失败；记录每项结果。 |
| `E_UIA_CALIBRATION_REQUIRED` | 尚未完成校准；运行 `校准.bat`。 |
| `E_UIA_CALIBRATION_INVALID` | 校准文件结构或数值无效；重新校准。 |
| `E_UIA_CALIBRATION_WINDOW` | 未找到可用微信窗口，或窗口状态不满足校准要求。 |
| `E_UIA_CALIBRATION_BUSY` | 校准或生命周期操作正在进行；等待结束后重试。 |
| `E_UIA_RECALIBRATION_REQUIRED` | 当前 DPI 或宽高比与参考不兼容；重新校准。 |

## 10. 日志、检测与安全排障

安装日志位于 `data\logs\install.log`，桥接运行日志位于 `data\logs\bridge.log`；`data\state` 还包含稳定身份映射和联系人记忆迁移前的一致性 SQLite 快照，不包含用于定位界面的校准细节。联系人消息数据库和 DPAPI 保护的本机密钥封装位于 `data\astrbot\data\plugin_data\astrbot_plugin_akasha_contact_memory`。`bridge.log` 默认记录私聊联系人、群名与群成员、收到的完整正文、Bot 尝试发送的完整正文及 `sent`/`failed` 状态；令牌、API Key 和本机路径仍会脱敏。未加引号且带空格的本机路径边界存在歧义时，脱敏会优先避免泄露，并可能连带遮住紧邻文本；消息中给路径加引号可保留准确边界。

Web 控制面板只解析结构化 `CHAT` 记录，不返回其他原始运行日志；聊天区域显示完整联系人、群名、群成员、方向、状态和正文。面板及接口只接受 `127.0.0.1` 或 `localhost` 的同源请求。任何能使用当前 Windows 账户打开该面板的人都可能看到聊天内容。

当前 `data\bridge\config.json`、`data\backups`、`data\state` 中的数据库或备份，以及 AstrBot `plugin_data` 都可能包含敏感身份或聊天数据，诊断系统不会收集这些数据。`bridge.log` 也属于本机高敏数据。求助时只能提供经过人工逐行检查、删除联系人、正文、路径和凭据后的少量日志摘录，同时提供固定错误码、失败阶段、组件版本和四项健康检查结果。

绝不要发送 `data\bridge\config.json`。不要发送整个 `data` 目录、原始未检查日志、`FIRST_LOGIN.txt`、WeFlow/AstrBot 配置、API Key、令牌、数据库、聊天内容或附件。

## 11. 本地开发验证

在仓库根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\Run-All.ps1
```

项目桥接代码采用 MIT 许可证。第三方许可见 `THIRD_PARTY_NOTICES.md`；本项目与 WeFlow、AstrBot 官方没有从属、代理或背书关系。
