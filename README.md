# Human-like Memory

一套给 AstrBot 用的**独立长期记忆插件**。零依赖（只要 SQLite），可选接本机 Ollama 做语义检索。

> **和 LivingMemory 的关系**：思路参考 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)（作者 lxfight，AGPL-3.0）的时间感知原子设计。本插件是**独立重写**的实现，没有复制它的代码，取舍是「更简单、零依赖、可离线跑」。如果你需要向量图谱、管理界面和更成熟的生态，请直接用 LivingMemory。

## 核心：记忆是分层的，不是平的

一条记忆从出现到消失，会走过四层：

```
新记忆
  │
  ├─ 重要性 < 0.3 ──────────────────► 丢弃（不写库）
  │
  └─ 落进「暂存区」candidate（48 小时）
        ├─ 转正 ──────────────► 正式库 active
        │                          └─ 放久（180 天）► 遗忘库 archived ► 真删
        ├─ 到期没定性 ─────────► 待定 dormant（7 天）► 真删
        └─ （遗忘库被再次提起时，会拉回来重新走一遍「定性 → 转正」）
```

| 层 | 状态 | 说明 |
|---|---|---|
| 暂存区 | `candidate` | 新记忆先落这里，等定性，默认 48 小时 |
| 正式库 | `active` | 转正后长期可用 |
| 待定 | `dormant` | 到期没定性，压最底层，相似时还能翻出来，7 天后删 |
| 遗忘库 | `archived` | 放久的正式记忆沉进来，**不参与检索**，被再次提起时能拉起 |

## 一条记忆的完整旅程

```
有人在群里说「以后回消息别太满」
    ↓
① 存下来：谁说的（QQ/昵称）· 什么时候 · 上文 2 条原文
    ↓
② 分区：群聊按「同一个人 + 时间间隔(15 分钟)」；私聊按话题
    ↓
③ 三维分类：layer(谁对谁 6 种) × type(什么性质 8 种) × subtype(子类型)
    ↓
④ 定性（带判据）：玩笑 / 真骂 / 正经嘱托 / 事实 / 提问 / 情绪
    ↓
⑤ 回溯比对：这个人最近 30 天说过类似的话吗？当时判成什么？
    ↓
⑥ 置信度三档：
    ≥0.7    正常定型
    0.6–0.7 自己标「多半是玩笑」先顶着，不吵主人
    <0.6    私聊问主人（10 分钟冷却）
    ↓
⑦ 隐私标记：情感/隐私类 → 打 sensitive 标签
    ↓
⑧ 重要性筛选：<0.3 丢弃；≥0.3 进暂存区
```

## 转正：四条路，任一满足

| # | 条件 | 默认阈值 |
|---|---|---|
| ① | 同一件事被重复提到 | ≥2 次 |
| ② | 定性置信度够高 | ≥0.8 |
| ③ | 陈述/承诺/夸奖类（事实不用判「是玩笑还是真骂」） | 置信 ≥0.55 |
| ④ | 重要性够高（重要的事该记就记） | ≥0.65 |

外加一个**定时重判**：维护循环每 10 分钟扫一遍暂存区，够条件就转正（v2.6.0 起的兜底，避免「只说过一次的话」永远卡在暂存区）。

## 三大分类维度

### layer（谁对谁，6 种）

| 值 | 含义 |
|---|---|
| `owner_to_me` | 主人对助手说 |
| `owner_to_other` | 主人对别人说 |
| `me_to_owner` | 助手对主人说 |
| `me_to_other` | 助手对别人说 |
| `other_to_me` | 别人对助手说 |
| `other_to_other` | 别人之间（助手只是旁观） |

### type（什么性质，8 种）

`command` 命令 / `promise` 承诺 / `praise` 夸奖 / `blame` 批评 /
`fact` 事实 / `emotion` 情绪 / `request` 提问 / `smalltalk` 闲聊

### subtype（只三种 type 细分）

- `command` → `rule` 长期规矩 / `action` 一次性任务 / `teaching` 教导
- `fact` → `person` 人物事实 / `event` 事件经过 / `observe` 旁观
- `blame` → `behavior` 行为批评 / `identity` 人格否定

## 性质判据：判了要有理由，改判能回档（v2.7.0）

每条记忆的性质不只存结论，还存**为什么这么判**：

| 字段 | 存什么 |
|---|---|
| `nature_reason` | 为什么判成这个性质，一句话 |
| `nature_evidence` | 原文摘录或触发的关键词 |
| `nature_by` | 谁判的：rule / history / assume / owner |
| `nature_ver` | 改判次数，改一次 +1 |

- **改判留档**：`correct_memory` 改一次，就往 `atom_nature_history` 写一条——谁改的、从什么改成什么、理由都留，可回看。
- **外号可追**：一个人的每个叫法（QQ/真名/昵称/外号/群名片）都记来源和状态（`confirmed` / `guess` / `rejected`），不会再「看到群昵称就当真」。

