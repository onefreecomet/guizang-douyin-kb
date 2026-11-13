# AUTOPILOT 设计文档（待评审）

版本：v0.1 · 2026-11-13 · 状态：设计稿，尚未实现任何代码

本文把「归藏」从手动工具改造为「自动流水线 + Obsidian 知识库」的方案定下来。评审通过后按第 10 节分三期实现。

---

## 1. 目标与原则

**用户链路**：手机刷抖音 → 收藏到收藏夹「有帮助」→ 本机（永不关机）按计划自动读取新增 → 下载校验 → 飞书妙记转写 → AI 摘要/关键词/主题/系列 → 写入 Obsidian → 每日刷新索引页 → 用户随时在 Obsidian（含手机）里阅读、检索、写笔记。

**原则**：

1. **Obsidian 是唯一真相源。** 笔记是 Markdown，纯文件，没有自己的数据库才有的事实。SQLite 只是可重建的索引缓存。
2. **程序只写一个沙盒目录。** `<你的 Obsidian vault>\90 - 抖音精选\`，写入前校验绝对路径前缀；不遍历、不修改用户其他 712 篇笔记。
3. **控制台可浏览，但不是第二套阅读器。** 浏览器端提供「列表 + 预览 + 搜索 + 跳转 Obsidian」，用于快速查阅和管理；精读、双链、写作仍在 Obsidian。
4. **幂等可恢复。** 以 `video_id` 为唯一键；任一阶段失败可单独重试，重跑不会产生重复笔记。
5. **不静默失败。** 验证码、登录失效、下载失败、转写失败、AI 失败、写库失败都要在控制台可见，并通知到人。
6. **用户手写内容神圣。** 程序只重写 `AUTO` 标记区块和它拥有的 frontmatter 字段；其余内容永不覆盖。

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────────┐
│ 控制台（8765，单页 index.html，原生 JS，无构建）              │
└───────────────┬─────────────────────────────────────────────┘
                │ REST + 轮询
┌───────────────▼─────────────────────────────────────────────┐
│ app/server.py（HTTP 服务，127.0.0.1）                        │
├─────────────────────────────────────────────────────────────┤
│ 调度器 scheduler.py   ← 每日 04:00 / 手动触发                 │
│ 流水线 pipeline.py    ← 阶段编排、暂停/恢复、事件上报          │
│ ├─ scan     收藏夹增量扫描（CDP Edge）                        │
│ ├─ download yt-dlp 下载 + MP4 校验                            │
│ ├─ transcribe 飞书妙记转写（lark-cli）                        │
│ ├─ enrich   AI 摘要/要点/主题/系列（OpenAI 兼容接口）           │
│ └─ publish  写 vault：素材 / 主题页 / 系列页 / 索引页          │
│ 索引 indexer.py       ← SQLite（work/knowledge.db）+ FTS5      │
│ 配置 config.py        ← work/config.json（含密钥，仅本机）      │
└─────────────────────────────────────────────────────────────┘
                │ 文件写入（原子替换）
┌───────────────▼─────────────────────────────────────────────┐
│ <你的 Obsidian vault>\  ← 坚果云 nutstore-sync 同步到手机       │
└─────────────────────────────────────────────────────────────┘
```

已复用的现有模块：`scripts/scan_favorites.py`（CDP 启动 Edge、过验证等待）、`scripts/download_with_cookies.py`、`scripts/batch_transcribe.py`、`scripts/task_state.py`（逐视频检查点）、`app/process_tree.py`（无窗口子进程）、`scripts/usage.py`（用量台账）、`scripts/naming.py`（文件名清理）。

---

## 3. Obsidian 目录与文件

### 3.1 目录结构

```
<你的 Obsidian vault>\
└── 90 - 抖音精选\
    ├── 00 - 索引.md              ← 总入口（每次运行后刷新）
    ├── 素材\
    │   └── 2026-11\              ← 按月分组，避免单目录上千文件
    │       ├── 圣经中最邪门的记载，神的儿子们.md
    │       └── 番茄工作法.md
    ├── 主题\
    │   ├── 神话与古代文明.md
    │   ├── 时间管理.md
    │   └── ...
    └── 系列\
        └── 人类的起源（系列）.md
```

