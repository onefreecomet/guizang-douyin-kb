# 归藏 · 抖音收藏知识库 — 安装说明

by 冰临城下 · 2026-11

一个在本机跑的小工具：把抖音「收藏/喜欢」里的视频按计划下载下来，转成文字笔记，存进你自己的 Obsidian 库。
没有服务器、不上传云端，笔记和视频都在你本机。

---

## 零、两种装法，先选一个

| 装法 | 适合谁 | 怎么做 |
| --- | --- | --- |
| **安装包（推荐）** | 只想用，不想碰 Python | 双击 `归藏Setup-1.0.0.exe`，下一步到底就装完 |
| 源码自助安装 | 想看代码、改代码 | 见下面「一、二、三」三节 |

### 安装包怎么装

1. 双击 `归藏Setup-<版本>.exe`（约 33.8 MB）。
2. 安装位置默认 `%LOCALAPPDATA%\Programs\Guizang`，**不需要管理员权限**，只装在当前用户下。
3. 安装时可以选择「创建桌面图标」；「开机自动同步」默认**不勾**，想要每天自动跑再去勾。
4. 装完会直接打开控制台。第一次进去先在「设置」里填抖音 cookies 与 Obsidian 库路径——
   体检命令 `runtime\python\python.exe app\doctor.py` 会告诉你还差什么。

**它装了什么**：Python 3.13.15 便携运行时（`runtime\python`，20.3 MB）、yt-dlp（`runtime\lib`，12 MB）、
Node 22（`bin\node\node.exe`，83.2 MB）、程序本体（`app`，1.7 MB）。全部在安装目录里，互不干扰，
卸载时整目录删掉即可；**`work` 目录（配置、数据库、下载记录）会保留**，重装后接着用。

**yt-dlp 自动更新**：守护进程每 24 小时查一次 PyPI，有新版本就把 `runtime\lib\yt_dlp` 换掉，
不用重装。手动跑：`runtime\python\python.exe app\ytdlp_update.py --check`。

**卸载**：Windows「设置 → 应用」里卸载，或跑安装目录里的 `unins000.exe`。
卸载会先关掉正在跑的 `server.py` / `watchdog.py` / `tray.py`（`app\uninstall_cleanup.py`），再删程序文件。

---

## 一、需要什么

| 项目 | 是否必须 | 说明 |
| --- | --- | --- |
| Windows 10 / 11（64 位） | 必须 | 桌面快捷方式、托盘、开机自启用的是 Windows 自带能力 |
| Python 3.10 ~ 3.13 | 仅源码安装需要 | 脚本会自动找；找不到会用 uv 装一个 3.13。安装包自带运行时，不需要 |
| 网络 | 必须 | 装依赖、下载视频、访问抖音都要 |
| Node.js 22 | 必须 | yt-dlp 解抖音的 JS 挑战要用。脚本会优先用随包自带的 `bin\node\node.exe` |
| Microsoft Edge | 必须 | 控制台是一个 Edge 的「应用窗口」，不用 Edge 就退回浏览器标签页打开 |
| 抖音 cookies.txt | 必须 | 登录抖音后导出，放到设置里填的位置 |
| Obsidian 库目录 | 必须 | 笔记写到这个目录里 |
| 飞书妙记 CLI（lark-cli） | 可选 | 用来做语音转写；没有就跳过转写，其余照常 |
| AI 密钥 | 可选 | 用来做摘要/要点/主题；没有就只保存转写原文 |

> 没有「一键绿色版 exe」——这一版是**自助安装版**：脚本帮你把环境准备好，代码仍然以源码形式放着，方便你自己看、自己改。
> 想要「双击安装、进开始菜单」的安装包形态，是下一步的事。

---

## 二、安装（三步）

