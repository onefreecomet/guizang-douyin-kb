# 归藏 · 抖音收藏知识库

把手机上的抖音收藏，自动变成自己电脑里的 **Obsidian 知识库**。

在手机上刷到值得留的内容 → 收藏到自己的抖音收藏夹（默认「有帮助」，可在设置里改） → 这台电脑每天到你设的时间（默认凌晨 04:00）自动扫描新增 →
下载视频 → 飞书妙记转写 → AI 生成摘要/要点/主题/系列 → 写成带双链的 Markdown 笔记 →
在 Obsidian（含手机端同步）里随时回来查、回来读、继续写自己的笔记。

> 「归藏」取「收藏归位、藏之于己」之意。控制台是流水线操作台，Obsidian 才是书房。

**下载安装**：[最新 Release](https://github.com/onefreecomet/guizang-douyin-kb/releases/latest) 里的 `Guizang-Setup-<版本>.exe`，双击即用（自带 Python / yt-dlp / Node，免管理员）。想从源码跑见下面「方式二」。

---

## 它解决什么问题

刷到好东西先收藏，然后永远不再打开——收藏夹越积越多，却从来没有变成自己的东西。
归藏把「收藏」变成「可检索、可回顾、可继续加工的个人资料库」，全程自动，只需要偶尔处理验证码。

## 功能

- **每日自动增量**：每天在你设的时间（默认 04:00，北京时间）扫描收藏夹，白天不乱跑；也可在控制台点「立即同步」。
- **四个开关都能自己改**：收藏夹名、每日运行时间、知识库子目录、下载目录全部在「设置」里改，改完立刻生效（`next_run`、扫描目标、笔记落点一起跟着变），不再是写死的灰框。
- **按视频 ID 去重**：重命名、跨日期、重复收藏都不会重复下载或重复付费转写。
- **下载三级通道**：yt-dlp → 抖音 iesdouyin 分享页直链 → greenvideo 解析，前一条被签名墙或 4291 限流挡住时自动换下一条；「暂时解析不到直链」不再等于整个收藏失败，可在队列里点「重试未完成项」补跑。
- **下载校验**：先写临时文件，再校验退出码 / HTTP / MP4 容器结构，最后原子落盘。
- **飞书妙记转写**：[lark-cli](https://github.com/larksuite) 上传视频 → 创建妙记 → 轮询取回带时间戳的文稿；
  失败可重试且复用已建妙记，**不会重复计费**。
- **AI 加工**：摘要、要点、关键词、主题、系列（OpenAI 兼容接口，默认走可替换的自定义端点）。
- **受控写入 Obsidian**：只写 `<vault>/90 - 抖音精选/`，自动生成素材笔记、主题页、系列页和总索引；
  用户手写区被 `<!-- AUTO:START -->` / `<!-- AUTO:END -->` 区块严格保护，`status: 已定稿` 的笔记不会被改动。
  子目录名可自定义（默认 `90 - 抖音精选`），程序仍然只写这一个目录。
- **知识库问答**：控制台内置对话框，既懂你的笔记内容，也懂这个程序怎么用。
- **稳定性**：开机自启 + 守护进程崩溃自愈（30 秒探活）、可恢复失败自动重试（退避 + 上限）、
  收藏夹没扫完时降级为「部分成功」而不是整轮失败。
- **卡在验证码不会永久停摆**：验证码/人工验证把这一轮置为「等待人工验证」后，当天不再重复弹窗，**次日到点会自动重跑**；主动暂停（你自己点的「暂停」）才需要手动恢复。
- **通知**：需要人工验证码、同步完成、同步暂停时通过飞书机器人私聊提醒。
- **关窗不退出**：关掉控制台窗口只是缩到右下角托盘，后台同步继续跑；托盘图标左键叫回窗口，
  右键可打开控制台 / 看作者 / 退出服务 / 只退出托盘。
- **三套皮肤**：纸白·墨、夜色·苍青、暖纸·朱砂，设置页一键切换。
- **能装给自己的亲友**：一条命令打安装包，双击即用，自带 Python / yt-dlp / Node。

## 架构

```
手机抖音 App ──收藏──▶ 收藏夹（默认「有帮助」，可改）
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
                       <vault>/<知识库子目录>/
                         ├── 00 - 索引.md
                         ├── 素材/YYYY-MM/<标题>.md
                         ├── 主题/<主题>.md
                         └── 系列/<系列>.md
                          │
                    坚果云 / 任意同步 ──▶ 手机 Obsidian 阅读
```

控制台（`http://127.0.0.1:8765/`）是操作台：自动同步、任务队列、知识库浏览、下载、文件、设置、说明七页（说明页写清了它做什么、怎么用、出问题怎么办）；
**不在浏览器里做第二套笔记编辑器**，需要改笔记时跳转 Obsidian。

## 环境要求

> 用安装包的话，下面前四项**都不用自己装**——安装包里带了 Python 运行时、yt-dlp 和 Node。

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| Windows 10/11 | — | 依赖 Job Object、`pythonw`、Edge |
| Python | 3.13 | 主程序与测试（纯标准库，无第三方包） |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 2026.10 及以上 | 下载抖音视频（守护进程每 24 小时自动升级） |
| Node.js | 22 | 供 `lark-cli` 使用 |
| [lark-cli](https://www.npmjs.com/package/@larksuiteoapi/lark-cli) | 1.0.100+ | 飞书妙记上传与取稿 |
| Microsoft Edge | — | CDP 自动化扫描收藏夹，也是控制台窗口壳 |
| Obsidian | 1.5+ | 阅读端（可选，但推荐） |

## 快速开始

### 方式一：安装包（推荐）

1. 双击 `归藏Setup-<版本>.exe`（约 33.8 MB）。免管理员，装在 `%LOCALAPPDATA%\Programs\Guizang`。
2. 装完自动打开控制台；第一次进去在「设置」里填：下载根目录、cookies 路径、Obsidian vault 路径、
   飞书通知接收人、AI 接口地址与密钥。
3. 拿不准还缺什么就跑体检：

   ```powershell
   runtime\python\python.exe app\doctor.py
   ```

安装包自带 Python 3.13.15 便携运行时、yt-dlp 与 Node 22；控制台、托盘、开机自启都在安装时建好。
卸载走「设置 → 应用」，会先关掉在跑的进程再删程序文件，`work` 数据保留。

### 方式二：源码

1. 把仓库放到本机任意目录（下文记为 `<安装目录>`）。
2. 一键准备环境并体检：

   ```powershell
   powershell -ExecutionPolicy Bypass -File tools\bootstrap.ps1
   ```

   它会找 Python → 建 `.venv` → 装 yt-dlp → 准备 node（优先用自带的 `bin\node`）→ 建快捷方式 → 体检。
   每一步都可重复跑。
3. 装飞书妙记 CLI 并登录：

   ```powershell
   npm i -g @larksuiteoapi/lark-cli
   lark-cli auth login
   ```

4. 导出浏览器 cookies 到 `%USERPROFILE%\抖音视频下载\cookies.txt`（路径也可以在「设置」里改）
   （或直接运行 `scripts\export_cookies.py`）。
5. 启动：双击桌面「归藏」，或

   ```powershell
   runtime\python\pythonw.exe app\launcher.py
   ```

   也可以双击 `app\start.bat`，或直接 `app\server.py --port 8765`（不带窗口壳）。
6. 打开 `http://127.0.0.1:8765/`，在「设置」里填齐配置。
7. 首次建议点一次「立即同步」走完整链路，确认能写入 Obsidian。

更细的安装/排错/构建安装包说明见 [INSTALL.md](INSTALL.md)。

### 开机自启与崩溃自愈

安装包会在「启动」里放一个 `归藏守护` 快捷方式（安装时可选择不勾）。源码方式则手动把
`app\watchdog.py` 放进启动项（`shell:startup` 放一个快捷方式）：

```
pythonw.exe app\watchdog.py --interval 30
```

守护进程启动时会先拉起一次服务（不用等你等到两轮探活失败），之后每 30 秒 `GET /api/ping` 探活、
连续两次失败才重启；顺手把被杀掉的托盘图标拉回来、每天查一次 yt-dlp 新版。
`work\watchdog.lock` 保证单实例。

控制台「退出服务」会写 `work\watchdog.pause` 标记，守护进程看到后不再拉起 —— 但这条标记只在
**本次开机内**有效：下次开机时守护进程发现它是上一次开机前留下的，就会直接清掉并立即拉起服务，
所以你不用担心「昨天点过一次退出，今天开机自启就永久失效」。想马上恢复，双击桌面图标即可
（启动器会顺手清掉标记）。「退出服务」按下去之后，守护进程补拉的托盘也不会把服务偷偷拉回来。

## 界面

控制台是一张纸：标题用宋体、面板有版心线、空状态是线描小图（匣 / 卷 / 册 / 笔 / 简），
侧栏用书法字标「归藏」。三套皮肤各走一个色相，不是同一个红换深浅：

| 皮肤 | 主色 | 纸面 | 适合 |
| --- | --- | --- | --- |
| 纸白 · 墨 | `#1C1F23` 墨 | `#F6F5F2` | 白天、长时间看 |
| 夜色 · 苍青 | `#77B4AB` 苍青 | `#14181A` | 夜里 |
| 暖纸 · 朱砂 | `#A9482C` 朱砂 | `#F3EADC` | 想要点旧书气 |

配色对比度全部过 WCAG AA（最紧的一对 4.5:1 以上），标题字自托管式回退到系统宋体，不联网取字体。
品牌资产在 `app/assets/`，图标由 `app/make_icon.py` 从 `app/icon_frames/` 拼出十档 ICO，
另配 PNG favicon 给 Chromium 用（Chromium 解不了纯 DIB 帧的 ICO，任务栏会退回默认地球图标）。

配置存在**运行目录** `work/config.json`（首次启动自动生成），含敏感字段，因此永远不纳入版本库：

| 键 | 说明 |
| --- | --- |
| `favorite` / `schedule_time` | 收藏夹名（默认「有帮助」）、每日运行时间（默认 `04:00`，24 小时制 `HH:MM`）；两项都能在设置页直接改 |
| `base_dir` / `cookies` | 下载根目录、cookies 文件路径 |
| `vault_path` / `kb_subdir` | Obsidian vault 路径、受控子目录（默认 `90 - 抖音精选`，可改名；改完只影响之后写入的位置，旧笔记不搬家） |
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
- **写入边界**：程序只写 `<vault>/<知识库子目录>/`（默认 `90 - 抖音精选`），写入前校验路径前缀并拒绝符号链接 / 目录联接，
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
  watchdog.py        # 守护进程：探活、崩溃自愈、托盘补拉、yt-dlp 日检
  tray.py            # 右下角托盘（纯 ctypes，无第三方依赖）
  paths.py           # 「东西在哪」唯一入口：Python / node / lark-cli / Edge / 数据目录
  doctor.py          # 一条命令看还缺什么（只读、不联网）
  ytdlp_update.py    # yt-dlp 从 PyPI 自更新（不 import yt_dlp）
  uninstall_cleanup.py  # 卸载前关掉在跑的进程
  create_shortcut.py # 建桌面 / 启动项快捷方式（纯标准库，走 PowerShell COM）
  launcher.py        # 拉起服务 + Edge 独立窗口 + 托盘
  make_icon.py       # 从 icon_frames 拼多尺寸 ICO（纯标准库）
  index.html         # 单文件控制台（原生 JS，无构建步骤）
  assets/            # 品牌 logo 主图与字标
scripts/
  scan_favorites.py  # Edge + CDP 扫描收藏夹（验证码人工接管）
  download_with_cookies.py          # 三级下载路径：yt-dlp → iesdouyin 分享页 → greenvideo
  batch_transcribe.py               # 飞书妙记上传、取稿、失败容忍
  video_parser/                     # Node 脚本：解析播放器直链（无第三方依赖）
  cdp_browser.py, command_runner.py, task_state.py, usage.py, naming.py, organize.py
tools/
  bootstrap.ps1       # 源码安装：找 Python → 建环境 → 装 yt-dlp → 备 node → 体检
  make_portable.ps1   # 打包便携运行时（Python embed + yt-dlp + node）
  make_installer.ps1  # 拼 payload 并调 ISCC 打安装包
  verify_installer.ps1# 安装包全流程冒烟测试（装 → 体检 → 起服务 → 卸载）
  verify_clean_env.ps1# 干净环境验证（PATH 只剩 System32、USERPROFILE 指向空目录，27 项检查）
  sign_installer.ps1  # 代码签名（有证书才签，没证书打印说明并正常退出）
  version.iss         # 版本号唯一来源：改版本只改这一行
  installer.iss       # Inno Setup 脚本
  ai_probe.py         # 手边探针：验证 AI 接口是否通
tests/                # 284 项离线测试（unittest，无网络）
docs/                 # 设计文档与各阶段说明
```

## 测试

```powershell
runtime\python\python.exe -m unittest discover -s tests -p "test_*.py"
```

284 项测试覆盖调度与队列、自定义调度与配置校验、重试与暂停、扫描判据、三级下载通道、下载校验、妙记容忍、
AI 加工与降级、受控写入与并发、自定义子目录、主题合并、守护进程、托盘、路径解析、yt-dlp 自更新、卸载清理、
HTTP 接口与知识库问答；另有开机自启（跨开机旧标记 / 本次开机标记）、托盘暂停闸门、
退出失败回滚这几组的回归用例。
全部离线运行（AI、飞书、PyPI、进程表均用本地桩）。

## 说明与限制

- 仅支持 **抖音网页版收藏夹**，且需要本机 Edge 保持登录；验证码/登录失效时会暂停并等待人工处理
  （最多 180 秒，超时置「等待人工验证」）。
- 飞书妙记按次计费，请注意 `work/ai_usage.json` 与妙记用量；程序已做「不重复创建妙记」的保护。
- 目前面向 Windows 单机使用；控制台与 Obsidian 配置都指向本机路径。
- 主题/系列由 AI 自动归类，可能产生近似主题，控制台提供「合并到…」手动收拢。
- **安装包没有代码签名**：首次运行可能弹 SmartScreen「Windows 已保护你的电脑」，点「更多信息 → 仍要运行」。
  消掉这个提示只能买代码签名证书，拿到后跑 `tools\sign_installer.ps1` 重签。
- 本机没有 Windows 沙盒/虚拟化，所以「干净机器」是用 `tools\verify_clean_env.ps1` 剥净环境近似的；
  它能证明不依赖本机开发环境（python / node / uv / .workbuddy），但**不等于**在真虚拟机上装过。

---

## 更新日志

| 版本 | 一句话 |
| --- | --- |
| [v1.1.1](CHANGELOG.md) | 桌面图标指向当前安装、开机自启真的会拉起、「退出服务」不再被托盘偷偷撤销、体检提示说清默认路径 |
| [v1.1.0](CHANGELOG.md) | 设置页三栏不再写死（收藏夹 / 运行时间 / 知识库子目录），下载加分享页通道，卡验证码不再永久停摆 |
| [v1.0.0](https://github.com/onefreecomet/guizang-douyin-kb/releases/tag/v1.0.0) | 第一个可安装的公开版本 |

## 许可

[MIT](LICENSE)
