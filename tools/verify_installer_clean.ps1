<#
干净环境实装验证（近似，不是真 VM）
  这台机器没有 Windows Sandbox、也没有 Hyper-V，所以「干净机器」用四件事近似：
    1. 剥离 PATH —— 只留 System32，确保 python / node / yt-dlp 不可能从系统里借到
    2. 全新安装目录 + 全新 GUIZANG_HOME（数据目录不落在安装目录里）
    3. 全新进程环境（子进程继承本脚本的最小环境变量）
    4. 安装包自带的守护/托盘链路端到端跑一遍（开机自启、退出服务不被撤销、桌面图标恢复）

验证内容：
  静默装 → 桌面/启动项快捷方式真的建了且指向安装目录 → doctor → 起服务探接口
  → 「开机自启」快捷方式在服务全停时拉起 → 「退出服务」不被托盘撤销
  → 桌面快捷方式把服务拉回来并清标记 → 静默卸载

用法：powershell -ExecutionPolicy Bypass -File tools\verify_installer_clean.ps1 [-Port 8798] [-Keep]

坑记录：
  · 安装包不能直接从 D 盘工作区启动（退出码 1、无日志），必须先拷到本地盘。
  · 脚本本身要 UTF-8 BOM（里面有中文输出）。
  · 守护进程固定用 DEFAULT_PORT 8765，所以「开机自启」这一段必须先把本机正在跑的
    生产服务与它的守护停掉，测完再拉回来（-KeepProd 可跳过这一段）。
#>
[CmdletBinding()]
param(
    [string]$Setup = "",
    [int]$Port = 8798,
    [switch]$Keep,
    [switch]$SkipBoot
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Setup) {
    $Setup = (Get-ChildItem (Join-Path $Root "dist") -Filter "归藏Setup-*.exe" |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
if (-not $Setup -or -not (Test-Path $Setup)) { Write-Host "找不到安装包" -ForegroundColor Red; exit 1 }

$InstallDir = "C:\gz-clean-inst"
$HomeDir    = "C:\gz-clean-home"
$Stage      = "C:\gz-clean-stage.exe"
$LogFile    = "C:\gz-clean-install.log"
$lnkStartup = Join-Path ([Environment]::GetFolderPath('Startup')) "归藏守护.lnk"
$lnkDesktop = Join-Path ([Environment]::GetFolderPath('Desktop')) "归藏.lnk"
$regKey     = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8C3F1A64-2B7D-4E19-9A55-6D2E7C4B18A1}_is1"
$ProdDir    = "C:\Users\Administrator\.workbuddy\skills\douyin-favorites-downloader"

$script:fails = 0
$script:lnkBackup = @{}
function Step($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  OK   $m" -ForegroundColor Green }
function Bad($m)  { Write-Host "  FAIL $m" -ForegroundColor Red; $script:fails++ }
function Check($cond, $msg) { if ($cond) { Ok $msg } else { Bad $msg } }
function RunExe($exe, $argLine) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = $argLine
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    if (-not $p.WaitForExit(240000)) { try { $p.Kill() } catch { } ; return -1 }
    return $p.ExitCode
}
function HostPort($p) {
    try { Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop | Out-Null; return $true } catch { return $false }
}
function Probe($p) {
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        try { if ((Invoke-WebRequest "http://127.0.0.1:$p/api/ping" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { return $true } } catch { }
    }
    return $false
}
function PostQuit($p) {
    try { (Invoke-WebRequest "http://127.0.0.1:$p/api/quit" -Method POST -UseBasicParsing -TimeoutSec 8).Content | Out-Null } catch { }
}
function KillByPath($dir) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$dir*" } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch { } }
    Start-Sleep -Milliseconds 800
}

Write-Host "安装包: $Setup"
Write-Host ("体积  : {0:N1} MB" -f ((Get-Item $Setup).Length / 1MB))

Step "清场（安装目录 / 数据目录 / 两个快捷方式 / 注册表 / 日志）"
# 快捷方式路径与生产环境重合：先备份用户真实的两个，收尾时原样放回
$script:lnkBackup = @{}
foreach ($lnk in @($lnkStartup, $lnkDesktop)) {
    if (Test-Path -LiteralPath $lnk) {
        $bak = $lnk + ".verify-bak"
        Copy-Item -LiteralPath $lnk -Destination $bak -Force
        $script:lnkBackup[$lnk] = $bak
    }
}
foreach ($p in @($InstallDir, $HomeDir)) { if (Test-Path $p) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue } }
foreach ($p in @($lnkStartup, $lnkDesktop, $LogFile, $Stage)) { if (Test-Path $p) { Remove-Item $p -Force -ErrorAction SilentlyContinue } }
if (Test-Path $regKey) { Remove-Item $regKey -Recurse -Force -ErrorAction SilentlyContinue }
KillByPath $InstallDir
Ok "干净了（用户原有的两个快捷方式已备份，收尾会放回）"

