# AkashaBot 合并回复与同联系人旧回复淘汰设计

日期：2026-07-30
状态：用户已批准总体方向，等待书面规格复核

## 1. 背景

当前 AkashaBot 将一轮模型回答按标点和长度切成多个短段。每一段都会单独进入 Bridge 的全局 UIA FIFO，并分别经历联系人选择、粘贴前预览、正文粘贴、粘贴后审核和提交。

这使粘贴审核时间与整轮回复时长成倍相乘。更严重的是，新入站消息只进入接收缓冲，不会让旧回答失效。联系人已经开始下一个话题时，机器人仍会发送旧回答剩余段。

最近一次真实日志展示了完整复现：

- 03:58:34，同一联系人发送新话题“发个表情来看看”。
- 03:58:35、03:58:45、03:58:54，机器人继续发送上一个“请吃饭”话题的三个剩余段。
- 新话题的回答只能在旧段全部结束后发送。

这不是单个延时参数的问题，而是入站缓冲、模型生成、AstrBot 分段和 UIA 发送之间没有共享的“当前回复回合”身份。

## 2. 目标

本设计必须同时实现以下行为：

1. 同一联系人连续发送的多条普通文本消息合并成一个输入回合。
2. 一轮纯文本模型回答合并成一条微信消息。
3. 每轮回答只执行一次正文预览、一次粘贴后审核和一次提交。
4. 保留用户配置的长粘贴审核时间，不为抢占路径创建零延时或短延时旁路。
5. 同一联系人发来新的普通消息时，所有尚未提交的旧合并文本回复立即失效。
6. 已跨过不可撤销提交边界的消息最多完成一次；系统不尝试撤回已经发送的微信消息。
7. 其他联系人不被淘汰，现有全局物理 UIA FIFO 顺序保持不变。
8. 红包/转账接收优先通道、收藏表情幂等与回执规则保持不变。
9. 被淘汰的内容不计为已送达消息，也不作为联系人已经看见的历史。
10. 不修改目标电脑上安装的 AstrBot 核心源码。

## 3. 非目标

本次不做以下事项：

- 不使用模型判断“是否换话题”。任何同联系人新普通消息都触发旧回复淘汰。
- 不跨联系人合并消息或回复。
- 不撤回已经提交给微信的消息。
- 不把多条旧分段在 Bridge 中用超时猜测方式拼接。
- 不改变群聊入站过滤策略。
- 不改变红包/转账的接收事务、优先屏障或成功判定。
- 不改变收藏表情的固定语义库、冷却、幂等键或 WeFlow 回执要求。
- 不自动截断长回答；通过提示约束鼓励精简，但完整正文必须保持。
- 第一版不为命令、控制通知或混合媒体模型结果提供同联系人 epoch 淘汰；其发送合同保持现状。用户遇到的普通纯文本聊天回复属于本设计的完整保证范围。

## 4. 用户可见行为

### 4.1 合并输入

普通私聊文本继续按稳定 `sessionId` 隔离。

- 第一条消息到达后开启 1.5 秒静默窗口。
- 同一联系人继续发消息时重置静默窗口。
- 从第一条消息起最多等待 5 秒，达到上限后必须形成一轮。
- 合并内容保持原始到达顺序。
- 重放的相同 `rawid` 在现有去重层被丢弃，不重置窗口，也不触发新的淘汰。
- 图片、视频和表情沿用现有异步媒体处理与独立入站批次，不与尚未完成的文本批次做跨线程拼接。原始媒体事件一经接受仍可淘汰旧文本回复；无论媒体表示在静默窗口或最大窗口之前还是之后准备完成，它都形成独立输入事件，绝不追加入已经关闭或仍打开的文本批次。
- 达到静默窗口或最大窗口的第一个回调在目标级锁内关闭批次；关闭操作必须 compare-and-swap 批次 ID，因此另一回调和陈旧 timer 都是无操作。

新增配置：

- `buffer_quiet_seconds`，默认 `1.5`，有效范围 `0.2–10` 秒。
- `buffer_max_seconds`，默认 `5.0`，有效范围不得小于静默窗口，最大 `30` 秒。

现有 `buffer_seconds` 保留为兼容别名。首次升级时按以下优先级生成新字段：

1. 两个新字段都有效且 `max >= quiet`：直接使用，`buffer_seconds` 不参与运行时计算。
2. 只有 `buffer_quiet_seconds`：`buffer_max_seconds` 取不小于 quiet 的有效旧值，否则取 `max(5.0, buffer_quiet_seconds)`。
3. 只有 `buffer_max_seconds`：静默窗口取 `min(1.5, buffer_max_seconds)`。
4. 两个新字段都不存在：最大窗口取有效旧 `buffer_seconds`，否则取 `5.0`；静默窗口取 `min(1.5, buffer_max_seconds)`。

“有效”严格指非布尔、可转为有限数，quiet 位于 `0.2–10`，max 和旧字段位于 `0.2–30`。显式提供但无效的单个字段按“缺失”处理；两个新字段都有效但 `max < quiet` 时整对拒绝，优先保留上一个有效配置，没有上一个有效配置时使用 `1.5/5.0`，不得悄悄交换二者。

控制面板必须原子校验并同时保存两个新值，再同步把 `buffer_seconds` 写成 `buffer_max_seconds`，供旧版本回退读取。重复升级不得再次改变有效新字段。启动时对被拒绝的值记录安全警告。

把旧 `buffer_seconds=5` 解释为新的 5 秒最大窗口、并把静默窗口设为 1.5 秒，是本版本有意的行为变化：旧配置不再意味着“每条消息后都完整等待 5 秒”。

### 4.2 合并输出

仓库自带的新插件在 AstrBot 4.26.6 的 `on_decorating_result` 阶段接管合并文本；该阶段位于核心 `segmented_reply` 之前。插件完成一次自定义动作后清空结果并终止该事件的后续传播，因此核心分段和默认发送均看不到这段正文。全局 `platform_settings.segmented_reply` 保持原值，使插件不接管的其他平台和消息类型保持现状。

插件只处理满足以下全部条件的结果：

- 平台为当前 aiocqhttp/OneBot 实例；
- 原始事件包含有效 Akasha Bridge generation 和 epoch；
- 结果是模型结果；
- 非空结果链中的每个组件都是 `Plain`；
- 不是命令、管理事件或 Bridge 控制通知。

插件行为：

