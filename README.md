# 归藏 · 抖音收藏知识库

把手机上的抖音收藏，自动变成自己电脑里的 **Obsidian 知识库**。

在手机上刷到值得留的内容 → 收藏到抖音的「有帮助」收藏夹 → 这台电脑每天凌晨 04:00 自动扫描新增 →
下载视频 → 飞书妙记转写 → AI 生成摘要/要点/主题/系列 → 写成带双链的 Markdown 笔记 →
在 Obsidian（含手机端同步）里随时回来查、回来读、继续写自己的笔记。

> 「归藏」取「收藏归位、藏之于己」之意。控制台是流水线操作台，Obsidian 才是书房。

---

## 它解决什么问题

刷到好东西先收藏，然后永远不再打开——收藏夹越积越多，却从来没有变成自己的东西。
归藏把「收藏」变成「可检索、可回顾、可继续加工的个人资料库」，全程自动，只需要偶尔处理验证码。

## 功能

- **每日自动增量**：每天 04:00（北京时间）扫描收藏夹，白天不乱跑；也可在控制台点「立即同步」。
- **按视频 ID 去重**：重命名、跨日期、重复收藏都不会重复下载或重复付费转写。
- **下载校验**：先写临时文件，再校验退出码 / HTTP / MP4 容器结构，最后原子落盘。
- **飞书妙记转写**：[lark-cli](https://github.com/larksuite) 上传视频 → 创建妙记 → 轮询取回带时间戳的文稿；
  失败可重试且复用已建妙记，**不会重复计费**。
- **AI 加工**：摘要、要点、关键词、主题、系列（OpenAI 兼容接口，默认走可替换的自定义端点）。
- **受控写入 Obsidian**：只写 `<vault>/90 - 抖音精选/`，自动生成素材笔记、主题页、系列页和总索引；
  用户手写区被 `<!-- AUTO:START -->` / `<!-- AUTO:END -->` 区块严格保护，`status: 已定稿` 的笔记不会被改动。
- **知识库问答**：控制台内置对话框，既懂你的笔记内容，也懂这个程序怎么用。
- **稳定性**：开机自启 + 守护进程崩溃自愈（30 秒探活）、可恢复失败自动重试（退避 + 上限）、
  收藏夹没扫完时降级为「部分成功」而不是整轮失败。
- **通知**：需要人工验证码、同步完成、同步暂停时通过飞书机器人私聊提醒。

## 架构

```
手机抖音 App ──收藏──▶ 收藏夹「有帮助」
                          │
                    ① 扫描（Edge + CDP，需人工处理验证码/登录）
                          ▼
                    ② 下载校验（yt-dlp + cookies）
                          ▼
                    ③ 转写（飞书妙记，带时间戳原文）
                          ▼
                    ④ AI 加工（摘要 / 要点 / 关键词 / 主题 / 系列）
                          ▼
                    ⑤ 写入 Obsidian
                       <vault>/90 - 抖音精选/
                         ├── 00 - 索引.md
                         ├── 素材/YYYY-MM/<标题>.md
                         ├── 主题/<主题>.md
                         └── 系列/<系列>.md
                          │
                    坚果云 / 任意同步 ──▶ 手机 Obsidian 阅读
```

控制台（`http://127.0.0.1:8765/`）是操作台：自动同步、任务队列、知识库浏览、下载、文件、设置六页；
**不在浏览器里做第二套笔记编辑器**，需要改笔记时跳转 Obsidian。

## 环境要求

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| Windows 10/11 | — | 依赖 Job Object、`pythonw`、Edge |
| Python | 3.13 | 主程序与测试（纯标准库，无第三方包） |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 2026.10 及以上 | 下载抖音视频 |
| Node.js | 22 | 供 `lark-cli` 使用 |
| [lark-cli](https://www.npmjs.com/package/@larksuiteoapi/lark-cli) | 1.0.100+ | 飞书妙记上传与取稿 |
| Microsoft Edge | — | CDP 自动化扫描收藏夹 |
| Obsidian | 1.5+ | 阅读端（可选，但推荐） |

## 快速开始

1. 把仓库放到本机任意目录（下文记为 `<安装目录>`）。
2. 安装依赖并准备虚拟环境：

   ```powershell
   py -3.13 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install yt-dlp
   npm i -g @larksuiteoapi/lark-cli
   lark-cli auth login
   ```

3. 导出浏览器 cookies 到 `%USERPROFILE%\抖音视频下载\cookies.txt`
   （或直接运行 `scripts\export_cookies.py`）。
4. 启动：

   ```powershell
   .\.venv\Scripts\pythonw.exe app\server.py --port 8765
   ```

   也可以双击 `app\start.bat`，或运行 `app\launcher.py`（自动拉起服务并打开独立窗口）。
5. 打开 `http://127.0.0.1:8765/`，在「设置」里填：
   下载根目录、Cookies 路径、Obsidian vault 路径、飞书通知接收人、AI 接口地址与密钥。
6. 首次建议点一次「立即同步」走完整链路，确认能写入 Obsidian。

### 开机自启与崩溃自愈

把 `app\watchdog.py` 放进启动项（`shell:startup` 放一个快捷方式）：

```
pythonw.exe app\watchdog.py --interval 30
```

守护进程每 30 秒 `GET /api/ping` 探活，连续两次失败才拉起服务；`work\watchdog.lock` 保证单实例；
控制台「退出服务」会写 `work\watchdog.pause` 标记，守护进程看到后不再拉起。

## 配置

配置存在**运行目录** `work/config.json`（首次启动自动生成），含敏感字段，因此永远不纳入版本库：

| 键 | 说明 |
| --- | --- |
| `favorite` / `schedule_time` | 收藏夹名（默认「有帮助」）、每日运行时间（默认 `04:00`） |
| `base_dir` / `cookies` | 下载根目录、cookies 文件路径 |
| `vault_path` / `kb_subdir` | Obsidian vault 路径、受控子目录（默认 `90 - 抖音精选`） |
| `notify_receive_id` / `notify_receive_id_type` | 飞书 `open_id` 或 `chat_id`；留空则不发送 |
| `ai_base_url` / `ai_model` / `ai_api_key` | OpenAI 兼容接口；密钥只存本机，接口只回显「已设置」 |
| `ai_enabled` / `ai_daily_limit` | 是否启用 AI 加工与每日调用上限 |
| `retry_attempts` / `retry_wait` | 失败自动重试次数与间隔秒数 |
| `skin` | 控制台外观：`paper` / `ink` / `warm` |

## 安全与隐私

- **绝不提交**：`work/`（含 `config.json`、`pipeline.db`、`knowledge.db`、日志）、cookies、视频与转写原文、
  任何备份目录都已被 `.gitignore` 排除。
- **密钥只在本机**：AI 密钥与飞书接收人只写入运行目录 `work/config.json`，接口不回显，
  页面只显示「已设置」；`tools/ai_probe.py` 从环境变量读 key，不落盘。
- **写入边界**：程序只写 `<vault>/90 - 抖音精选/`，写入前校验路径前缀并拒绝符号链接 / 目录联接，
  不会碰你已有的其它笔记。
- **不收集数据**：除了你自己配置的 AI 接口与飞书，程序不向任何第三方发送内容。

## 目录结构

```
app/
  server.py          # 本地控制台后端（HTTP + 调度器 + 流水线装配）
  autopilot.py       # 每日调度、持久队列、暂停/恢复、重试策略
  knowledge.py       # 受控写入 Obsidian、AUTO 区块、主题/系列/索引页
  enrich.py          # AI 加工与用量台账（纯标准库 HTTP 客户端）
  chat.py            # 知识库问答（检索 + 程序说明 + 笔记上下文）
  watchdog.py        # 守护进程：探活、崩溃自愈、单实例锁
  index.html         # 单文件控制台（原生 JS，无构建步骤）
scripts/
  scan_favorites.py  # Edge + CDP 扫描收藏夹（验证码人工接管）
  download_with_cookies.py          # yt-dlp / greenvideo 两条下载路径
  batch_transcribe.py               # 飞书妙记上传、取稿、失败容忍
  video_parser/                     # Node 脚本：解析播放器直链（无第三方依赖）
  cdp_browser.py, command_runner.py, task_state.py, usage.py, naming.py, organize.py
tests/               # 203 项离线测试（unittest，无网络）
docs/                # 设计文档与各阶段说明
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

203 项测试覆盖调度与队列、重试与暂停、扫描判据、下载校验、妙记容忍、AI 加工与降级、
受控写入与并发、主题合并、守护进程、HTTP 接口与知识库问答。全部离线运行（AI 与飞书均用本地桩）。

## 说明与限制

- 仅支持 **抖音网页版收藏夹**，且需要本机 Edge 保持登录；验证码/登录失效时会暂停并等待人工处理
  （最多 180 秒，超时置「等待人工验证」）。
- 飞书妙记按次计费，请注意 `work/ai_usage.json` 与妙记用量；程序已做「不重复创建妙记」的保护。
- 目前面向 Windows 单机使用；控制台与 Obsidian 配置都指向本机路径。
- 主题/系列由 AI 自动归类，可能产生近似主题，控制台提供「合并到…」手动收拢。

## 许可

[MIT](LICENSE)