Step "剥离 PATH（只留 System32）+ 全新 GUIZANG_HOME"
$envPathBackup = $env:PATH
$env:GUIZANG_HOME_BACKUP = $env:GUIZANG_HOME
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
$env:GUIZANG_HOME = $HomeDir
$env:GUIZANG_NODE = ""
$env:GUIZANG_LARK = ""
$env:GUIZANG_BROWSER = ""
"  新 PATH = $env:PATH"
Check (-not (Get-Command python -ErrorAction SilentlyContinue)) "PATH 里没有 python"
Check (-not (Get-Command node   -ErrorAction SilentlyContinue)) "PATH 里没有 node"
Check (-not (Get-Command yt-dlp -ErrorAction SilentlyContinue)) "PATH 里没有 yt-dlp"

Step "静默安装到 $InstallDir（勾上桌面 + 开机自启）"
Copy-Item $Setup $Stage -Force
$code = RunExe $Stage ('/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LANG=chinese ' +
    '/MERGETASKS="desktopicon,startup" /LOG=' + $LogFile + ' /DIR=' + $InstallDir)
Check ($code -eq 0) "安装退出码 = $code"
Check (Test-Path "$InstallDir\unins000.exe") "装到了指定目录"
Check (Test-Path $lnkDesktop) "桌面快捷方式建了"
Check (Test-Path $lnkStartup) "启动项快捷方式建了"

Step "快捷方式必须指向这个安装目录（不是别处的旧版）"
$sh = New-Object -ComObject WScript.Shell
foreach ($pair in @(@("桌面", $lnkDesktop), @("启动项", $lnkStartup))) {
    $label = $pair[0]; $lnk = $pair[1]
    if (Test-Path $lnk) {
        $s = $sh.CreateShortcut($lnk)
        Check ($s.TargetPath -like "$InstallDir*") ("{0} TargetPath 在安装目录：{1}" -f $label, $s.TargetPath)
        Check ($s.Arguments -like "*$InstallDir*") ("{0} 参数指向安装目录" -f $label)
        Check (Test-Path -LiteralPath $s.TargetPath) ("{0} 目标文件存在" -f $label)
        Write-Host "        $label → $($s.TargetPath) $($s.Arguments)"
    } else { Bad "$label 快捷方式不存在" }
}

Step "文件清单 / 不该打包的东西"
$need = @(
    "app\server.py", "app\launcher.py", "app\tray.py", "app\watchdog.py", "app\paths.py",
    "app\process_tree.py", "app\doctor.py", "app\start.bat", "app\icon.ico", "app\index.html",
    "app\uninstall_cleanup.py", "app\ytdlp_update.py", "runtime\python\python.exe",
    "runtime\python\pythonw.exe", "runtime\lib\yt_dlp\__init__.py", "bin\node\node.exe", "unins000.exe"
)
$missing = @()
foreach ($rel in $need) { if (-not (Test-Path (Join-Path $InstallDir $rel))) { $missing += $rel } }
Check ($missing.Count -eq 0) ("关键文件齐（缺：" + ($missing -join ", ") + "）")
foreach ($gone in @("work", "tests", "docs", ".venv", "runtime\python\Lib")) {
    Check (-not (Test-Path (Join-Path $InstallDir $gone))) "$gone 未打包"
}