- 将本轮结果中的纯文本组成一个完整正文。
- 规范多余空白，但保留有意义的段落换行。
- 通过一次自定义 OneBot 动作提交完整正文。
- 结果一经认领，无论后续规范化、记忆绑定、动作构造、调用或受理结果如何，都必须由最终守卫清空默认正文并调用 `event.stop_event()`，防止后续装饰器、核心分段或默认发送重复发送。

正文规范化必须确定：先把 CRLF/CR 统一为 LF，删除每行末尾的空格/制表符，移除首尾空白行，并把三个及以上连续换行收敛为两个；不得折叠行内空格、制表符、全角字符或代码缩进。协议指纹、粘贴正文和草稿所有权比较都使用同一份规范化结果。

命令结果、管理消息、控制通知和混合媒体结果不进入该纯文本合并路径，继续使用各自现有发送合同；第一版不宣称这些结果可被新 epoch 淘汰。

插件通过 `on_llm_request` 只为上述 Akasha 普通私聊事件在 `ProviderRequest.system_prompt` 末尾追加一次带内部标记 `[AKASHA_MERGED_REPLY_V1]` 的以下指令；不得把它拼入用户正文，已存在该标记时不得重复追加：

> 请只回复当前最新一轮消息。默认用一条简洁自然的微信消息回答；删除不影响含义的总结、客套和追问，不要为了凑完整而追加低价值尾句。

该约束只影响表达风格，不允许截断已经生成的正文。

规范化后的正文必须满足：

- 至少包含一个非空白字符；
- 不得包含除换行和制表符以外的 C0 控制字符；
- 不超过 2,000 个 Unicode 码点且 UTF-8 编码不超过 16 KiB。

空白结果沿用 AstrBot 的空结果行为，不发消息。超限或含非法控制字符时返回确定性失败，不截断、不分段，也不回落到默认发送。

### 4.3 长审核时间

`uia_fixed_pre_paste_preview_delay` 和 `uia_fixed_pre_send_delay` 保持原值与现有允许范围。合并后的整轮回复只消耗一次审核时间。

例如 `uia_fixed_pre_send_delay=25` 时：

- 一轮回答只出现一次完整正文预览。
- 粘贴后仍按现有抖动规则等待约 23–25 秒。
- 新消息淘汰旧回复后，新回复仍获得完整的 23–25 秒审核，不走快速发送路径。

## 5. 回合身份

### 5.1 稳定目标键

逻辑淘汰不得使用联系人显示名称。稳定目标键为：

`(account, scope, sessionId)`

Bridge 可继续使用该身份映射得到的 OneBot `user_id` 作为进程内索引，但协议验证仍以持久身份绑定为准。

### 5.2 `reply_epoch`

Bridge 为每个稳定目标维护一个正整数 `reply_epoch`，并以现有 `lifecycle_generation` 隔离重启前后的状态。epoch 表最多保留 2,048 个目标；24 小时未活动且没有 job、幂等记录、outbox、预览、草稿隔离或未知提交的项才可 LRU 淘汰。表满且没有安全淘汰项时，不接纳新的目标、不推进该 WeFlow 消费检查点并把健康状态降级为 `E_EPOCH_CAPACITY`；不得超过硬上限或删除受保护目标。已有目标仍可递增其现有 epoch。

每一条通过以下边界的普通私聊入站消息都会递增该目标的 epoch：

1. 已通过 `rawid` 去重。
2. 已通过现有群聊、公众号/频道过滤。
3. 已先交给现有红包/转账接收服务，且未被该服务消费。
4. 已通过现有 `should_ignore()` 与自回复过滤。
5. `isSend` 未明确表示出站；明确的出站事件必须拒绝，字段缺失时继续使用现有自回复去重合同。
6. 拥有稳定的私聊 `sessionId` 和非空 `BOT_WXID`。
7. 已排除红包/转账回执等非普通对话记录。

实现应把这些判断收敛为一个明确的“接受普通私聊入站”边界。money service 的调用顺序不得改变。

rawid、epoch、spool 与源检查点必须作为一个可回滚的接纳事务：

1. 先做无副作用的 rawid 查重、过滤和稳定目标解析。
2. 在修改去重表或 epoch 前，预留 epoch 目标槽、pending WAL 字节/条目和批次状态空间；任何预留失败都返回背压，不能留下去重命中、epoch 递增或检查点前移。
3. 在同一持久事务中写入 rawid 去重记录、该目标的新 epoch、raw envelope/批次状态和 WeFlow 消费检查点，再提交预留。
4. 只有事务提交后才向旧 job 发出 supersession 信号并启动/重置 quiet/max timer。
5. 崩溃恢复要么看到上述四项全部存在，要么全部不存在；后一种情况允许 WeFlow 重放并重新接纳该 rawid。

Bridge 推送给 AstrBot 的合并入站事件包含：

- `akasha_reply_epoch`
- `akasha_bridge_generation`
- `akasha_plugin_instance_id`
- `akasha_admission_token`
- 现有 `account`
- 现有 `session`
- 现有 `routing_name`
- 现有 `source_messages`

同一输入批次最终事件使用批次关闭时的最新 epoch。

## 6. 仓库自带合并回复插件

新增独立插件 `astrbot_plugin_akasha_merged_reply`，避免把发送调度职责混入联系人记忆插件。

AstrBot 4.26.6 的 handler registry 按 `priority` 从高到低执行，且 `on_decorating_result` 位于核心分段逻辑之前。插件采用以下固定注册顺序：

- 结果通知消费：合并插件 `event_message_type(PRIVATE_MESSAGE, priority=40_000)` 只认 `akasha_send_result` v2；识别后立即认领，并在 `finally` 中无条件停止该控制事件。它调用联系人记忆 result-consumer 或 no-memory 适配器，只有持久应用成功后才 ACK；当前 money `30_000` 和普通私聊 handler 永远不会看到它。
- admission 跟踪：合并插件 `event_message_type(PRIVATE_MESSAGE, priority=5_000)` 为带有效 admission 的普通事件建立独立 lifecycle tracker，先于默认优先级命令/模型处理运行。
- 提示与身份准备：`on_llm_request(priority=-30_000)`，在当前联系人记忆的默认优先级 handler 之后执行；它一次性追加提示、生成 UUIDv4 `request_id` 并写入事件私有状态。
- 结果认领与规范化：合并插件 `on_decorating_result(priority=-20_000)`。确认是符合第 4.2 节的结果后，第一步就设置 `akasha_merged_claimed=true`，再在 `try/except` 中规范化正文；任何内部异常只记录失败，不撤销认领。
- 联系人记忆绑定：联系人记忆插件 `on_decorating_result(priority=-25_000)`。它在发送动作之前，把 `request_id`、contact key、generation、epoch、规范化正文和云端待确认输出原子持久化，并设置 `akasha_memory_bind_status=bound`；联系人记忆明确关闭时记录 `not_applicable`，其他失败均为 fail-closed。
- 动作与最终守卫：合并插件 `on_decorating_result(priority=-30_000)`。它只在规范化和记忆绑定成功时调用动作；无论调用返回、超时或抛错，只要事件已认领，`finally` 都清空结果并 `stop_event()`。该守卫还必须独立识别“带有效 Akasha generation/epoch 的纯文本模型结果”，因此即使前一个同插件 handler 在设置标记前异常，也不能落入核心分段。