- 视频文件**不进 vault**，笔记里用 `file:///D:/抖音视频下载/...` 链接；原始 txt 也不进 vault（正文已嵌在笔记里）。
- 文件名 = 清理后的标题（复用 `scripts/naming.py` 的 `clean_title`）；规则补充：`|` 换成 `｜`（会破坏 wikilink 别名）、去掉 `[` `]` `#`、截断到 60 字。同月重名时追加 `-<video_id 后 4 位>`。

### 3.2 素材笔记模板

```markdown
---
title: "圣经中最邪门的记载，神的儿子们"
video_id: "7300000000000000000"
url: "https://www.douyin.com/video/7300000000000000000"
author: "某博主"
saved_at: 2026-11-12
duration: "10min 14s"
source: 抖音收藏夹
favorite: 有帮助
topics: ["神话与古代文明", "宗教文本"]
series: "人类的起源"
series_index: 3
keywords: ["以诺书", "堕天使", "尼非林"]
ai_summary: "视频讨论《以诺书》中神的儿子们与人类女子结合生下巨人族的记载，并对比不同文明神话中的相似母题。"
ai_model: deepseek-v4.1-flash
processed_at: 2026-11-13T04:03:00+08:00
---

> [!abstract] 一句话摘要
> 视频讨论《以诺书》中神的儿子们与人类女子结合生下巨人族的记载，并对比不同文明神话中的相似母题。

<!-- AUTO:START -->
## 要点

- 要点一
- 要点二
- 要点三

## 原文（带时间戳）

**00:00:00** 开场……

**00:00:00.200** 正文段落……

`Keywords: 以诺书 / 堕天使 / ...`
（来源：飞书妙记转写，10min 14s）
<!-- AUTO:END -->

## 我的笔记

<!-- 这里永远由你自己写，程序不会动 -->
```

**保护机制**：

- `<!-- AUTO:START -->` 与 `<!-- AUTO:END -->` 之间：程序全权重写。
- 区块外（`## 我的笔记` 及以后，以及用户在中间手插的内容）：永不覆盖。
- frontmatter 采用**按字段合并**：程序只写自己拥有的字段（上表所列），用户新增的字段（如 `rating`、`status`、`tags`）原样保留；用户手改过 `ai_summary` 也会被下次重跑覆盖（如需保护，改 `status: 已定稿` 后程序跳过该条）。
- `status: 已定稿` / `status: 冻结` 的素材，流水线只更新主题页列表，不重写正文。

### 3.3 主题页模板

```markdown
---
type: topic
topic: 神话与古代文明
parent: 宗教与神话            # 可选，形成树
aliases: [古代神话, 神话学]
---

<!-- AUTO:START -->
## 简介

（AI 维护：这个主题在收集到的视频里讨论过什么，2-4 句）

## 相关视频（12）

- 2026-11-12 [[圣经中最邪门的记载，神的儿子们]] — 视频讨论《以诺书》中……
- 2026-11-08 [[某某]] — ……
<!-- AUTO:END -->

## 我的笔记

（自由编辑）
```

### 3.4 系列页模板

```markdown
---
type: series
series: 人类的起源
---

<!-- AUTO:START -->
## 系列综述

（AI 根据已入库各集内容生成，300 字以内；新增一集后重跑）

## 分集（3）

1. 2026-11-01 [[第1集标题]] — 一句话
2. 2026-11-05 [[第2集标题]] — 一句话
3. 2026-11-12 [[第3集标题]] — 一句话
<!-- AUTO:END -->

## 我的笔记
```

### 3.5 索引页 `00 - 索引.md`

```markdown
<!-- AUTO:START -->
# 抖音精选

共 128 条 · 23 个主题 · 6 个系列 · 最近更新 2026-11-13 04:07

## 最近入库
| 日期 | 标题 | 主题 | 时长 |
|---|---|---|---|
| 11-12 | [[……]] | [[神话与古代文明]] | 10m |

## 按主题浏览
- [[神话与古代文明]]（12）
- [[时间管理]]（8）

## 系列
- [[人类的起源（系列）]]（3 集）

## 待处理
- 3 条下载失败、1 条转写失败 → 到控制台任务队列重试
<!-- AUTO:END -->
```

索引页每日刷新；控制台顶部「立即刷新索引」按钮可手动触发。

---

## 4. 数据模型（SQLite，可重建）

`work/knowledge.db`：