Step "运行时（必须全部来自安装目录，PATH 已被剥离）"
$py   = "$InstallDir\runtime\python\python.exe"
$node = "$InstallDir\bin\node\node.exe"
$v = (& $py -c "import sys;print(sys.version.split()[0])" 2>&1)
Check ($v -match '^3\.1[0-9]') "python $v"
$v = (& $py -c "import sys;print(sys.executable)" 2>&1)
Check ($v -like "$InstallDir*") "sys.executable 指向安装目录"
$v = (& $py -m yt_dlp --version 2>&1)
Check ($v -match '^\d{4}\.\d') "yt-dlp $v"
$v = (& $node --version 2>&1)
Check ($v -match '^v\d+') "node $v"
$v = (& $py -c "import process_tree, paths; print('ok')" 2>&1)
Check ($v -match 'ok') "app 模块可导入"

Step "doctor 体检（全新机器：只有 cookies / 库两项没过，且要给出默认路径提示）"
New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
$doc = (& $py "$InstallDir\app\doctor.py" 2>&1 | Out-String)
$docCode = $LASTEXITCODE
Check ($doc -match 'Python' -and $doc -match 'yt-dlp') "doctor 正常输出"
Check ($docCode -eq 1) "doctor 退出码 1（有关键项没配，符合全新安装）"
Check ($doc -match '默认路径') "提示里写清了「默认路径 … 可在控制台「设置」里改」"
Check ($doc -match '还没在控制台') "提示里写清了「这台机器还没配过路径」"
($doc -split "`n") | Select-Object -Last 10 | ForEach-Object { Write-Host "    $_" }

Step "起服务并探接口（端口 $Port，GUIZANG_HOME 由父进程环境继承）"
$srv = Start-Process -FilePath "$InstallDir\runtime\python\pythonw.exe" `
    -ArgumentList @("""$InstallDir\app\server.py""", '--port', "$Port") `
    -WorkingDirectory $InstallDir -PassThru
Check (Probe $Port) "/api/ping 通"
if (HostPort $Port) {
    foreach ($u in @("/", "/assets/logo-mark.png", "/favicon-32.png", "/favicon.ico")) {
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$Port$u" -UseBasicParsing -TimeoutSec 6
            Check ($r.StatusCode -eq 200) ("{0} {1}（{2} 字节）" -f $u, $r.StatusCode, $r.RawContentLength)
        } catch { Bad "$u 不通" }
    }
    $r = Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing
    Check ($r.Content -match 'brand-v2') "首页带样式层"
    Check ($r.Content -match 'by 冰临城下') "首页带作者署名"
    $dataDir = (& $py -c "import paths;print(paths.data_dir())" 2>&1).Trim()
    Check ($dataDir -like "$HomeDir*") "数据目录跟着 GUIZANG_HOME 走：$dataDir"
    Check (-not (Test-Path (Join-Path $InstallDir "work"))) "安装目录里没有生成 work"
}
PostQuit $Port
Start-Sleep -Seconds 3
KillByPath $InstallDir
Check (-not (HostPort $Port)) "服务已停（端口 $Port 释放）"