Akasha 自有的 AstrBot 实例必须保持 `provider_settings.streaming_response=false`。流式 chunk 无法满足“一轮只调用一次动作”，因此流式配置开启时插件不得发布 readiness，Bridge 也不得转发普通聊天事件。

插件职责：

1. 在普通私聊事件开始时读取并严格校验 Bridge 传入的 generation 和 epoch。
2. 将它们保存在事件私有状态中。
3. 对纯文本模型结果执行正文合并和空白规范化。
4. 使用 `on_llm_request` 生成的小写规范格式 UUIDv4 `request_id`；同一次动作重放必须复用，不能重新生成。
5. 调用自定义 OneBot 动作 `send_akasha_merged_reply`，参数严格为：
   - `user_id`
   - `bridge_generation`
   - `reply_epoch`
   - `request_id`
   - `text`
   - `plugin_instance_id`
   - `lease_id`
   - `admission_token`
6. 自定义动作只等待 Bridge 完成校验、持久化和入队，不等待全局 UIA FIFO 或长审核结束。
7. 结果一经认领就始终清除默认正文并停止该事件；任何失败都禁止自动回落到标准 `send_private_msg`。
8. 传输层在尚未得到受理结果时可用同一 request ID、相同业务指纹和当前 lease 做幂等重放。三次即时尝试仍未知时，联系人记忆绑定持久化为 `acceptance_unknown`，后台以最多每 30 秒一次的速率在 readiness 恢复后继续查询/重放同一动作；generation 失效或收到任一终态即停止。该过程只补全“是否受理”，不能创建第二个 job；一旦收到 `commit_unknown` 等终态，不得自动重试发送。
9. 对协议错误和真实发送故障保留失败状态，不伪装成淘汰，并通过插件日志或明确错误结果呈现。
10. 不依赖 `after_message_sent` 表示送达；被停止事件的请求清理和送达确认由 request-targeted v2 结果通知完成。
11. 对未被合并插件认领的命令或混合媒体，结果装饰 handler 必须在核心响应阶段前幂等释放 admission；空结果由事件生命周期清理 hook 释放。释放确认前不得进入标准纯文本发送。进程崩溃遗留 token 由下一 generation 恢复事务清理。

若在 Bridge 创建 job 之前失败或 admission 已被新 epoch 淘汰，最终守卫必须调用联系人记忆的 `record_pre_action_terminal(request_id, outcome, reason)`，原子写入幂等本地终态（`failed` 或 `superseded`，`memory_applied=true`）、删除该待确认输出并把已经发生云端分歧的 session 标记为需要重建；随后用同一 request/reason 补全 admission 释放。若本地持久化本身不可用，则健康降级且禁止继续普通入站。联系人记忆启动时还要扫描没有 `accepted/terminal/pre_action_terminal` 的遗留绑定并执行同样清理，覆盖进程在两个 handler 之间崩溃的情况。

lifecycle tracker 独立于结果 hook，专门覆盖“没有结果”的终态：

1. tracker 以 admission token 为键，所有 `on_llm_response`、`on_agent_done`、结果装饰、`after_message_sent` 和插件卸载路径都在 `finally` 中调用同一个幂等 finalizer。
2. 对 Provider 抛错、任务取消或管线在结果装饰前退出，tracker 每 0.5 秒只读检查固定 AstrBot 4.26.6 的 `active_event_registry`；scheduler 在自身 `finally` 注销事件后，tracker 立即 finalizer。
3. finalizer 先查询 admission 是否已转换为 job/释放；未转换时调用 `finish_akasha_reply_admission`，根据 Bridge 返回的当前 epoch 选择本地 `failed/E_PROVIDER_NO_RESULT` 或 `superseded`。已经生成 request ID 时再调用 `record_pre_action_terminal`；尚未进入 LLM 请求、没有 request ID 或云端待确认输出时，只释放 admission，记忆应用是幂等空操作。
4. 单个事件最长占用 admission 10 分钟；达到硬截止时 tracker 请求停止 Agent、形成 `failed/E_PROVIDER_TIMEOUT` 并释放预留。Provider 稍后才返回的正文仍被 tombstone 和最终守卫拒绝。
5. 若只读 registry 适配器、tracker supervisor 或 finalizer 后台任务不可用，插件不得续租 readiness。AstrBot 进程崩溃则由连接断开和下一 generation 恢复事务终结 token。

readiness 不注入任何伪私聊或探针事件，而使用插件后台发起的连接级租约：

1. 每次 AstrBot 反向 WebSocket 建立、断开或 Bridge generation 改变时，Bridge 都把该连接标为 `merged_reply_ready=false`。
2. 插件加载后生成固定到本次进程生命周期的随机 `plugin_instance_id`，并立即通过当前反向 WebSocket 调用 `register_akasha_merged_reply`，提交协议、动作、记忆 schema 和 `streaming_response=false`。Bridge 首次返回随机 `lease_id`、当前 generation 和 15 秒到期时间。
3. 此后插件每 5 秒携带同一 plugin instance 和 lease ID 续租；Bridge 只对同一活跃连接、同一 generation、同一实例和完全匹配版本延长该 lease，不在正常续租时轮换 ID。只有断线、generation 或 plugin instance 改变才签发新 lease。
4. 15 秒未续租立即清除 readiness。入站事件只绑定签发时的 `plugin_instance_id`、generation 和独立 admission token，不保存易过期 lease；结果动作在调用瞬间从插件并发安全的租约状态读取当前 lease ID。续租与动作竞态导致一次过期拒绝时，只能在续租完成后用同一 request/admission 做幂等重放。
5. readiness 建立前，Bridge 不向 AstrBot 转发普通聊天事件，报告 `E_MERGED_REPLY_NOT_READY` 并保持服务健康失败；插件缺失时不会产生任何伪消息、模型调用、联系人记忆或默认回复。
6. `merged_reply_ready` 健康项同时验证连接租约、插件协议、动作版本、记忆结果 schema、lifecycle tracker/registry 适配器和流式关闭。