1. **解压**到一个你打算长期放的位置，路径别用中文以外的怪字符，例如 `D:\Apps\guizang`
2. 打开 PowerShell，进到解压出来的目录，运行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File tools\bootstrap.ps1
   ```

   它会依次做四件事，**每一步都可以重复跑**（已经完成的会自动跳过）：
   - 找 Python → 建 `.venv`（或直接用 `runtime\python` 便携运行时）→ 装 `yt-dlp`
   - 准备 node：自带 `bin\node` → 旧环境 → 系统已装 → 官网下载 v22
   - 建桌面快捷方式「归藏」+ 开机自启「归藏守护」（守护进程）
   - 跑一遍体检，把还缺的东西列出来

3. 按体检结果补齐 **抖音 cookies** 和 **Obsidian 库目录**，再跑一次 bootstrap 确认全绿。

常用参数：

```powershell
.\tools\bootstrap.ps1 -SkipNode        # 不处理 node
.\tools\bootstrap.ps1 -SkipShortcut   # 不建快捷方式/自启（只想先跑起来看看）
.\tools\bootstrap.ps1 -Yes            # 不问，直接干
```

---

## 三、体检怎么读

```powershell
.venv\Scripts\python.exe app\doctor.py
.venv\Scripts\python.exe app\doctor.py --json    # 给脚本读
```

三种标记：

- `✔` 好
- `✘` **关键项**没过，程序不能正常工作
- `—` 可选功能没开，不影响主流程

关键项：Python、node、yt-dlp、数据目录、浏览器、抖音 cookies、Obsidian 库。
可选项：飞书妙记 CLI、AI 密钥、飞书通知。

退出码：`0` = 关键项全过；`1` = 还有关键项没过。

> 控制台是 GBK 的时候打不出 `✔`，体检会自动退回 `[OK]`/`[!!]`/`[--]`，不会崩。

---

## 四、启动与退出

- **桌面快捷方式**：双击「归藏」
- **手动**：`app\start.bat`（服务没起就起服务，起了就只把窗口叫出来）
- **命令行**：`.venv\Scripts\pythonw.exe app\launcher.py`

窗口右上角的 × **不会退出程序**，只是把窗口收起来，服务和托盘继续跑。
要真正停下来：托盘图标右键 →「退出服务（停止后台同步）」；或者控制台左下角「退出服务」。

托盘菜单（右下角图标右键）：
`打开控制台` / `在浏览器中打开` / `关于归藏（by 冰临城下）` / `退出服务（停止后台同步）` / `只退出托盘`

---

## 五、东西存在哪

| 内容 | 位置 | 能不能改 |
| --- | --- | --- |
| 程序 | 解压出来的目录 | 随便挪，挪完重跑一次 bootstrap 重建快捷方式 |
| 数据（配置/台账/数据库） | 默认 `<程序目录>\work` | 设环境变量 `GUIZANG_HOME` 可以指到别处 |
| 下载的视频 | 设置里「下载目录」，默认 `~\抖音视频下载` | 可以在设置里改 |
| 抖音 cookies | 设置里「cookies 路径」 | 可以在设置里改 |
| 笔记 | 设置里「Obsidian 库」 | 可以在设置里改 |

配置里含 AI 密钥，**不要把这个目录发给别人**。

---

## 六、常见问题

**`bootstrap` 说找不到 Python**
装一个 3.10–3.13（[python.org](https://www.python.org/downloads/windows/)，安装时勾 "Add python.exe to PATH"），或先装 [uv](https://docs.astral.sh/uv/)（脚本会用 `uv python install 3.13` 自己装）。

**说 node 找不到**
脚本会尝试官网下载。国内网不通就手动装 Node.js 22，或者把 `node.exe` 放到 `bin\node\node.exe`。

**任务栏图标变成了地球 / 空白**
控制台窗口的图标来自网页的 favicon。检查 `app\favicon-32.png`、`app\favicon-16.png` 是否还在，然后重启服务。

**关掉窗口后托盘图标不见了**
托盘进程被杀了。再双击一次快捷方式即可（守护会在启动时把它拉起来）。

**飞书妙记那句一直显示「未找到」**
转写是可选功能。要开就把 `lark-cli` 装好并登录飞书；路径也可以用环境变量 `GUIZANG_LARK` 指定。

**改了代码要重启吗**
`app\index.html`、`docs` 里的静态文件：刷新页面就生效。`app\*.py`：要重启服务（托盘 → 退出服务，再双击快捷方式）。

---

## 七、给想改代码的人

```powershell
# 全套测试（251 项，约 21 秒）。有便携运行时就直接用它，没有才用 .venv
runtime\python\python.exe -m unittest discover -s tests -v

# 只看路径解析这块
runtime\python\python.exe -m unittest tests.test_paths -v

# 体检
runtime\python\python.exe app\doctor.py
```

**打安装包**（需要 Inno Setup 6；`winget install JRSoftware.InnoSetup`）：

```powershell
# 1. 准备便携运行时（20 MB）+ yt-dlp（12 MB），下过一次会缓存
powershell -ExecutionPolicy Bypass -File tools\make_portable.ps1

# 2. 拼 payload、编译安装包 → dist\归藏Setup-<版本>.exe
#    版本号只从 tools\version.iss 读，不用传 -Version（要改版本就改那一行）
powershell -ExecutionPolicy Bypass -File tools\make_installer.ps1

# 3. 冒烟测试：静默装到 C:\gz-inst-test → 体检 → 起服务探接口 → 卸载并确认删干净
powershell -ExecutionPolicy Bypass -File tools\verify_installer.ps1