if (-not $SkipBoot) {
    Step "「开机自启」：先把生产服务与守护停掉，再把启动项快捷方式当开机来跑"
    $prodPids = @()
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$ProdDir*" } |
        ForEach-Object { $prodPids += $_.ProcessId; try { Stop-Process -Id $_.ProcessId -Force } catch { } }
    "  停掉生产进程: " + ($prodPids -join ", ")
    Start-Sleep -Seconds 3
    Check (-not (HostPort 8765)) "8765 已空出来"

    if (Test-Path (Join-Path $HomeDir "watchdog.pause")) { Remove-Item (Join-Path $HomeDir "watchdog.pause") -Force }
    Start-Process -FilePath $lnkStartup
    Check (Probe 8765) "启动项快捷方式把服务拉起来了（不用等两轮探活）"
    $cmd = (Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like "*$InstallDir*" } |
            Select-Object -First 1).CommandLine
    Check ($cmd -like "*$InstallDir*") "拉起的是安装目录里的 server.py"
    Get-Content (Join-Path $HomeDir "watchdog.log") -Encoding utf8 -ErrorAction SilentlyContinue -Tail 3 | ForEach-Object { Write-Host "    $_" }

    Step "「退出服务」之后，守护补拉的托盘不能把服务又拉回来"
    PostQuit 8765
    Start-Sleep -Seconds 3
    $pause = Join-Path $HomeDir "watchdog.pause"
    Check (Test-Path $pause) "退出服务写了暂停标记"
    Check (-not (HostPort 8765)) "退出后服务确实停了"
    Start-Sleep -Seconds 15
    Check (-not (HostPort 8765)) "15 秒后仍然停着（没被托盘偷偷拉回）"

    Step "双击桌面快捷方式：服务回来、标记清掉"
    Start-Process -FilePath $lnkDesktop
    Check (Probe 8765) "桌面快捷方式把服务拉起来了"
    Check (-not (Test-Path $pause)) "暂停标记已被清掉"
    PostQuit 8765
    Start-Sleep -Seconds 3
    KillByPath $InstallDir
    KillByPath $ProdDir

    Step "恢复生产环境（守护 + 服务 + 托盘）"
    Remove-Item Env:GUIZANG_HOME -ErrorAction SilentlyContinue
    Start-Process -FilePath "$ProdDir\runtime\python\pythonw.exe" `
        -ArgumentList @("""$ProdDir\app\watchdog.py""", '--interval', '30') -WorkingDirectory "$ProdDir\app"
    Check (Probe 8765) "生产服务已恢复"
    foreach ($proc in (Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue)) {
        if (-not $proc.CommandLine) { continue }
        if ($proc.CommandLine -notlike "*$ProdDir*") { continue }
        $what = 'tray'
        if ($proc.CommandLine -like '*server.py*') { $what = 'server' }
        if ($proc.CommandLine -like '*watchdog.py*') { $what = 'watchdog' }
        Write-Host ("    {0}  {1}" -f $proc.ProcessId, $what)
    }
}

$env:PATH = $envPathBackup
if ($env:GUIZANG_HOME_BACKUP) { $env:GUIZANG_HOME = $env:GUIZANG_HOME_BACKUP } else { Remove-Item Env:GUIZANG_HOME -ErrorAction SilentlyContinue }

if ($Keep) {
    Write-Host ""
    Write-Host "按 -Keep 保留 $InstallDir 不卸载" -ForegroundColor Yellow
} else {
    Step "静默卸载"
    Remove-Item $lnkDesktop, $lnkStartup -Force -ErrorAction SilentlyContinue
    $code = RunExe "$InstallDir\unins000.exe" '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
    Check ($code -eq 0) "卸载退出码 = $code"
    Start-Sleep -Seconds 3
    foreach ($rel in @("runtime\python\python.exe", "bin\node\node.exe", "app\server.py", "app\tray.py")) {
        if (Test-Path (Join-Path $InstallDir $rel)) { Start-Sleep -Seconds 2 }
        Check (-not (Test-Path (Join-Path $InstallDir $rel))) "$rel 已删除"
    }
    Check (-not (Test-Path $regKey)) "卸载注册表项已清"
    Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
}
foreach ($p in @($HomeDir, $Stage)) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue }

Step "把用户原有的快捷方式放回去（安装/卸载会覆盖同一路径）"
foreach ($lnk in @($lnkStartup, $lnkDesktop)) {
    $bak = $script:lnkBackup[$lnk]
    if ($bak -and (Test-Path -LiteralPath $bak)) {
        Copy-Item -LiteralPath $bak -Destination $lnk -Force
        Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue
        Ok "已恢复 $lnk"
    } elseif (-not (Test-Path -LiteralPath $lnk)) {
        Write-Host "  注意：$lnk 原本就不存在，保持不存在" -ForegroundColor Yellow
    }
}

Write-Host ""
if ($script:fails -eq 0) { Write-Host "全部通过" -ForegroundColor Green; exit 0 }
Write-Host "有 $($script:fails) 项失败" -ForegroundColor Red
exit 1
