# Akasha 合并回复

此插件只接管 Akasha Bridge 标记过的 `aiocqhttp` 私聊纯文本模型结果：

- 将同一轮的全部 `Plain` 内容作为一条微信消息提交；
- 在 AstrBot 核心分段前清空并停止默认结果，避免重复发送；
- 用 5 秒内返回的持久受理动作把长 UIA 审核移到 Bridge 后台 worker；
- 通过 5 秒续租、15 秒失效的连接能力租约发布 readiness，并在旧记忆分页对账完成前保持普通入站关闭；
- 每个普通批次携带持久 admission，标准私聊文字发送不能绕过它；
- Bridge 通过 `bridge_generation + reply_epoch` 判断回复是否仍属于联系人最新一轮；
- 新消息到达后，尚未进入微信提交边界的旧回复会返回 `superseded`，不会继续发送；
- 发送终态通过持久 outbox 重放，联系人记忆按 `request_id` 应用后 ACK；
- 图片、表情、群聊、命令和其他平台维持原有发送路径。

插件要求专用 AstrBot 实例的 `provider_settings.streaming_response=false`。它不会缩短 Bridge 的粘贴预览或发送审核时间；结果一旦被认领，异常和超时也会失败关闭，不会回退到 AstrBot 默认分段发送。