```sql
CREATE TABLE videos (
  video_id      TEXT PRIMARY KEY,
  title         TEXT,
  author        TEXT,
  url           TEXT,
  saved_at      TEXT,          -- YYYY-MM-DD
  duration      TEXT,
  dir_path      TEXT,          -- <下载根目录>\2026-11-12\01_xxx
  note_relpath  TEXT,          -- 相对 vault 的路径
  phase         TEXT NOT NULL, -- discovered/downloaded/transcribed/enriched/published/failed_*/needs_manual
  attempt       INTEGER DEFAULT 0,
  error         TEXT,
  ai_model      TEXT,
  created_at    TEXT,
  updated_at    TEXT
);

CREATE TABLE topics (
  name        TEXT PRIMARY KEY,
  parent      TEXT,
  note_path   TEXT,
  count       INTEGER DEFAULT 0,
  created_at  TEXT
);

CREATE TABLE series (
  name        TEXT PRIMARY KEY,
  note_path   TEXT,
  count       INTEGER DEFAULT 0
);

CREATE TABLE runs (
  run_id      TEXT PRIMARY KEY, -- 2026-11-13-0400
  started_at  TEXT, finished_at TEXT,
  found INTEGER, downloaded INTEGER, transcribed INTEGER,
  enriched INTEGER, published INTEGER, failed INTEGER,
  status      TEXT              -- running/done/paused/failed
);

CREATE TABLE events (           -- 控制台时间线
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, video_id TEXT, phase TEXT, level TEXT, message TEXT
);

CREATE VIRTUAL TABLE notes_fts USING fts5(
  video_id UNINDEXED, title, summary, body, topics
);
```

- 删除 `knowledge.db` 后，`indexer.py` 可从 vault 里的 Markdown（frontmatter + 正文）完整重建索引 —— 这是「可重建」的验收标准。
- 索引只服务控制台的列表/搜索/状态，不承载事实。

---

## 5. 流水线阶段与状态机

| 阶段 | 输入 | 动作 | 成功标志 | 失败落点 |
|---|---|---|---|---|
| `discovered` | 收藏夹页面 | 扫出 video_id 列表，按 ID 查重 | 新增 N 条入库 | `needs_manual`（验证码/登录失效） |
| `downloaded` | video_id | yt-dlp + 校验 MP4 容器 | mp4 可播放、大小合理 | `failed_download`（重试 3 次后转人工） |
| `transcribed` | mp4 | 飞书妙记上传 + 轮询 + 导出 | 正文非空、时长可解析 | `failed_transcribe`（可重试/重建） |
| `enriched` | 转写正文 + 妙记关键词 | AI 产出 JSON | 字段齐全、主题已在树中 | `failed_ai`（降级：写素材笔记但摘要留空，标 `待加工`） |
| `published` | 上述全部 | 生成/更新素材 + 主题页 + 系列页 | 文件写入成功、索引已更新 | `failed_write` |

**状态机要点**：

- 阶段单向推进，但 `published` 可被后续内容触发**局部重跑**（如新增一集后重算系列页）。
- `needs_manual` 不算失败：流水线整体暂停，控制台黄条提示，用户处理后点「继续」，从断点继续跑。
- 每次运行写 `work/runs/<run_id>.json`（阶段计数、失败明细），控制台可回看。
- 中断恢复：`work/autopilot_state.json` 记录当前 run 的进度；服务重启后自动续跑未完成的 run。

**AI 失败降级**：AI 不可用时仍产出素材笔记（含原文、妙记关键词、时长），`ai_summary` 留空、`status: 待加工`；下次运行优先补加工这些条目。

---

## 6. 调度

- **每日 04:00**：应用内常驻定时器（服务本来常驻），不依赖 Windows 任务计划。
- **手动**：「立即同步」按钮，等价跑一次同样流程。
- **白天增量**：开关，默认关。开启后每 N 小时（默认 2）只做「扫描 + 落库」，下载/转写/AI 仍集中在夜间，避免白天占用桌面（每次扫描要启一次 Edge）。
- **错过补偿**：若 04:00 时服务未运行或机器休眠，下次启动后立即补跑一次。
- **并发**：单条串行（下载与转写对抖音/飞书接口都敏感），不做并行下载。

---

## 7. AI 加工规格

**接口**：OpenAI 兼容（`POST {base_url}/chat/completions`），已实测可用。

