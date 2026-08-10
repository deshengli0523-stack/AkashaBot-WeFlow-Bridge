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
4. 先按校准向导依次点击搜索框、第一条搜索结果或会话、消息输入框、发送按钮并确认。输入确认后，向导会把刚才完成标定的微信窗口重新切回前台；随后关闭表情面板，继续标定左下角笑脸、收藏心形标签、第 1 格中心和第 20 格中心。
5. 收藏阶段的四次标定点击都会被吞掉，不会选中或发送表情。向导只保存完整暴露的前四行固定 4×5 网格比例坐标，不截图；按 `Esc` 或拒绝确认会取消本次保存。
6. 校准成功后双击 `启动.bat`，再运行 `健康检查.bat`。

校准点使用微信客户区比例保存。更换显示器、调整 DPI、改变微信窗口宽高比或界面布局后，先停止服务，运行 `校准.bat` 重新校准，再启动。校准数据不应手工编辑。

## 5. 完成 WeFlow 首次向导

第一次打开 WeFlow 时，按其界面完成首次设置。安装器不会读取、移动或删除微信数据库。

如果看到 `E_WEFLOW_CONFIG_MISSING`，先完成 WeFlow 首次向导并完全关闭 WeFlow，再重新运行 `安装.bat`。如果 WeFlow 仍在运行，配置保护会返回 `E_WEFLOW_RUNNING`。

配置完成后，WeFlow HTTP API 使用 `127.0.0.1:5031`，OneBot v11 反向 WebSocket 使用 `127.0.0.1:11229`。不要复制或公开配置文件。

## 6. AstrBot 首次登录

安装器创建 AstrBot 独立环境与数据目录。首次登录文件位于：

`%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\data\astrbot\FIRST_LOGIN.txt`

打开 `http://127.0.0.1:6185`，使用文件中的初始信息登录，立即修改 AstrBot 密码，并配置模型提供商与 API Key。确认新密码可以登录后安全删除 `FIRST_LOGIN.txt`。桥接面板位于 `http://127.0.0.1:8766`，会显示最近的完整联系人名称、收发方向和聊天正文。公众号、频道与所有群聊入站消息会在进入红包/转账、媒体和普通消息处理前过滤；主动向群聊发送消息的能力不受影响。

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

安装器同时部署 `Akasha 合并回复` 插件，关闭 AstrBot 流式输出，并保留已有的发送预览时长。该插件要求专用 AstrBot 数据目录；如果发现 `akasha_ob11` 之外仍有启用的平台，初始化会以 `E_ASTRBOT_SHARED_INSTANCE` 失败关闭。普通私聊默认使用 1.5 秒静默合并窗口和 5 秒最长窗口；可在桥接面板调整。对方在旧回复点击发送前开启新话题时，同一联系人的旧轮次会被淘汰，其他联系人及已经进入提交边界的发送不受影响。

### 配置原生收藏表情

安装器已经部署 `Akasha 微信收藏表情` 插件。完成上述校准并首次启动
AstrBot 后，编辑：

`%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge\data\state\favorite-sticker-catalog.json`

文件必须保留恰好 20 项，并让 `sticker_key` 唯一覆盖 `slot_01` 到
`slot_20`。把每项的 `id` 改成稳定、易懂的小写语义 ID，并填写
`description`、`use_when`、`avoid_when`，然后在 AstrBot 中重载插件。
插件更新不会覆盖该持久文件。

模型工具每个消息事件最多调用一次，同一会话默认冷却 60 秒。Bridge 通过
标定点打开笑脸和收藏标签，然后根据 `slot_01` 到 `slot_20` 直接计算完整
暴露的固定 4×5 格子坐标并点击；发送时不截图、不读取模板，也不进行置信度或唯一性
检查。路由不可信、窗口比例变化、暂停、取消或超时仍会失败关闭。点击后还
必须由 WeFlow 在同一会话发现新的己方原生表情记录才报告确认；超时或结果
未知时不要重试。笑脸到收藏标签及收藏标签到目标槽位的等待独立随机采样；
默认范围分别为 0.8–1.3 秒和 0.9–1.5 秒。收藏项本身是动画时由微信原生链路
发送，不转换为普通图片。

