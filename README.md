# 铃光记忆

一套**独立的长期记忆插件**。不依赖 livingmemory，也不需要 Ollama 或向量库。

> **它和 LivingMemory 的关系**：思路来自 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)（作者 lxfight，AGPL-3.0）的时间感知原子设计——很赞的项目。本插件是**独立重写**的实现，没有复制它的代码，取舍是「更简单、零依赖、可离线跑」。如果你需要向量检索、图谱和管理界面，请直接用 LivingMemory。

## 它解决什么问题

旧做法：一段对话聊够轮数 → LLM 总结成一条记忆 → 直接进正式库。
问题：**一件事刚出现、还没定性，就被当成"事实"记下来，跟已经确定的事平级。**

这套改成两级：

```
新记忆 → 短时暂存区（48 小时）
           ├─ 被重复提到 / 定性明确 → 转正进正式库
           ├─ 到期没定性 → 待定（压最底层，相似时能翻出来）
           └─ 重要性 < 0.3 → 直接丢弃
```

## 一条记忆的完整旅程

```
噩壤在群里说「以后回消息别太满」
    ↓
存下来：谁说的(sender_id/昵称) · 什么时候 · 上文 2 条
    ↓
分区：群聊按「同一个人 + 时间间隔」；私聊按话题
    ↓
分类：layer(谁对谁) × type(什么性质) × subtype(子类型)
    ↓
定性：玩笑 / 真骂 / 正经嘱托 / 事实 / 提问 / 情绪
    ↓
回溯：这个人以前说过类似的话吗？当时判成什么？
    ↓
置信度三档：
    ≥ 0.7   正常定型
    0.6-0.7 自己标「多半是玩笑」先顶着，不吵主人
    < 0.6   私聊问主人（10 分钟冷却）
    ↓
隐私：情感/隐私类 → 打 sensitive 标签
    ↓
重要性 ≥ 0.3 → 进暂存；< 0.3 → 丢弃
```

## 三大分类维度

### layer（谁对谁）

| 值 | 含义 |
|---|---|
| `owner_to_me` | 主人对助手说 |
| `owner_to_other` | 主人对别人说 |
| `me_to_owner` | 助手对主人说 |
| `me_to_other` | 助手对别人说 |
| `other_to_me` | 别人对助手说 |
| `other_to_other` | 别人之间 |

### type（什么性质）

`command` 命令 / `promise` 承诺 / `praise` 夸 / `blame` 骂 /
`fact` 事实 / `emotion` 情绪 / `request` 提问 / `smalltalk` 闲聊

### subtype（只三种有）

- `command` → `rule` 长期规矩 / `action` 一次性任务 / `teaching` 教导
- `fact` → `person` 人物事实 / `event` 事件经过 / `observe` 旁观
- `blame` → `behavior` 行为批评 / `identity` 人格否定

## 红线

| 档 | 规则 |
|---|---|
| ① | **与主人的私聊，一个字不进群** |
| ② | 任何私聊里标了「私密性」的内容（情感/隐私），**一个字都不往外带**，连「有个朋友提过」也不许 |

注入时按当前会话判断：群聊注入会自动剔除这两类。

## 工具（她会自己用）

| 工具 | 什么时候用 |
|---|---|
| `recall_memory` | 主人问「你记得…」「我们之前说过…」 |
| `remember_this` | 主人说「记住这个」「别忘了」 |
| `correct_memory` | 主人说「这条不是玩笑」「我判错了」 |
| `memory_status` | 想看自己有多少记忆 |

## 配置

装好之后在 AstrBot 的插件配置页调，全部有默认值：

| 项 | 默认 | 说明 |
|---|---|---|
| `owner_qq` | 空 | 留空自动取管理员 |
| `staging_ttl_hours` | 48 | 暂存区存活多久 |
| `importance_drop_threshold` | 0.3 | 低于此值直接不记 |
| `promote_min_reinforce` | 2 | 重复几次算定性 |
| `promote_min_confidence` | 0.8 | 置信度多少直接转正 |
| `dormant_keep_days` | 7 | 待定保留几天后删 |
| `group_partition_gap_minutes` | 15 | 群聊分区的时间间隔 |
| `ask_cooldown_minutes` | 10 | 问主人的冷却 |
| `summary_trigger_messages` | 30 | 多少条消息触发总结 |

## 和 livingmemory 的区别

| | livingmemory | 本插件 |
|---|---|---|
| 依赖 | Ollama + faiss 向量库 | 只要 sqlite |
| 检索 | 向量相似度 | 2 字片段 LIKE |
| 记忆层级 | 只有正式库 | 暂存 / 正式 / 待定 |
| 定性 | 无 | 六类 + 三档 |
| 红线 | 无 | 两档 |
| 纠错 | 无 | `correct_memory` |
| 代码量 | 322 文件 / 5.5 MB | 8 文件 / 约 2000 行 |

**取舍**：放弃向量检索，换来极简部署。中文短句用 2 字片段匹配已经够用；如果以后数据量到几万条再考虑加向量。

## 数据在哪

```
data/plugin_data/astrbot_plugin_lingguang_memory/
├── memory.db          记忆与原子
├── conversations.db   原始消息（总结后 30 天清理）
└── corrections.jsonl  你每次纠正的记录
```

## 注意

- **不会自动迁移 livingmemory 的数据**。要迁移得单独写脚本。
- 两个插件**不建议同时开**——会各自记一份，互相干扰。切换前先把 livingmemory 关掉。

## 安装

1. 把整个 strbot_plugin_lingguang_memory 目录放进 AstrBot/data/plugins/
2. 重启 AstrBot（新插件需要重启才会被扫描）
3. 到插件配置页按需调整（全部有默认值，装完就能用）

## 常见问题

**Q：和 LivingMemory 能一起装吗？**
不建议。两个都会往上下文注入记忆，会出现两份。切换时先禁用另一个。

**Q：能迁移 LivingMemory 的数据吗？**
不能自动迁移。表结构不同，要迁移得单独写脚本。

**Q：为什么不用向量检索？**
中文短句用 2 字片段匹配已经够用，省掉 Ollama 和 faiss 能少踩很多坑。数据到几万条时再考虑加向量。

**Q：记忆存在哪？**
`data/plugin_data/astrbot_plugin_lingguang_memory/` 下的 memory.db 和 conversations.db，纯 SQLite，可以直接备份或查看。

## 协议

MIT。随便用、改、商用，保留版权声明就行。

## 致谢

- 思路参考：[LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)（lxfight，AGPL-3.0）
- 平台：[AstrBot](https://github.com/AstrBotDevs/AstrBot)