- Base URL：`https://amr-link.open-design.ai/v1`
- 默认模型：`deepseek-v4.1-flash`（cost=low，capability=advanced，上下文 1,048,000）
- 备选：`deepseek-v4.1-flash-eco`（更省）、`glm-5.3-flash`、`claude-haiku-5.5`、`deepseek-v4-pro`（高质量，cost=high）
- 已验证：`/v1/models` 列表、`/v1/chat/completions`、`response_format: {"type":"json_object"}` 均正常；该模型会产生 `reasoning_details`（推理 token 计入用量，单次约 100 级，可忽略）。

**单条视频的调用**（一次调用产出全部字段，控制在 1 次请求内）：

输入：标题、博主、时长、妙记关键词、转写正文（截断到 30,000 字，超长时保留首尾各 12,000 字 + 中段抽样）、**现有主题名清单**（最多 200 个）。

输出 JSON：

```json
{
  "summary": "2-3 句摘要，160 字以内",
  "key_points": ["要点1", "要点2", "要点3"],
  "topics": [{"name": "神话与古代文明", "parent": "宗教与神话", "is_new": false}],
  "series": {"name": "人类的起源", "index": 3},
  "keywords": ["以诺书", "堕天使"]
}
```

**主题碎片化控制**（关键约束）：

1. 提示词给出**现有主题清单**，要求优先复用；只有确实无法归入时才新建。
2. 新建主题必须同时给出 `parent`（已有主题或空），每条视频最多新建 1 个主题。
3. 每 20 条新内容后跑一次「主题整理」调用：合并同义主题、给孤立主题挂父级，输出变更清单写进 `work/topic_merges.json` 并在控制台可查（不自动删除主题页，只改归属和别名）。
4. 主题页/系列页的重写由程序按数据生成，AI 只提供简介和综述文字。

**成本控制**：`daily_limit`（默认 200 条/天）、单条最大 token 上限、失败重试 1 次；用量记进 `work/ai_usage.json`，控制台显示今日用量。

---

## 8. 控制台（8765）

### 8.1 布局

```
┌──────────────────────────────────────────────────────────────┐
│ ● 服务  yt-dlp 2026.10.26  Node 22  lark 1.0.100  抖音登录✓   │
│ Obsidian ✓   AI ✓   今日新增 5   下次 04:00                   │
├──────────┬───────────────────────────────────────────────────┤
│ 自动同步 │  主区域（随左侧切换）                              │
│ 任务队列 │                                                   │
│ 知识库   │                                                   │
│ 下载     │                                                   │
│ 文件     │                                                   │
│ 设置     │                                                   │
└──────────┴───────────────────────────────────────────────────┘
```

### 8.2 面板

**① 自动同步**：总开关、下次运行时间、当前阶段与进度条、当前处理条目、「立即同步」、「暂停/继续」、最近 7 天统计（新增/成功/失败）、失败快速入口。

**② 任务队列**：每条视频一张卡（标题/博主/日期/时长/当前阶段/耗时/错误），操作：重试、跳过、在 Obsidian 打开、打开视频。筛选：全部/失败/待人工/已入库。

**③ 知识库**（用户明确要求：能在这里直接打开/展示内容）：

- 左栏：树形浏览（最近入库 / 按月素材 / 主题 / 系列）+ 顶部搜索框
- 右栏：Markdown 预览（自研 mini renderer，支持标题、列表、引用 callout、加粗斜体、行内代码、代码块、链接、`[[wikilink]]`、分隔线、表格）+ 元信息卡（博主/时长/日期/主题/系列）
- 搜索：标题 + 摘要 + 正文全文（SQLite FTS5），结果高亮命中片段，点击跳转预览
- 按钮：**在 Obsidian 中打开**（`obsidian://open?vault=<你的 vault 名>&file=90 - 抖音精选/素材/...`）、**打开视频**（`file:///`）、**重建索引**
- 只读预览；编辑一律交回 Obsidian，避免两处写冲突

**④ 设置**：AI（base_url / 模型下拉 / key / 「测试连通」按钮，key 回显为 `sk-***abcd`）、Obsidian（vault 路径 + 落点目录 + 「检查可写」）、调度（时间、白天增量开关）、通知（桌面/飞书）、高级（每日条数上限、重试次数）。

**⑤ 下载 / ⑥ 文件**：保留现有面板。