添加、删除、替换或调整收藏顺序后请立即停止服务，重新校准并同步复核语义
目录。固定槽位直点不能发现目标缺失或内容错位；WeFlow 回执只能确认发出了
某个原生收藏表情，不能证明它就是语义目录描述的那一张。

### 配置红包与转账接收

安装器已经部署 `Akasha 红包与转账接收` 插件。进入 AstrBot 插件配置后：

1. 保持 `enabled=true`。
2. 将 `vision_provider_id` 选择为支持图片输入的 AstrBot Provider；插件引用该 Provider 的现有凭据，不保存第二份 API Key。
3. `bridge_url` 保持 `http://127.0.0.1:8766`。该字段只允许本机回环 HTTP 地址。
4. 默认单次最多执行 12 个视觉步骤；需要时可在 1–30 范围调整 `max_agent_steps`。
5. 红包与收到的转账均直接进入 Agent 接收流程，不设置金额上限。
6. 最大化并保持微信位于前台。接收链路不调用 Windows OCR；桥按来源联系人搜索固定首结果，AstrBot 多模态 Agent 从截图核对聊天标题并在不一致时要求重新选择。当前发送完成后，普通 FIFO 会等待到视觉正常页与 WeFlow 收款回执同时成立。

Bridge 配置中的 `money_receive_enabled` 默认开启，`money_receive_timeout_seconds` 默认 180 秒，`money_receipt_poll_seconds` 默认 1 秒。插件的单次模型调用默认最多 60 秒，但不会超过桥层事务剩余期限。超时、停止、窗口状态不符或 Provider 失败都会结束本次事务并恢复普通 FIFO，但不会报告收款成功。

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

## 8. 一键更新

1. 下载并完整解压新版源码，或在现有源码目录执行 Git 更新。源码目录必须位于当前安装目录之外。
2. 从新版源码根目录双击 `一键更新.bat`。
3. 更新器自动停止 AkashaBot、AstrBot、Bridge 和 WeFlow，再调用事务式安装器更新程序与四个 AstrBot 插件。
4. 如果现有校准有效，安装器自动启动服务并运行聚合健康检查。如果提示需要校准，请运行安装目录的 `校准.bat`，完成后运行 `启动.bat` 和 `健康检查.bat`。

`一键更新.bat` 是源码包入口，不会复制到安装目录。首次安装请运行 `安装.bat`。默认更新 `%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge`；自定义位置可在命令行运行 `一键更新.bat -InstallRoot "<自定义安装目录>"`。需要只更新而不启动时，可追加 `-SkipStart`。