Bridge 另维护 `ordinary_ingress_open`。普通事件只有在 capability readiness 和 ingress 都为 true 时才转发；容量背压只关闭 ingress，不能关闭结果通知/ACK 所需的插件连接租约。

每个转发给 AstrBot 的普通批次必须先获得持久 `admission_token`：

- Bridge 最多允许 32 个“已转发但尚未转换为 job/释放”的 generation admission；更多批次留在 pending spool。
- 创建 token 时就原子预留一个幂等/job 槽和一个结果 outbox 槽，并绑定目标、generation、epoch 和 plugin instance；因此动作到达时不会因其他请求抢占容量而无法形成终态。
- 每个目标最多一个未消费 admission；新 epoch 在接纳事务后把该目标更旧 admission 原子改为 `superseded_before_action` tombstone 并释放预留。旧模型稍后调用时得到确定性 superseded，不能触碰 UIA。
- `send_akasha_merged_reply` 原子消费 token 并把预留转换为 `AcceptedSend`。插件确定结果属于命令、控制或混合媒体而不接管时，必须先调用 `release_akasha_reply_admission`，确认释放后才允许默认发送。
- 认领后、创建 job 前的失败或 superseded 使用 `finish_akasha_reply_admission` 释放 Bridge 预留，并与本地 `record_pre_action_terminal` 使用同一 request/outcome/reason；调用暂时未知时后台按幂等键补全。
- 只要某目标存在未消费 admission，Bridge 就拒绝该目标无 token 的标准纯文本 `send_private_msg`，返回 `E_MERGED_REPLY_ADMISSION_REQUIRED`。这样插件在事件转发后崩溃也不会 fail-open 到默认发送。
- admission 在当前 generation 内不按墙钟超时；成功消费/释放或 generation 恢复事务才能清除。每个 token 的状态都计入硬上限和健康检查。

capability 未就绪或 `ordinary_ingress_open=false` 期间，通过普通入站边界的真实消息仍立即递增 epoch 并参与正常 quiet/max 合批。每个已接受 raw envelope 必须先写 pending WAL 才能推进相应 WeFlow 消费检查点；quiet/max 关闭标记也原子写入同一状态目录，不交给 AstrBot。重启后恢复未关闭批次，并用原始到达时间执行已经到期的窗口。两项条件恢复时按到达顺序扫描，但同目标只转发 `reply_epoch` 仍等于当前值的最新批次，较旧批次记为 `superseded_before_generation`。spool 上限为 512 个批次、2,048 个 raw envelope 或 10 MiB；达到任一上限时停止推进检查点、健康降级并等待恢复，不能静默丢弃。spool 和检查点跨重启保留。

插件依赖 AstrBot 4.26.6 已验证的事件/结果接口、只读 `active_event_registry` 适配器和 aiocqhttp `bot.call_action()` 能力，不修改安装环境中的 AstrBot 核心文件。固定版本的 aiocqhttp 底层超时虽为 180 秒，但 `send_akasha_merged_reply` 必须在 5 秒内返回持久化后的 `accepted` 或立即终态；全局 FIFO 排队和 10/60 秒审核全部异步执行，不占用该调用预算。

## 7. Bridge 自定义发送动作

`send_akasha_merged_reply` 是普通私聊文本的唯一合并发送入口。

处理顺序：

1. 严格校验参数集合、类型、正整数范围、UUID 格式、plugin/lease/admission 格式、正文内容与大小。
2. 基于 `user_id`、generation、epoch 和规范化正文 SHA-256 形成请求指纹；plugin instance、lease 和 admission 是传输/容量能力，不属于业务指纹。
3. 在任何当前 epoch 判断之前查询有界幂等缓存：
   - 相同 `request_id` 且指纹一致：返回当前 `accepted` 或最初缓存的终态；
   - 相同 `request_id` 但指纹不同：返回 `E_OB_IDEMPOTENCY_CONFLICT`，不得触碰 UIA。
4. 对首次请求核对当前连接、plugin instance、`lease_id`、`admission_token` 和 `bridge_generation`，再使用 `user_id` 解析持久私聊路由并核对 `account/session` 身份绑定。
5. 核对目标当前 `reply_epoch` 和 admission tombstone；已经过期时返回 `pre_action_superseded`，由第 6 节持久 `PreActionTerminal`，不创建 job 或 v2 outbox。
6. 在一个原子事务中消费 admission，把它预留的容量转换为 `AcceptedSend` 幂等记录、持久 UIA job 和结果 outbox 预留，然后才把 job 放入内存队列。
7. 在 5 秒动作预算内返回 `accepted=true`、request ID 和指纹；此时只代表 Bridge 已可靠受理，不代表已发送。
8. 异步 worker 取得 job 后，在触碰 UIA 前和原子提交边界前再次核对 generation/epoch，并执行第 8 节状态机。
9. worker 在一个原子事务中把 job 和幂等记录变为终态、写入第 11 节结果 outbox，然后才发布内存通知。崩溃恢复时以持久 job/commit intent 为准，不从动作响应推测结果。

幂等/作业/admission 存储硬上限为 2,048 条，24 小时 TTL 只适用于已终态、结果已 ACK、无预览、无隔离且无未知提交的记录。达到 2,016 条高水位或没有可安全淘汰项时，`ordinary_ingress_open=false` 并停止签发新 admission；插件连接租约仍保持，以便结果 outbox 继续排空。已经签发的最多 32 个 admission 都有预留容量，动作到达时不再竞争空槽。硬上限到达时返回 `E_OB_STATE_CAPACITY`，不得越界、淘汰受保护记录或触碰 UIA。

结果分类：