## 人物与关系：人是原子，关系是边（v2.0–v2.3）

- **人**：每个人是独立原子，有自己的活性（`active` / `quiet` / `archived`），永远不会因为「没关系」而被删。
- **关系**：两个人之间连一条边，类型有 家人 / 恋人 / 朋友 / 同事 / 同学 / 主仆 / 师徒 / 熟人 / 陌生人 / 敌对。
- **多向验证**：一条关系由证据链支撑——主人亲口 0.85、当事人 0.80、第三方 0.70、自己推 0.40；单条封顶 0.85，独立来源 ≥2 才 `confirmed`，只有 1 个来源是 `single`，说法矛盾进 `conflicted`。
- **关系历史保留**：关系变了给旧边写结束时间、开新边，不覆盖。

## 检索：两条腿走路

- **关键词路（始终可用）**：2 字片段匹配 + 停用词过滤 + 同义词扩展 + 结果去重。
- **语义路（可选）**：接本机 Ollama 的 `bge-m3`，解决「换了说法就找不到」的问题；向量存 SQLite，内存暴力算余弦，不引入 faiss。没装 Ollama 就自动退回纯关键词。

## 红线（两档）

| 档 | 规则 |
|---|---|
| ① | 与主人的私聊，一个字不进群 |
| ② | 任何私聊里标了「私密性」的内容（情感/隐私），一个字都不往外带，连「有个朋友提过」也不许 |

## 管理面板（三个标签页）

装好之后，在 AstrBot 后台「插件 → Human-like Memory → 视图」打开：

| 标签 | 看什么 |
|---|---|
| 记忆 | 事件原子（状态/类型/谁对谁/性质/时间），可筛选分页 |
| 人物 | 每个人一张卡片：叫什么、提及几次、重要度、几条关系 |
| 关系 | 力导向关系图 + 关系列表 |

## 工具（她会自己用）

| 工具 | 什么时候用 |
|---|---|
| `recall_memory` | 问「你记得…」「我们之前说过…」 |
| `remember_this` | 说「记住这个」「别忘了」 |
| `correct_memory` | 说「这条不是玩笑」「我判错了」 |
| `memory_status` | 想看自己有多少记忆 |
| `who_is` | 查一个人叫什么、跟谁什么关系 |
| `relate` | 记两个人是什么关系 |

## 配置

装好之后在 AstrBot 插件配置页调，全部有默认值。核心几项：

| 项 | 默认 | 说明 |
|---|---|---|
| `owner_qq` / `owner_name` | 空 / 噩壤 | 主人是谁（QQ 号或名字） |
| `staging_ttl_hours` | 48 | 暂存区存活多久 |
| `importance_drop_threshold` | 0.3 | 低于此值直接不记 |
| `dormant_keep_days` | 7 | 待定保留几天后删 |
| `archive_keep_days` | 180 | 遗忘库保留几天后删 |
| `recover_similarity_threshold` | 0.62 | 遗忘库拉起相似度阈值 |
| `semantic_enabled` | auto | 语义检索开关 |
| `hot_memory_count` | 3 | 热记忆注入几条 |
| `inject_max_chars` | 400 | 注入字数上限 |

完整配置见 `_conf_schema.json`。

## 安装

1. 把 `astrbot_plugin_human_like_memory` 整个目录放进 `AstrBot/data/plugins/`
2. 重启 AstrBot（新插件需要重启才会被扫描）
3. 到插件配置页按需调整（全部有默认值，装完就能用）

## 从 LivingMemory 迁移

```bash
python tools/migrate_from_livingmemory.py --dry-run   # 先看迁多少，不写库
python tools/migrate_from_livingmemory.py             # 真迁（写前自动备份）
```

路径自动探测，找不到用 `--old` / `--new` 指定。重复跑不会重复插入。

## 语义检索（可选）

```bash
ollama pull bge-m3
python tools/build_embeddings.py
```

建完重载插件，日志出现「语义检索已启用」即成。之后新记忆自动补向量。

## 常见问题

**Q：必须装 Ollama 吗？** 不用。语义检索可选，没装就是纯关键词。

**Q：和 LivingMemory 能一起装吗？** 不建议。两个都往上下文注入记忆，会各记一份互相干扰。

**Q：记忆存哪？** `data/plugin_data/astrbot_plugin_human_like_memory/` 下的 `memory.db`、`conversations.db`、`people.db`，纯 SQLite，可直接备份或查看。

**Q：她判断错了怎么办？** 直接说「这条不是玩笑」，她会用 `correct_memory` 改，并写进改判历史。

## 协议

**非商用许可**：个人、学习、非商业用途可自由使用、修改、分发；**禁止任何商业用途**，商用需联系作者（噩壤）单独授权。分发须保留版权声明与许可声明。详见 `LICENSE`。

## 致谢

- 思路参考：[LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)（lxfight，AGPL-3.0）
- 平台：[AstrBot](https://github.com/AstrBotDevs/AstrBot)