### 8.3 REST 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 扩展：加 obsidian/ai/scheduler 状态 |
| GET | `/api/autopilot` | 调度与当前运行状态 |
| POST | `/api/autopilot/run` | 立即同步 |
| POST | `/api/autopilot/pause` | 暂停 / 继续（`{action:"pause"|"resume"}`） |
| GET | `/api/queue?filter=&page=` | 任务队列 |
| POST | `/api/queue/retry` | `{video_id}` 单条重试 |
| GET | `/api/kb/tree` | 知识库目录树 |
| GET | `/api/kb/search?q=` | 全文搜索 |
| GET | `/api/kb/note?path=` | 笔记原文（校验路径必须落在落点目录内） |
| POST | `/api/kb/reindex` | 重建索引 |
| GET/POST | `/api/settings` | 读/写配置（key 只回 masked） |
| POST | `/api/settings/ai-test` | 测试 AI 连通 |

安全：仅监听 `127.0.0.1`；`/api/kb/note` 做路径规范化与前缀校验；不做登录（本机单用户）。

---

## 9. 配置、失败与通知

`work/config.json`（运行目录，权限 600，不进日志、不进 vault）：

```json
{
  "ai": {
    "base_url": "https://amr-link.open-design.ai/v1",
    "model": "deepseek-v4.1-flash",
    "api_key": "<本机保存，日志与页面均只显示掩码>",
    "temperature": 0.2,
    "max_tokens": 2000,
    "daily_limit": 200
  },
  "obsidian": {"vault": "<你的 Obsidian vault>", "folder": "90 - 抖音精选"},
  "schedule": {"enabled": true, "time": "04:00", "daytime_poll_hours": 0},
  "pipeline": {"favorite": "有帮助", "base_dir": "<下载根目录>"},
  "notify": {"desktop": true, "feishu": false, "feishu_target": ""}
}
```

**失败处理与通知**：

| 情况 | 流水线行为 | 通知 |
|---|---|---|
| 抖音验证码 / 登录失效 | 整体暂停，等待人工 | 控制台黄条 + 桌面通知（可选飞书） |
| 单条下载失败 | 重试 3 次（退避）→ 标 `failed_download` | 队列红卡，日终汇总通知 |
| 转写失败/额度不足 | 重试 → `failed_transcribe` | 同上 + 提示额度问题 |
| AI 失败 | 降级写笔记、标 `待加工` | 队列黄卡，下次自动补 |
| 写 vault 失败（权限/路径） | 该条 `failed_write`，不中断其他条 | 立即通知 |
| 磁盘空间不足 | 整体暂停 | 立即通知 |

通知渠道：默认「控制台黄条 + Windows 桌面通知」；飞书可选（复用已有 `lark-cli` 给自己发消息），配置后验证码等阻塞事件会推送到手机。

---

## 10. 分期与验收

**P1 · 自动闭环（能自己跑起来）**

- 收藏夹扫描：修 `author` 恒空、校验「真的进了收藏夹/有帮助」、扫出结构化列表（video_id/标题/链接/作者/时长）
- 增量：按 `video_id` 比对，只处理新增
- 调度：04:00 定时 + 手动触发 + 断点续跑
- 队列与状态：SQLite 落库、控制台「自动同步 / 任务队列」两个面板
- 通知：验证码暂停与失败通知
- 出片：写入 `90 - 抖音精选\素材\YYYY-MM\*.md`（含原文、关键词、时长；暂不做 AI 摘要）

验收：新收藏 1-3 条视频 → 04:00 自动跑完 → vault 出现对应笔记 → 控制台可看到每条的阶段与结果 → 人为制造一次验证码暂停并成功恢复。

**P2 · 知识库加工（有结构）**

- AI 加工：摘要、要点、关键词、主题、系列（实测接口已通）
- 主题页 / 系列页 / 索引页生成与每日刷新
- 控制台「知识库」面板：树 + Markdown 预览 + 全文搜索 + 跳转 Obsidian
- 主题碎片化控制与合并

验收：跑够 10 条 → 主题树成形、系列正确聚合、索引页链接全部有效（无死链）→ 控制台搜索能命中正文任意词 → 删除 `knowledge.db` 后能完整重建。

**P3 · 打磨**