| 状态/结果 | 含义 | OneBot 动作或 v2 通知 |
| --- | --- | --- |
| `pre_action_terminal` | job 创建前已失败或被淘汰 | 动作返回本地终态所需 outcome/reason；不产生 v2 |
| `accepted` | 已持久化并入队，尚无发送终态 | 动作成功，`accepted=true` |
| `sent` | OS 提交动作已确定成功执行 | v2 终态，`committed=true` |
| `superseded` | 被同联系人新入站淘汰，未提交 | v2 中性终态，`superseded=true`、`committed=false` |
| `manual_cancel` | 操作员在提交前先取消当前预览 | v2 中性终态，`manual_cancel=true`、`committed=false` |
| `failed` | 路由、UIA、协议或系统故障 | v2 失败终态，带安全错误码和阶段 |
| `commit_unknown` | 可能已经提交但无法确认 | v2 未知终态且禁止自动重试 |

只有成功创建 `AcceptedSend` job 后的结果才进入 v2 outbox。job 创建前的 superseded、校验/路由失败走持久本地 `PreActionTerminal` 并释放 admission；已经释放的 outbox 预留不得被重新要求。动作响应和通知都不得把 `superseded`、`manual_cancel` 或 `accepted` 解释为已送达。

## 8. UIA 发送状态机

物理 UIA 仍使用现有全局锁，不改为联系人级并行锁。

逻辑状态：

```text
QUEUED
  -> PREVIEW_BEFORE_PASTE
  -> UI_SELECTED
  -> PASTED_OWNED
  -> COMMITTING
  -> COMMITTED
  -> DONE

QUEUED / PREVIEW_BEFORE_PASTE / UI_SELECTED / PASTED_OWNED
  -> CANCEL_REQUESTED
  -> CLEANUP
  -> SUPERSEDED / MANUAL_CANCELED / FAILED_QUARANTINED
```

规则：

- 新 epoch 只影响同目标较旧 epoch：尚未由 worker 取得的持久 `QUEUED` job 可在状态事务中直接终态化为 `superseded` 并写 outbox；已经进入 sender 的 job 只设置取消信号。
- SSE 线程只设置取消信号，不直接操作微信 UI。
- sender 线程在现有条件等待轮询中观察取消信号。
- `COMMITTING` 由原子 `try_commit_send()` 建立，是取消权限关闭点，但不是“已发送成功”。
- 进入 `COMMITTING` 后必须尝试一次 OS 提交动作，不能再报告手工取消或自动淘汰。
- OS 提交动作确定成功才进入 `COMMITTED` 并返回 `sent`。
- 前台窗口在注入提交输入前丢失、提交调用明确失败或生命周期在 `COMMITTING` 前停止时，OS 动作确定未发生，返回带阶段的 `failed`（生命周期使用 `E_UIA_STOPPED`）。
- 已注入点击/按键但无法确认处理结果、连接在提交响应前丢失或进程在该窗口崩溃时，返回粘滞的 `commit_unknown`。
- 已确定提交的消息完成一次，之后所有同目标旧 epoch 工作仍然被丢弃。
- 手工“取消此条”仍只取消当前 `preview_id`，与整轮自动淘汰保持不同语义。
- 取消信号原子保存 `manual_cancel` 或 `superseded` 原因；第一位成功设置者获胜，后到原因不得改写终态。
- 进入 `COMMITTING` 后到达的手工取消、自动淘汰或 Stop 都不能把结果改写为未提交，只能等待 `sent`、明确 `failed` 或 `commit_unknown`。

sender 必须在正文粘贴前持久化 draft intent，在 `try_commit_send()` 成功时先持久化 commit intent，再执行对应 UI 副作用。Stop→Start 合同如下：

| Stop 时的持久状态 | 必须行为 |
| --- | --- |
| `QUEUED/PREVIEW_BEFORE_PASTE/UI_SELECTED` | 禁止后续 UI 操作，形成 `failed/E_UIA_STOPPED` 和 v2 outbox |
| `PASTED_OWNED` | Stop 等待 sender 按第 9 节校验并清稿；成功后 `failed/E_UIA_STOPPED`，所有权丢失则 target quarantine + `failed` |
| `COMMITTING` | 正常 Stop 等待一次 OS 提交尝试和终态落盘；强制崩溃/超时退出后，新 generation 从 commit intent 恢复为 `commit_unknown` |
| `COMMITTED` | 保持 `sent`，不得因 Stop 改写 |

新 generation 在发布 readiness 前必须扫描所有旧 job 并按上表原子终态化：遗留 draft intent 进入 target quarantine 并形成 `failed/E_UIA_DRAFT_RECOVERY_REQUIRED`，遗留 commit intent 形成 `commit_unknown`。它不恢复执行任何旧 UIA job，只恢复隔离、终态和 outbox 重放。Stop 脚本在 `PASTED_OWNED/COMMITTING` 存在时必须给 sender 有界收尾时间；强制结束后不得自动清稿或重发。

当前预览状态增加：

- `target_id`
- `bridge_generation`
- `reply_epoch`
- `request_id`
- `stage`

这些字段只在本机控制面板使用，不显示稳定 session 或其他敏感身份。

结果优先级：

| 已观察状态 | 最终结果 |
| --- | --- |
| 命中相同幂等请求终态 | 返回缓存原结果 |
| OS 提交可能已发生 | `commit_unknown` |
| OS 提交确定成功 | `sent` |
| 已进入 `COMMITTING` 但提交确定失败 | `failed` |
| 提交前 epoch 过期且尚无取消原因 | `superseded` |
| 提交前操作员先取消 | `manual_cancel` |
| 路由、配置或 UIA 前置失败 | `failed` |

## 9. 已粘贴正文的安全清理

现有取消路径会重新选择联系人并执行全选删除。该行为无法证明输入框中仍然只有机器人正文，可能误删用户临时编辑。

新路径采用正文所有权校验：

1. 重新验证并选择精确目标联系人。
2. 聚焦消息输入框。
3. 优先用输入控件的 UIA `ValuePattern`/`TextPattern` 读回正文；仅在该控件不提供可用值时使用受所有权保护的剪贴板读回。
4. 剪贴板读回必须先无损快照当前所有格式和 `GetClipboardSequenceNumber()`，再执行全选、复制并记录机器人复制后的 sequence/content。若原剪贴板无法无损快照，则不执行复制或删除，直接进入隔离。
5. 将读取结果与本次 `request_id` 对应的预期正文做规范化后精确比较。
6. 在删除前再次核对 WeChat 前台窗口、输入控件焦点、request/epoch、剪贴板 sequence 和外部输入变化。只有正文完全一致且所有所有权信号未改变，才对仍保持的选区立即执行一次删除。
7. 使用剪贴板时，只有 sequence/content 仍等于机器人刚写入的值才恢复原始多格式快照；若用户或其他程序已经更新剪贴板，绝不覆盖新值，并把本次清理视为所有权丢失。
8. 若无法读取、内容不一致、焦点变化、检测到用户编辑或剪贴板竞争：
   - 不执行删除；
   - 退出选区；
   - 返回 `E_UIA_DRAFT_OWNERSHIP_LOST`；
   - 在控制面板显示需要人工检查；
   - 将该目标写入 `TARGET_DRAFT_QUARANTINED`，拒绝其后续合并文本发送。

