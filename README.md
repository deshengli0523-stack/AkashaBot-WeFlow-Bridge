# AkashaBot WeFlow Bridge

这是一个面向 Windows 的本地桥接与安装项目，让 WeFlow 通过 OneBot v11 与 AstrBot 协作。公开仓库只包含桥接程序、联系人记忆插件源码、安装脚本、测试和公开文档，不包含 WeFlow 安装包、AstrBot 数据、个人配置或用户数据。

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

## 私聊联系人独立记忆

安装器会在 AstrBot 完成初始化后部署 `Akasha 联系人记忆` 插件。它只处理本系统桥接进来的微信私聊，不为群聊建立记忆：

- 联系人主键是微信账号与稳定 `sessionId` 的组合，不是昵称；同名联系人不会共用上下文。昵称只用于微信 UIA 路由，发送前还会用 WeFlow 联系人接口核对唯一的 `sessionId`；同名、目标不符或无法校验时拒绝发送，不会点击第一个结果。无法区分时请给联系人设置唯一备注名。
- 每个联系人拥有独立的 Qwen Conversations 会话，但所有联系人共用 AstrBot 中已有的一个 Qwen Provider 和 API Key，不需要为每个人配置一套 API。
- A → B → A 切换时会继续使用 A 原来的 `conversation_id`，不会重新发送 A 的全部历史；云端仍按上下文 token 计费，Session Cache 命中可降低缓存部分费用，真正控制用量依靠相关旧记录、最近记录和软上限重建。
- 本地 SQLite 永久保存 WeFlow 实际记录。首次使用某联系人时前台目标同步最近 2,000 条，超时则尝试 500 条，完整旧记录在后台继续归档且没有总条数上限。
- 图片和视频的模型转述会与 WeFlow 原始媒体占位符分开保存；联系人云端会话重建或切换模型后仍能恢复视觉语义，但不会把转述冒充成微信原文。原始视频只在本机临时下载用于转述，完成后删除。
- 新建或七天到期的云端会话最多播种约 150,000 token，在约 700,000 token 时提前重建，为 1,000,000 token 模型窗口保留回复与工具余量。

更新和首次安装默认是 `shadow` 模式：只归档，不改变回复。先在桥接面板填写当前机器人微信账号的真实 `bot_wxid`；缺少该稳定账号标识时，私聊身份、记忆和发送都会失败关闭。然后在 AstrBot 配置一个可调用 Qwen Responses API 的 Provider（模型 `qwen3.7-max`），在插件配置中选择它并确认 `/akasha_memory status` 正常，再切换为 `active` 并重载插件。插件只引用原 Provider 凭据，不会把 API Key 复制到第二份配置。AstrBot 的 `tool_schema_mode` 应保持默认的 `full`；`skills_like` 下带工具的轮次会回退到源 Provider，并安全重建联系人会话。

图片和视频转述使用桥接面板中的“图片与视频描述”配置，与联系人记忆使用的 `qwen3.7-max` Provider 相互独立。使用视频时请选择支持视频输入的 OpenAI 兼容视觉模型（例如 `qwen3.7-plus`）；Base64 视频只处理不超过 6 MiB 的本机 WeFlow 视频，超限、尚未下载到微信本地或视觉服务失败时会安全降级为媒体占位符。

管理命令：

- `/akasha_memory status`
- `/akasha_memory rebuild`
- `/akasha_memory forget CONFIRM`

命令需要 AstrBot 管理员权限。`rebuild` 和 `forget` 必须从目标联系人的 Akasha 私聊会话执行；删除时会先删除云端 items 和 conversation，成功后再删除本地记忆。

## 分段发送与发送审核

安装器会启用 AstrBot 的 LLM 分段回复：中英文句末标点、普通或全角空格、制表符、换行和空行都会触发分段；无分隔符时强制每段不超过 15 个字符。句末标点会保留，作为边界的空白会移除；各段随机间隔 0.8–1.8 秒进入桥接发送队列。