- 时间戳深链（点笔记里的 `00:03:21` 跳到视频对应位置）
- 系列综述、主题综述
- 手机端浏览优化（索引页做阅读友好版式）
- 导出（飞书文档 / PDF）
- 主题重命名、合并的可视化工具

---

## 11. 用户确认与实现约束（2026-11-13 12:12）

以下确认优先于前文的旧默认值：

1. **通知渠道**：采用飞书通知，控制台同时显示阻塞与失败状态。后续查明当前登录用户对应的接收标识；先完成收件人与消息预览，再验证实际发送。凭据与完整接收标识不记入日志。
2. **主题起始树**：从空白开始，由 AI 归类与整理；不预置种子主题。新建、合并仍需遵守主题碎片化控制约束。
3. **定稿冻结**：`status: 已定稿` / `status: 冻结` 的素材完整冻结；不修改 frontmatter、正文、文件名、目录位置，也不主动修改它在主题/系列页中已有的列表项。重新索引只读。其他未冻结条目可以继续新增；冻结解除前不得因主题合并间接改写该条目。
4. **调度**：仅每日北京时间 04:00；保留手动同步；白天不增加自动扫描。
5. **控制台知识库浏览提前至 P1**：首期就能浏览素材、阅读正文、跳转 Obsidian；完整全文搜索、主题与系列加工按 P2 实现。
6. **原视频失效**：保留已下载文件与笔记，不自动删除；冻结素材的失效状态只在控制台显示。

## 12. 界面审查与视觉方向（待确认）

使用 qu-ai-wei（Unslop）技能；方法来源：Damir Sokolovsky [@heydamir](https://x.com/heydamir)，CC BY 4.0。

审查依据：现有 `douyin-favorites-downloader/app/index.html` 源码。尝试浏览 `http://127.0.0.1:8765/` 返回 `net::ERR_CONNECTION_REFUSED`；尚无当前页面截图，不能据此声称视觉验收通过。

### 12.1 发现

- `body` 的 radial-gradient 与 header 的毛玻璃叠加制造无信息装饰。
- `.logo` 青红渐变、800 字重，以及 `.dot.on` 发光，均抢占内容注意力。
- `.btn-main` 与两类进度条使用渐变，主要操作缺乏统一强调色。
- `.pills` 把依赖版本全部包成胶囊，页面顶部噪声多。
- 表单、说明与状态普遍 10.5–12.5px；半像素字号与多种字重削弱阅读层级。
- 14px 间距、9px 控件圆角和多种非统一 padding 缺少一致节奏。
- 现有双栏以输入与日志为主，需要按已批准的新功能扩展导航及阅读区域。

### 12.2 五行方向

- **产品**：把抖音收藏加工成可检索、可反复学习的个人资料库，同时展示自动处理的进度。
- **性格**：沉静、清楚、略带书卷气。
- **世界**：书桌、纸张、索引卡；界面以近中性的浅灰与墨色为底，深青只用于选中与主操作。
- **重点**：知识库的笔记阅读页，以及自动同步页的一条真实阶段轨迹；内容承担视觉重点，不添加装饰图片。
- **标记**：视频转写的时间戳作为页边刻度，贯穿列表元信息和原文段落。

### 12.3 布局与细节

- 保留产品名「归藏」。左导航宽 248px，行高 36px；自动同步、任务队列、知识库、下载、文件、设置，日志可从任务页打开。
- 知识库使用目录/检索结果 + 笔记阅读区域；宽屏阅读正文限制约 72 个字符，侧栏与元信息占用剩余空间。
- UI 字体采用系统中文无衬线，正文 16px、行高 1.8，元信息 12–13px，时间戳采用等宽数字；字重 400/500，必要处 600。
- 统一 4px 间距尺度；页面边距 32px、面板 padding 24px、列表 16px；控制项圆角 8px，面板圆角 12px。
- 背景 #F5F6F6，正文 #202629，次要文字 #5C666B，强调色 #17665F；错误/等待使用有含义的克制色，并配文字说明。
- 无渐变、发光、emoji 图标或装饰动画；统一 16px/1.5 stroke 线图标，清楚的 hover 与键盘焦点。
- 空库显示真实空状态，不填虚构统计；飞书通知失败在控制台可见，可重试。

按技能要求，先确认以上方向，再修改实际页面。后续依次执行间距、颜色、图标、文案、字体、布局、细节、视觉、动效九轮检查。