`TARGET_DRAFT_QUARANTINED` 是目标级状态，不设置现有全局 `paused`：

- sender 完成清理尝试后必须先释放全局 UIA 锁，绝不在锁内等待人工操作。
- 其他联系人和 money priority 通道继续运行。
- 隔离记录以目标 HMAC/OneBot ID、request ID、原因和时间原子写入 Bridge 状态目录；重启后仍保持。
- 控制面板显示“我已人工检查草稿，恢复此联系人”按钮。
- `POST /resolve-draft-quarantine` 只接受 loopback 请求、正整数 `target_id` 和当前隔离 `request_id`。
- 操作员确认后仅清除隔离；端点不自动删除或发送微信草稿。
- 旧按钮、错误 request ID 或未隔离目标返回无变化。

图片粘贴没有可靠正文读回，因此图片沿用 v0.4.3 现有图片发送与粘贴后等待合同；本设计不为图片增加合并、自动清稿或 target quarantine 行为。

## 10. 生成阶段淘汰

Bridge epoch 是正确性的最终边界：即使旧模型调用无法及时取消，其结果也会在触碰 UIA 前被拒绝。

为改善速度，联系人记忆运行时增加最佳努力的旧请求取消：

- 新合并私聊事件推送到 AstrBot 时，通知该联系人的旧 Qwen 响应任务停止。
- 取消只作用于更旧 epoch，不作用于新请求。
- `CancelledError` 必须沿用现有脏会话和锁释放清理。
- 若 Provider 不支持及时取消，允许旧调用自然结束，但其输出仍会因 epoch 过期而被丢弃。

正确性不得依赖 Provider 是否支持取消。

## 11. 联系人记忆一致性

生成但未送达的旧回答不能留在云端会话中假装联系人已经看见。

`akasha_send_result` 升级为 schema v2，并增加必填 `outcome`：

- `sent`
- `superseded`
- `failed`
- `commit_unknown`
- `manual_cancel`

v2 处理器必须先按 `outcome` 分派；不得先读取旧 `success` 布尔值。旧字段只为旧接收器保留：

- `sent` 可带 `success=true`；
- `failed` 可带 `success=false`；
- `superseded`、`commit_unknown` 和 `manual_cancel` 不携带 `success`。

通知携带 `notice_id`、`result_revision`、`result_digest`、`request_id`、generation、epoch 和已提交正文列表。发送动作只有在第 6 节的 request-to-pending-output 绑定已持久化后才可调用；v2 处理器找不到完全匹配的绑定时不得猜联系人或 ACK。

终态通知使用持久 outbox 和幂等 ACK：

1. Bridge 在同一原子事务中写入 job/幂等终态与 outbox 条目，再向调用方或控制面板暴露终态。
2. `notice_id` 对一个 request/revision 唯一，`result_digest` 覆盖 schema、request ID、revision、目标、generation、epoch、outcome、stage 和正文指纹。
3. Bridge 在有效插件能力租约的连接上发送 outbox；即使普通入站因容量被关闭，结果通道仍保持。未 ACK 条目在重连、Stop→Start 和定时退避后重放。旧 generation 的通知可通过新连接重放，但只能做记忆对账，绝不能重新进入 UIA。
4. 联系人记忆在一个本地事务中核对持久绑定、按 `(notice_id, result_digest)` 幂等应用结果并记录 `result_applied`，然后调用 `ack_akasha_send_result`。联系人记忆明确关闭且绑定为 `not_applicable` 时，由合并插件的 no-memory 适配器持久化 receipt tombstone 后 ACK，不能让 outbox 永久悬挂。
5. Bridge 只接受与持久 outbox 完全匹配的 ACK。ACK 响应丢失时，重复通知只触发重复 ACK，不重复修改记忆。
6. 同一 `notice_id` 的 digest 冲突、缺少绑定或 schema 不支持时发送明确 NACK、健康降级并保留 outbox，等待修复；不得静默确认。
7. outbox 的“未 ACK 条目 + admission/job 预留槽”硬上限为 4,096 条或 64 MiB，达到 4,000 条或 60 MiB 高水位时只关闭 `ordinary_ingress_open` 并对新普通入站实施安全背压，插件能力租约和 ACK 通道继续工作。每个 job 形成终态时原子消费自己的预留槽，不能临时扩容。未 ACK 条目不得因 TTL 或容量删除；已 ACK 条目保留 24 小时 tombstone 后才可压缩。

被合并插件认领的事件会在结果装饰阶段停止，因而不依赖 AstrBot 的 `after_message_sent`；联系人记忆的请求清理和送达真相都以持久绑定与 v2 通知为准。

联系人记忆按 `request_id` 和 epoch 处理通知：

- `sent`：确认完整合并正文一次。
- `superseded` 且无已提交内容：删除对应待确认输出，并将云端会话标记为需要从已确认历史重建。
- 提交边界与新入站发生竞态：保留已经提交的完整正文一次；后续旧工作丢弃。
- `manual_cancel`：删除该请求对应的待确认输出；若云端已包含该回答则标记该联系人会话重建。
- `failed`：沿用真实失败处理，但只更新通知指定请求；需要全联系人重建时必须由明确的云端会话分歧决定。
- `commit_unknown`：保留未知状态，不自动重试，等待 WeFlow 历史对账。

`commit_unknown` 只允许单向解析为已确认 `sent` 或已确认未发送的 `failed`，并产生 `result_revision+1` 的新 v2 outbox 条目。自动解析 `sent` 需要同 session、`isSend=1`、文本指纹相同、落在记录的提交尝试时间窗内且候选唯一的 WeFlow 出站记录；不存在记录不能证明未发送。歧义时由 loopback-only `POST /resolve-commit-unknown` 要求操作员提交 request ID、当前 revision 和 `sent|not_sent`，端点不执行重发。解析完成前记录受保护并计入容量。

