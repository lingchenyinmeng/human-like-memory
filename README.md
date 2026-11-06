# Human-like Memory

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
data/plugin_data/astrbot_plugin_human_like_memory/
├── memory.db          记忆与原子
├── conversations.db   原始消息（总结后 30 天清理）
└── corrections.jsonl  你每次纠正的记录
```

## 注意

- **不会自动迁移 livingmemory 的数据**。要迁移得单独写脚本。
- 两个插件**不建议同时开**——会各自记一份，互相干扰。切换前先把 livingmemory 关掉。

## 安装

1. 把整个 astrbot_plugin_human_like_memory 目录放进 AstrBot/data/plugins/
2. 重启 AstrBot（新插件需要重启才会被扫描）
3. 到插件配置页按需调整（全部有默认值，装完就能用）


## 从 LivingMemory 迁移

如果你之前用的是 LivingMemory，可以把它记的东西搬过来：

```bash
# 先看会迁多少，不写库
python tools/migrate_from_livingmemory.py --dry-run

# 真迁（写前会自动备份新库）
python tools/migrate_from_livingmemory.py
```

**路径会自动探测**——脚本会从自己所在位置往上找 AstrBot 的 `data` 目录。
找不到就手动指定：

```bash
python tools/migrate_from_livingmemory.py --old <旧库> --new <新库>
```

或者设环境变量 `ASTRBOT_DATA_DIR` 指向 AstrBot 的 `data` 目录。

**迁移会保留**：内容、摘要、key_facts、参与者、重要性、时间、状态、说话人、定性、隐私标记。
**会重算**：话语类型（旧库是记忆类型，语义不同）。

**重复跑不会重复插入**——每条迁过来的记忆在 metadata 里记了 `source_memory_id`。

## 语义检索（可选）

默认只用关键词。想让召回更准，可以接本机的 Ollama：

```bash
# 1. 确认 Ollama 有 bge-m3
ollama pull bge-m3

# 2. 给现有记忆建一次向量索引（957 条约 1 分钟）
python tools/build_embeddings.py
```

建完重载插件，日志里出现「语义检索已启用」就成了。

**之后新记忆会自动补向量**，不用再手动建。

配置项：

| 项 | 默认 | 说明 |
|---|---|---|
| `semantic_enabled` | `auto` | auto 自动检测；on 强制开；off 关 |
| `semantic_base_url` | `http://127.0.0.1:11434` | Ollama 地址 |
| `semantic_model` | `bge-m3` | 嵌入模型 |

**没装 Ollama 也完全没问题**——自动退回纯关键词，不报错、不影响使用。

### 效果

| 查询 | 纯关键词 | 加语义 |
|---|---|---|
| 「说我讲得太细」 | 0 条 | **3 条**（说话太直 / 描述不用太具体 / 回话太绕） |
| 「电脑里没钱了」 | 3 条（都是花费） | 3 条（把「找电脑里的 DeepSeek」提到第 1） |

语义解决的是「换了说法就找不到」的问题。

### 实现

- 向量存 SQLite 单独表 `atom_vectors`，删掉这张表就等于关掉语义
- 检索时全部读进内存暴力算余弦（957 条约 2ms，不用 faiss）
- 关键词路和语义路各自归一到 0-1，按 0.55 : 0.45 加权

## 遗忘库（v1.2.0 新增）

正式记忆放久了不会直接删掉，而是**沉进遗忘库**：

```
正式库(active) 到期  →  遗忘库(archived)  →  保留 180 天  →  真删
```

### 关键：遗忘库不参与检索

- 遗忘库里的东西**永远搜不到**
- 只有**同一件事被重新提起**时才会被拉起来
- 拉起后回到暂存区，重新走一遍「定性 → 转正」

这跟「待定(dormant)」不一样：待定只是压权重、还能搜到；遗忘库是彻底搜不到，只等着被自己的"回声"唤醒。

### 为什么阈值比暂存区宽松

| 场景 | 阈值 |
|---|---|
| 暂存区查重（同一批对话） | 0.85 |
| 遗忘库拉起（隔了很久） | **0.62** |

实测：「养过一只叫小黑的狗」vs「养的那只狗叫小黑」，相似度 **0.65**——用 0.85 拉不起来，用 0.62 就能。

因为隔了几个月再提起，说法肯定不一样。

### 配置

| 项 | 默认 | 说明 |
|---|---|---|
| `archive_keep_days` | 180 | 遗忘库保留多少天后真删 |
| `recover_on_repeat` | true | 被再次提起时是否拉起 |
| `recover_similarity_threshold` | 0.62 | 拉起相似度阈值 |
## 常见问题

**Q：和 LivingMemory 能一起装吗？**
不建议。两个都会往上下文注入记忆，会出现两份。切换时先禁用另一个。

**Q：能迁移 LivingMemory 的数据吗？**
不能自动迁移。表结构不同，要迁移得单独写脚本。

**Q：为什么不用向量检索？**
中文短句用 2 字片段匹配已经够用，省掉 Ollama 和 faiss 能少踩很多坑。数据到几万条时再考虑加向量。

**Q：记忆存在哪？**
`data/plugin_data/astrbot_plugin_human_like_memory/` 下的 memory.db 和 conversations.db，纯 SQLite，可以直接备份或查看。

## 协议

MIT。随便用、改、商用，保留版权声明就行。

## 致谢

- 思路参考：[LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)（lxfight，AGPL-3.0）
- 平台：[AstrBot](https://github.com/AstrBotDevs/AstrBot)