安装器拒绝在已记录服务仍运行时覆盖文件，并返回 `E_INSTALL_RUNNING`。正常重装保留安装根目录下的 `data`、联系人记忆数据库、收藏表情模板、持久语义目录和现有配置；旧桥接目录以及修改前的 WeFlow/AstrBot 配置会备份到 `data\backups`。三个 AstrBot 插件的代码都会在 AstrBot 初始化完成后分别暂存并原子替换，失败会恢复旧插件代码，不会删除 `data\astrbot\data\plugin_data` 或 `data\state` 中的收藏表情数据。

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
| `E_UPDATE_LOCATION` | 更新包与安装目录重叠；把 ZIP 完整解压到安装目录之外。 |
| `E_UPDATE_PACKAGE` | 更新包缺少必要文件；重新下载并完整解压。 |
| `E_UPDATE_NOT_INSTALLED` | 未找到现有安装；首次安装应运行 `安装.bat`。 |
| `E_UPDATE_STOP` | 无法安全停止现有服务；检查上方错误后重试。 |
| `E_UPDATE_INSTALL` | 安装阶段失败；检查 `data\logs\install.log`。 |
| `E_PROCESS_STATE` | `data\state\processes.json` 缺少可信结构或已损坏；保留错误码求助。 |
| `E_HEALTH_FAILED` | 四项健康检查至少一项失败；记录每项结果。 |
| `E_UIA_CALIBRATION_REQUIRED` | 尚未完成校准；运行 `校准.bat`。 |
| `E_UIA_CALIBRATION_INVALID` | 校准文件结构或数值无效；重新校准。 |
| `E_UIA_CALIBRATION_WINDOW` | 未找到可用微信窗口，或窗口状态不满足校准要求。 |
| `E_UIA_CALIBRATION_BUSY` | 校准或生命周期操作正在进行；等待结束后重试。 |
| `E_UIA_RECALIBRATION_REQUIRED` | 当前 DPI 或宽高比与参考不兼容；重新校准。 |
| `E_UIA_STICKER_CALIBRATION_REQUIRED` | 尚未生成收藏表情槽位标定；重新运行 `校准.bat`。 |
| `E_UIA_STICKER_TEMPLATE_MISSING` | 旧识图模式兼容错误；当前固定槽位直点路径不产生。 |
| `E_UIA_STICKER_MATCH_LOW_CONFIDENCE` | 旧识图模式兼容错误；当前固定槽位直点路径不产生。 |
| `E_UIA_STICKER_MATCH_AMBIGUOUS` | 旧识图模式兼容错误；当前固定槽位直点路径不产生。 |
| `E_UIA_STICKER_CONFIRMATION_UNKNOWN` | 已点击但未在期限内取得 WeFlow 回执；不要自动重试。 |

## 10. 日志、检测与安全排障

安装日志位于 `data\logs\install.log`，桥接运行日志位于 `data\logs\bridge.log`；`data\state` 还包含稳定身份映射、联系人记忆迁移前的一致性 SQLite 快照、收藏表情槽位标定和 `favorite-sticker-catalog.json` 持久语义目录。旧版本生成的 `favorite-sticker-templates` 私有模板会原样保留，但当前直点路径不读取，也不会在更新时主动删除。联系人消息数据库和 DPAPI 保护的本机密钥封装位于 `data\astrbot\data\plugin_data\astrbot_plugin_akasha_contact_memory`。`bridge.log` 默认记录私聊联系人、群名与群成员、收到的完整正文、Bot 尝试发送的完整正文及 `sent`/`failed` 状态；令牌、API Key 和本机路径仍会脱敏。未加引号且带空格的本机路径边界存在歧义时，脱敏会优先避免泄露，并可能连带遮住紧邻文本；消息中给路径加引号可保留准确边界。

Web 控制面板只解析结构化 `CHAT` 记录，不返回其他原始运行日志；聊天区域显示完整联系人、群名、群成员、方向、状态和正文。面板及接口只接受 `127.0.0.1` 或 `localhost` 的同源请求。任何能使用当前 Windows 账户打开该面板的人都可能看到聊天内容。

当前 `data\bridge\config.json`、`data\backups`、`data\state` 中的数据库或备份，以及 AstrBot `plugin_data` 都可能包含敏感身份或聊天数据，诊断系统不会收集这些数据。`bridge.log` 也属于本机高敏数据。求助时只能提供经过人工逐行检查、删除联系人、正文、路径和凭据后的少量日志摘录，同时提供固定错误码、失败阶段、组件版本和四项健康检查结果。

绝不要发送 `data\bridge\config.json`。不要发送整个 `data` 目录、原始未检查日志、`FIRST_LOGIN.txt`、WeFlow/AstrBot 配置、API Key、令牌、数据库、聊天内容或附件。

## 11. 本地开发验证

在仓库根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\Run-All.ps1
```

项目桥接代码采用 MIT 许可证。第三方许可见 `THIRD_PARTY_NOTICES.md`；本项目与 WeFlow、AstrBot 官方没有从属、代理或背书关系。