下一轮重建只使用：

- 已确认的 WeFlow 入站；
- 已确认的机器人出站；
- 当前合并输入批次。

不把被淘汰正文加入可见对话历史。

## 12. 专用通道兼容性

### 12.1 其他联系人

联系人 B 的新入站不得淘汰联系人 A。全局 UIA 锁继续串行化对同一个微信窗口的物理操作，淘汰只是从逻辑上让过期任务快速退出。

### 12.2 红包和转账

被 money service 消费的 SSE 事件：

- 不递增普通回复 epoch；
- 不取消当前普通回复；
- 保持“当前已进入 UI 的普通发送完成后，收款优先于所有尚未开始普通发送”的现有合同。

### 12.3 收藏表情

收藏表情动作继续使用自身的 request ID、冷却和 WeFlow 回执。第一版合并回复不改变收藏表情动作参数，也不在固定槽位点击后报告淘汰。

模型最终纯文本回复仍受合并回复 epoch 保护。

## 13. 控制面板和日志

控制面板继续显示一条当前预览，新增可读状态：

- “合并回复：等待粘贴”
- “合并回复：已粘贴，等待发送”
- “旧回复已被新消息替换”
- “草稿内容发生变化，已暂停自动清理”

日志使用结构化、隐私安全字段：

- target HMAC 或 OneBot ID
- generation
- epoch
- request ID
- stage
- outcome

日志不得写稳定 session、访问令牌、lease/admission capability、完整配置或未脱敏媒体路径。现有 CHAT 审计记录继续承担正文记录职责。

## 14. 配置和升级

安装/升级脚本必须：

1. 安装并启用合并回复插件。
2. 保持全局 AstrBot `segmented_reply` 原值；合并插件在核心分段前只接管自己认领的 Akasha 纯文本模型结果。
3. 在只含受管 `akasha_ob11` 平台的 Akasha 专用 AstrBot 实例中，把 `provider_settings.streaming_response` 设置为 `false`，并纳入 readiness 健康检查。
4. 保留未知 AstrBot 和 Bridge 配置字段。
5. 保留用户自定义的长预览时间。
6. 添加输入合并静默窗口和最大窗口字段。
7. 不把旧的预览时间强制迁移为更短值。
8. 先备份待变更配置，再原子写入。
9. 重复运行得到相同配置。

控制面板提供两个输入合并设置，但不增加“话题识别”开关。

`streaming_response` 是实例级配置。若升级检查发现同一 AstrBot 数据目录存在任何其他已启用平台，初始化器必须保留原配置并以 `E_ASTRBOT_SHARED_INSTANCE` 停止，要求迁移到专用实例；不得为了 Akasha 静默改变其他平台的流式行为。

预览时间迁移规则：

- 新安装继续使用发行版默认值，用户可在控制面板延长。
- 升级时读取并原样保留当前安装值，包括直接遇到的旧 `10` 秒和任意合法自定义值。
- 已经被旧版本迁移为 `5` 秒的安装无法可靠推断此前是否为 `10` 秒，因此保持 `5`，不得猜测恢复。
- 本版本移除旧初始化器“精确 `10` 自动改为 `5`”的规则及对应旧断言，替换为上述三类升级测试。

输入缓冲迁移遵守第 4.1 节的字段优先级。旧字段暂不删除，以支持版本回退。

## 15. 测试合同

### 15.1 P0

1. 三条同联系人消息在窗口内到达：按顺序形成一个输入事件。
2. 持续消息超过最大窗口：第一批在 5 秒上限形成，后续进入下一批。
3. 一轮超过 25 字并含标点/换行的纯文本结果：一个预览、一次粘贴、一次提交。
4. 即使全局 `segmented_reply` 保持开启，已认领正文也在核心分段前被清空并停止传播，不发生默认重复发送。
5. 新消息在 `PREVIEW_BEFORE_PASTE` 到达：旧正文不触碰微信。
6. 新消息在 `PASTED_OWNED` 到达且正文仍匹配：清理旧草稿，不提交。
7. 新消息在 `PASTED_OWNED` 到达但正文不匹配：不删除，报告所有权丢失并隔离该联系人。
8. 新消息与 `COMMITTING` 竞态：已提交正文最多一次，后续旧工作全部淘汰。
9. 旧模型结果在新 epoch 后才返回：自定义动作在 UIA 前返回 `superseded`。
10. 同一 `request_id` 重放：不会重复提交。
11. 已发送请求在 epoch 前进后重放：返回最初 `sent`，不改写为 `superseded`。
12. 相同 UUID 携带不同正文、目标、generation 或 epoch：返回幂等冲突，不触碰 UIA。
13. 自定义动作失败、超时或 `commit_unknown`：默认发送均被抑制。
14. `commit_unknown` 在产生更高 reconciliation revision 前重放：返回原未知终态，禁止重试。
15. 长审核配置为 25 秒：新回复仍获得完整的约 23–25 秒审核。
16. 联系人 B 新消息不淘汰联系人 A。
17. 被淘汰回复不被联系人记忆确认为已送达。
18. v2 `superseded` 通知不进入旧 `success=true/false` 分支，只清理指定 request。
19. 草稿隔离后全局 UIA 锁已释放，联系人 B 和 money priority 继续运行。
20. 正确 target/request ID 的人工确认解除隔离；错误或陈旧请求无效。
21. 插件缺失、协议/动作/schema 不匹配、流式开启、lease/generation/connection 不匹配时 readiness 为 false，Bridge 不转发普通聊天事件；整个过程没有伪私聊、模型调用或记忆副作用。
22. 联系人记忆绑定在 `call_action()` 前持久化；即使 v2 通知先于动作响应到达，也能按 request ID 精确应用。
23. Bridge 在“终态落盘后、通知前”和“通知后、ACK 前”分别断线：重连后 outbox 重放，记忆只应用一次并最终 ACK。
24. 联系人 B 已在 60 秒审核、前面还有 money 工作时，联系人 A 的动作仍在 5 秒内返回 `accepted`，不会受全局 FIFO 时长影响。
25. 规范化、记忆绑定、参数构造和 `call_action()` 各阶段故障注入：已认领正文始终被清空并停止传播。
26. v2 result-consumer 应用失败或发送 NACK 时控制事件仍被停止，不触发 money、普通消息链或模型调用；修复后重放再 ACK。
27. Bridge 动作前失败或进程在结果 handler 之间崩溃：遗留 request 只清理自己的待确认输出并把分歧 session 标脏，不产生默认发送。
28. 模型生成 20 秒且期间多次续租：事件使用固定 plugin instance，动作读取当前 lease 后正常受理；动作与续租并发的一次过期只幂等重放，不重复 job。
29. epoch/spool 容量预留失败后重放同一 rawid：第一次没有遗留 dedupe、epoch 或检查点状态，恢复容量后恰好接纳一次。
30. Provider 抛错、生成取消、空结果和结果装饰前终止：lifecycle tracker 都形成一次本地 pre-action 终态并释放 admission；32 次此类事件不会永久关闭 ingress。