文本消息严格按 FIFO 顺序处理。当前一条会先出现在 `http://127.0.0.1:8766` 控制面板，默认等待 1 秒再粘贴到微信输入框，粘贴后再等待 10 秒才点击发送。等待期间：

- “暂停”会冻结当前倒计时和后续队列；“恢复”从当前条的剩余时间继续，不会丢弃或重复发送。
- “取消此条”只取消面板显示的 `preview_id` 对应消息，旧按钮或重复请求不会误取消下一条。
- 已进入“正在发送”的消息不能再报告为取消成功。
- “停止”是结束本次运行，不承诺在进程重启后保留内存队列。

等待期间微信输入框由机器人占用，请不要手工修改其中的待发内容；取消粘贴后的消息会清空该输入框。当前待发联系人和完整正文仅通过回环地址显示，但仍属于本机敏感聊天数据，不要把面板开放给其他设备或不受信任的本机用户。

图片仍走原有发送流程，不显示正文预览、不增加 10 秒等待，也不能按单条取消；全局暂停和 FIFO 顺序仍然生效。默认等待时间可在桥接面板设置中通过 `uia_fixed_pre_paste_preview_delay` 和 `uia_fixed_pre_send_delay` 调整。更新不会覆盖已有的新版嵌套 `uia_fixed_calibration`，因此使用当前校准格式的电脑无需仅因本功能重新校准。

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

安装日志位于 `data\logs\install.log`，桥接运行日志位于 `data\logs\bridge.log`，稳定身份映射、运行状态和联系人记忆迁移备份位于 `data\state`。如果 Bridge 在完整日志初始化前退出，`data\logs\bridge-startup.log` 会记录一行经过凭据和本机路径脱敏的固定诊断；`启动.bat` 也会直接提示检查这两个日志。联系人消息数据库与本机 DPAPI 密钥封装位于 AstrBot 的 `data\plugin_data\astrbot_plugin_akasha_contact_memory`。`bridge.log` 默认记录私聊联系人、群名与群成员，以及收到的完整正文和 Bot 尝试发送的完整正文；发送记录同时标注 `sent` 或 `failed`。令牌、API Key 和本机路径仍会脱敏。未加引号且带空格的本机路径边界存在歧义时，脱敏会优先避免泄露，并可能连带遮住紧邻文本；消息中给路径加引号可保留准确边界。

从 `0.3.2` 开始，启动器会在生命周期锁内核验 `data\state\bridge.pid`：已退出进程或 PID 已被复用时自动清理，确有存活 Bridge 时拒绝重复启动，身份无法核验时失败关闭。`停止.bat` 在 `processes.json` 丢失或为空时也会核验并停止属于本安装目录的孤儿 Bridge，不再需要手工删除 PID 文件。

Web 控制面板会从 `bridge.log` 的结构化 `CHAT` 记录中显示最近的完整联系人名称、群名、群成员、收发方向、发送状态和完整正文；不会返回其他运行日志。面板及聊天接口只接受本机回环 Host，不提供跨域读取，并使用文本节点渲染聊天内容。面板内容与 `bridge.log` 同属本机高敏数据。

求助时只能提供经过人工逐行检查并删除联系人、正文、路径和凭据后的少量摘录，同时说明固定错误码、失败阶段和组件版本。绝不要发送 `data\bridge\config.json`、`FIRST_LOGIN.txt`、API Key、令牌、数据库、聊天内容、附件、整个 `data` 目录或未经检查的原始日志。详细规则见 [SECURITY.md](SECURITY.md)。

## 本地开发验证

在仓库根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\Run-All.ps1
```

桥接代码采用 MIT 许可证。AstrBot 与 WeFlow 分别适用其上游许可证和条款；本项目不隶属于、不代表、也不受 WeFlow 或 AstrBot 官方背书。