# 4. 干净环境验证：装到 C:\gz-clean-test，PATH 只剩 System32、USERPROFILE 指向空目录，
#    确认不依赖本机的 python / node / uv / .workbuddy（27 项检查）
powershell -ExecutionPolicy Bypass -File tools\verify_clean_env.ps1

# 5. 代码签名（可选，需要证书；没有证书会打印说明并正常退出）
powershell -ExecutionPolicy Bypass -File tools\sign_installer.ps1 -Pfx 你的证书.pfx
powershell -ExecutionPolicy Bypass -File tools\sign_installer.ps1 -Thumbprint <证书指纹>
```

构建链上几个必须知道的坑（踩过）：

- 便携运行时的 `python313._pth` 里**必须有 `..\..\app`**。嵌入式 Python 一旦有 `._pth`，
  就不再自动把「脚本所在目录」塞进 `sys.path`，`from process_tree import ...` 会 ModuleNotFoundError。
- 安装包**不能直接从 `D:\...\dist\` 启动**（退出码 1、连日志都不生成），先拷到本地盘再跑。
- 静默安装要传 `/DIR=C:\path`（**不加引号**）且路径不含空格，否则会落到默认目录。
- `[Tasks]` 里 `startup` 的 `Flags` 是 `unchecked`：向导里默认不勾开机自启。
- **安装包没有代码签名**。首启可能弹 SmartScreen「Windows 已保护你的电脑」，点「更多信息 → 仍要运行」即可。
  要消掉这个提示只能买代码签名证书（OV 数千元/年，EV 更贵），拿到证书后跑 `tools\sign_installer.ps1` 重签、重新上传 Release。
- **本机没有 Windows 沙盒/虚拟化**（`Containers-DisposableClientVM` 为 Disabled、`HypervisorPresent` 为 False），
  所以「干净机器验证」是用 `tools\verify_clean_env.ps1` 剥净环境近似的：PATH 只剩 System32、`USERPROFILE` 指向空目录、
  数据目录隔离到临时位置。它能证明不依赖本机开发环境，但**不等于**在真虚拟机上装过。

代码结构：

- `app/paths.py` — **唯一**判断「东西在哪儿」的地方（node / lark / Edge / 数据目录 / 便携 python / venv 解释器）。要加新的外部依赖，往这里加，不要散落在各处的 `shutil.which`。
- `app/doctor.py` — 体检，只读、不联网。
- `app/server.py` — 本机 HTTP 服务（127.0.0.1:8765），也是控制台页面和 API。
- `app/tray.py` — 右下角托盘（纯 ctypes，无第三方库）。
- `app/launcher.py` — 双击之后干的第一件事：起服务、叫窗口、拉托盘。
- `app/watchdog.py` — 守护，定时检查服务在不在、托盘在不在，顺带每天查一次 yt-dlp 新版。
- `app/ytdlp_update.py` — yt-dlp 自我更新（从 PyPI 下 wheel，替换 `runtime\lib\yt_dlp`）。
- `app/uninstall_cleanup.py` — 卸载前把本目录起来的进程关掉。
- `app/create_shortcut.py` — 建桌面「归藏」/ 启动「归藏守护」快捷方式（走 PowerShell COM，不依赖 pywin32）。
- `tools/bootstrap.ps1` — 源码自助安装脚本（幂等）。
- `tools/version.iss` — 版本号的**唯一来源**（`#define AppVersion "x.y.z"`）；改版本只改这一行。
- `tools/make_portable.ps1` / `make_installer.ps1` / `verify_installer.ps1` / `verify_clean_env.ps1` / `sign_installer.ps1` / `installer.iss` — 安装包构建、冒烟测试、干净环境验证、签名。
- `tests/` — 单元测试，全部用临时目录 + mock，不碰真实数据。

约定：

1. **新增外部依赖 → 先加到 `paths.py`**，再让调用方从 `paths` 取。
2. 不要 `import win32com` 之类只在某些机器上有的东西；要用系统能力就走 PowerShell / ctypes。
3. 改完跑全套测试；测试数不能减少。
4. 日志里不要写 cookies、令牌、AI 密钥。

---

## 八、卸载

**安装包装的**：Windows「设置 → 应用」→ 找到「归藏 · 抖音收藏知识库」→ 卸载。
（会先关掉在跑的进程，再删程序文件；`work` 目录保留，想彻底清洁就手工删掉它。）

**源码装的**：

1. 托盘 →「退出服务」
2. 删桌面快捷方式「归藏」
3. 删开机自启里的「归藏守护」（`Win+R` → `shell:startup`）
4. 直接删掉解压出来的目录
5. （可选）删下载目录和 Obsidian 里已经生成的笔记——这两处是你的数据，程序不会去动
