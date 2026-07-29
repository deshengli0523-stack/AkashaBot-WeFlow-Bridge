# Akasha 微信收藏表情插件

该插件为 AstrBot 4.26.6 注册一个模型工具：

`send_wechat_favorite_sticker(sticker_id)`

模型只选择语义 ID。插件将其映射为 `slot_01` 到 `slot_20`，
并通过 aiocqhttp 调用 Akasha Bridge 的同名自定义 action。
它不会发送 OneBot `face` 或普通图片。

## 配置表情语义

插件目录中的 `catalog.json` 只是首次运行的默认种子。安装环境首次加载时，
插件会将它原子复制为：

`%AKASHABOT_STATE_DIR%\favorite-sticker-catalog.json`

之后只读取这份持久文件，插件更新不会覆盖用户已经配置的语义。请编辑持久
文件，为每项填写：

- `id`：给模型看的稳定语义 ID，只能使用小写字母、数字、下划线和连字符；
- `sticker_key`：Bridge 使用的固定位置键，不得改变 `slot_01` 到 `slot_20` 的全集；
- `description`：表情画面和语气；
- `use_when`：适合使用的上下文；
- `avoid_when`：容易误解或不应使用的上下文。

必须恰好保留 20 项，而且每个 `sticker_key` 只能出现一次。持久目录必须
是状态目录的直系普通文件，不能是符号链接或 Windows reparse point。保存
后在 AstrBot 中重载插件，新的目录才会进入模型工具说明。目录不合法时
插件会拒绝加载，不会悄悄退回默认目录，防止错误槽位被发送。

## Bridge action 契约

插件只会发送以下参数：

```json
{
  "user_id": 123,
  "sticker_key": "slot_01",
  "request_id": "UUID"
}
```

群聊改用 `group_id`，且 `user_id` 与 `group_id` 始终只出现一个。
Bridge 负责目标路由、标定、完整暴露的固定 4×5 槽位点击和送达确认。当前发送路径
不截图、不读取模板，也不检查目标画面；收藏顺序、数量或内容改变后必须
同步复核目录并重新标定。

插件在每个 AstrBot 消息事件中最多尝试一次，并为同一会话设置冷却。
超时或送达状态未知时不会自动重试，避免原生表情被重复发送。

动画收藏项仍由微信原生点击链路发送，不会被插件转成普通图片。WeFlow
回执只能确认同一会话出现了新的己方原生表情，不能确认固定槽位中的内容
正确。无论成功或失败，插件都不会自动重试。
