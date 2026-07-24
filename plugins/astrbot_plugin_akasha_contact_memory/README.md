# Akasha 联系人记忆插件

本插件只服务于 `AkashaBot-WeFlow-Bridge` 的微信私聊链路。它从桥接事件读取稳定的
`account + sessionId`，不使用昵称作为联系人主键，也不会为群聊建立记忆。

## 工作方式

- WeFlow HTTP API 是实际微信聊天记录的最高事实源。
- 本地 `plugin_data/astrbot_plugin_akasha_contact_memory/memory.db` 永久归档记录。
- 每个联系人拥有独立的 Qwen Conversations `conversation_id`。
- 所有联系人共用 AstrBot 中已有 Qwen Provider 的 API Key；插件不会保存第二份 Key。
- 正常连续对话只向 Qwen 发送本轮新消息。云端会话超过 7 天、接近 70 万 token、
  人格变化或工具回退后，会从本地记录重新建立。

## 启用

安装和更新默认使用 `shadow`：同步并归档，但不改变回复 Provider。先在 AstrBot
插件配置中确认 `source_provider_id` 指向可调用 Qwen Responses API 的 Provider，
并检查 `/akasha_memory status`，再将 `mode` 改为 `active` 后重载插件。
启用前还必须在 Akasha 桥接面板填写当前机器人微信账号的真实 `bot_wxid`；
缺少它时系统无法安全区分多微信账号，私聊身份、记忆和发送会失败关闭。
AstrBot 的 `tool_schema_mode` 请保持默认的 `full`；如果改成 `skills_like`，
带工具的轮次会回退到源 Provider，并让当前云端会话在下次回复前安全重建。

源 Provider 的 `api_base` 应为阿里云 OpenAI 兼容 API 根路径（通常以
`/compatible-mode/v1` 结尾）。如源 Provider 不是该地址，可只填写
`qwen_responses_base_url`；API Key 仍从源 Provider 读取。

## 管理命令

- `/akasha_memory status`
- `/akasha_memory rebuild`
- `/akasha_memory forget CONFIRM`

命令仅允许 AstrBot 管理员使用。`forget` 会先逐条删除云端 conversation items，
再删除 conversation，全部成功后才删除本地联系人记忆；云端不可用时本地数据保持
不变但立即进入墓碑状态，停止继续回填，便于安全重试且不会懒加载恢复旧历史。

昵称只用于 UIA 路由，不参与记忆主键。桥接发送前会用 WeFlow 联系人接口核对
唯一的稳定 `sessionId`；同名、目标不符或接口不可验证时拒绝发送。请为无法区分
的联系人设置唯一备注名。

请不要上传 `plugin_data`、SQLite 数据库、桥接配置、API Key 或聊天内容。