### 15.2 P1

1. 重复 `rawid` 不递增 epoch。
2. 暂停期间发生淘汰：暂停保持，恢复后只显示有效工作。
3. Stop→Start 后旧 generation 的动作无法取消或提交新 generation 工作。
4. money priority 屏障顺序不变。
5. 收藏表情幂等、冷却和回执测试不变。
6. 路由失败与 `superseded` 可区分。
7. Provider 取消失败时，旧结果仍在 Bridge 被拒绝。
8. 空白规范化不吞字、不重复正文、不破坏有意义段落。
9. 空白、控制字符、2,000 码点和 16 KiB 边界均为确定结果；超限不截断、不拆分、不回落。
10. 手工取消与自动淘汰竞态遵守第一原因获胜。
11. UIA 读回不可用、剪贴板快照不可还原、复制读取失败、用户编辑/焦点/sequence 竞态均不删除草稿；无竞争时原始多格式剪贴板被恢复。
12. 目标隔离跨 Bridge 重启保持，解除后有界清理。
13. 标准文字、异步图片、视频和表情各自保持已定义批次边界；媒体表示在窗口前后完成都不会追加入文本批次。
14. 静默窗口与最大窗口的精确边界、陈旧 timer callback 均不重复推送。
15. 缺失 `BOT_WXID`、缺失 session 和明确 `isSend=1` 不递增普通回复 epoch。
16. 插件或自定义动作不可用时健康检查失败，模型正文失败关闭；5 秒异步受理预算、同 UUID 传输重放和无默认回退有固定版本集成测试。
17. 配置升级覆盖仅旧字段、仅 quiet、仅 max、混合有效/无效字段和重复运行。
18. 旧 `10`、已迁移 `5` 和自定义 `25` 预览值分别按迁移合同处理。
19. epoch、job/幂等缓存、pending spool、结果 outbox 和已解除隔离状态均执行有界清理；高水位触发背压，硬上限不越界且不淘汰受保护记录。
20. 控制面板不暴露稳定 session 或敏感配置。
21. 固定 AstrBot 4.26.6 中，`on_llm_request(-30_000)` 晚于联系人记忆，结果 handler 严格按 `-20_000/-25_000/-30_000` 执行且都早于核心分段；认领后最终 `stop_event()` 阻止响应阶段。
22. 命令、控制通知和混合媒体结果不被合并插件认领，并明确维持第一版不提供 epoch 淘汰的现状。
23. Stop 在 `QUEUED`、`PASTED_OWNED`、`COMMITTING` 和 `COMMITTED` 各阶段遵守持久恢复表；强制退出后的 draft quarantine/`commit_unknown` 在新 generation 通过 outbox 重放。
24. `[AKASHA_MERGED_REPLY_V1]` 只追加到匹配事件的 `ProviderRequest.system_prompt` 一次，不污染用户正文或其他平台请求。
25. readiness 缺失期间关闭的批次跨重启保留；恢复时只转发每目标当前 epoch 批次，较旧批次安全淘汰，spool 满时不推进源检查点。
26. 共享 AstrBot 实例保留原 streaming 配置并报 `E_ASTRBOT_SHARED_INSTANCE`；专用实例才执行非流式迁移。
27. `commit_unknown` 只有唯一正向 WeFlow 证据或带 revision 的人工确认才能解析；缺少记录不自动判失败、不重试。
28. 33 个并发生成批次到达时只转发 32 个持久 admission，其余留在 spool；每个已转发请求都能转换为 job 或本地失败终态，不会因容量缺口悬挂。
29. 活跃 admission 存在时插件崩溃：该目标标准纯文本 `send_private_msg` 被 Bridge 拒绝；恢复后消费或释放 token 才重新开放。
30. 新 epoch 淘汰尚未成 job 的 admission：释放其 job/outbox 预留，旧动作只形成持久本地 `pre_action_terminal`，不得再要求 v2 outbox 槽。

## 16. 验收标准

功能完成必须满足：

- 全部现有测试通过。
- 新增合并输入、合并输出、epoch 淘汰、草稿所有权、记忆一致性和专用通道回归测试通过。
- `tests/Run-All.ps1` 通过。
- 在真实微信前台执行至少一次同联系人新话题抢占验收：
  1. 旧合并回复进入长粘贴审核；
  2. 联系人发来新消息；
  3. 旧正文未提交；
  4. 新消息形成一个新合并回合；
  5. 新回复只预览和发送一次。
- 再执行一次提交边界竞态验收，证明不会重复提交。
- 控制面板和 Bridge 日志能够区分 `sent`、`superseded`、`manual_cancel`、`failed` 和 `commit_unknown`。

## 17. 预计修改边界

实现计划预计涉及：

- `bridge/bridge_core.py`
- `bridge/ob_protocol.py`
- `bridge/state.py`
- 新持久 job/outbox/pending-spool 模块
- `bridge/uia_fixed_sender.py`
- `bridge/config.py`
- `bridge/config.example.json`
- `bridge/web_panel.py`
- `scripts/Initialize-Configuration.ps1`
- `scripts/Install.ps1`
- `scripts/Stop-Services.ps1`
- `scripts/Test-Health.ps1`
- 新插件 `plugins/astrbot_plugin_akasha_merged_reply/`
- `plugins/astrbot_plugin_akasha_contact_memory/`
- `tests/python/test_bridge_runtime.py`
- `tests/python/test_uia_fixed_sender.py`
- 新合并回复插件测试
- `tests/Test-Initialization.ps1`
- `tests/Test-InstallerLayout.ps1`
- `README.md`
- `CHANGELOG.md`

不修改目标机器安装环境中的 AstrBot `site-packages`。
