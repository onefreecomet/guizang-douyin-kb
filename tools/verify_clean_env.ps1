<#
归藏 · 干净环境验证（本机能做到的最接近「新机器」的验证）

为什么需要：安装包在开发者机器上跑通，不代表在别人机器上跑通——
本机有系统 Python、有 node、有 .venv、有一堆 PATH，随便漏点东西都会「碰巧能用」。

这个脚本做两件事：
  1. 静默装到临时目录，**把环境剥干净**再跑：
     PATH 只留 System32、清掉 PYTHON* / PIP* / NODE_*、把 USERPROFILE/APPDATA/
     LOCALAPPDATA/HOME 指到空目录，模拟一台什么都没装的新 Windows。
  2. 在这套环境里逐项验：便携 Python、内置 yt-dlp、内置 node、app 模块可导入、
     服务能起、五个 HTTP 端点正常、体检只差 cookies / Obsidian 库、
     卸载能把程序文件删干净。

注意：这不是真正的虚拟机/沙盒验证（本机 Windows 沙盒功能未启用、也没有硬件虚拟化），
只证明「不依赖本机已有的开发环境」。真机验证仍需一台干净 Windows。

用法：
  powershell -ExecutionPolicy Bypass -File tools\verify_clean_env.ps1
  powershell -ExecutionPolicy Bypass -File tools\verify_clean_env.ps1 -Setup dist\归藏Setup-1.0.0.exe -Keep
#>
[CmdletBinding()]
param(
    [string]$Setup = "",
    [string]$Dir = "C:\gz-clean-test",
    [string]$FakeHome = "C:\gz-clean-home",
    [int]$Port = 8798,
    [switch]$Keep
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot
$fails = New-Object System.Collections.Generic.List[string]
$checks = 0

function Head($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Ok($m)   { $script:checks++; Write-Host "  [OK] $m" -ForegroundColor Green }
function Bad($m)  { $script:checks++; $script:fails.Add($m); Write-Host "  [!!] $m" -ForegroundColor Red }
function Say($m)  { Write-Host "  $m" }

function Assert-True([bool]$cond, [string]$what) {
    if ($cond) { Ok $what } else { Bad $what }
}

# ---------------------------------------------------------------- 0. 找安装包
if (-not $Setup) {
    $cand = Get-ChildItem (Join-Path $Root "dist") -Filter "*.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $cand) { Write-Host "dist\ 里没有 exe" -ForegroundColor Red; exit 1 }
    $Setup = $cand.FullName
}
if (-not (Test-Path $Setup)) { Write-Host "找不到 $Setup" -ForegroundColor Red; exit 1 }

Write-Host "归藏 · 干净环境验证"
Say "安装包: $Setup"
Say "安装到: $Dir"
Say "假用户: $FakeHome"

# ---------------------------------------------------------------- 1. 清场 + 装
Head "1/5 清场并静默安装"
foreach ($p in @($Dir, $FakeHome)) {
    if (Test-Path $p) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue }
}
New-Item -ItemType Directory -Force -Path $FakeHome | Out-Null

$log = Join-Path $env:TEMP "gz-clean-install.log"
if (Test-Path $log) { Remove-Item $log -Force }

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $Setup
$psi.Arguments = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /DIR=`"$Dir`" /LOG=`"$log`""
$psi.UseShellExecute = $false
$p = [System.Diagnostics.Process]::Start($psi)
if (-not $p.WaitForExit(300000)) { Bad "安装超时（5 分钟）"; }
elseif ($p.ExitCode -ne 0) { Bad "安装退出码 $($p.ExitCode)（日志 $log）" }
else { Ok "安装退出码 0" }

$py  = Join-Path $Dir "runtime\python\python.exe"
$pyw = Join-Path $Dir "runtime\python\pythonw.exe"
$node = Join-Path $Dir "bin\node\node.exe"
$appDir = Join-Path $Dir "app"
Assert-True (Test-Path $py)   "便携 Python 在包内"
Assert-True (Test-Path $pyw)  "pythonw.exe 在包内"
Assert-True (Test-Path $node) "node.exe 在包内"
Assert-True (Test-Path (Join-Path $appDir "server.py")) "app\server.py 在包内"
Assert-True (Test-Path (Join-Path $appDir "tray.py"))   "app\tray.py 在包内"

# ---------------------------------------------------------------- 2. 剥干净环境
Head "2/5 构造「新机器」环境"
$cleanEnv = @{
    "SystemRoot"        = $env:SystemRoot
    "windir"            = $env:windir
    "ComSpec"           = $env:ComSpec
    "TEMP"              = $env:TEMP
    "TMP"               = $env:TMP
    "USERPROFILE"       = $FakeHome
    "APPDATA"           = (Join-Path $FakeHome "AppData\Roaming")
    "LOCALAPPDATA"      = (Join-Path $FakeHome "AppData\Local")
    "HOME"              = $FakeHome
    "HOMEDRIVE"         = (Split-Path -Qualifier $FakeHome)
    "HOMEPATH"          = (Split-Path -NoQualifier $FakeHome)
    "PATH"              = "$env:SystemRoot\System32;$env:SystemRoot"
    "GUIZANG_HOME"      = (Join-Path $FakeHome "guizang-data")
    "PYTHONIOENCODING"  = "utf-8"
}
foreach ($d in @($cleanEnv.APPDATA, $cleanEnv.LOCALAPPDATA, $cleanEnv.GUIZANG_HOME)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Say "PATH 只剩: $($cleanEnv.PATH)"
Say "USERPROFILE: $FakeHome"
Say "数据目录: $($cleanEnv.GUIZANG_HOME)"

# ---------- 剥净环境 ----------
# 坑：PS 5.1 下 ProcessStartInfo.EnvironmentVariables 既取不到（NULL）又不能赋值（ReadOnly），
# 子进程会安静地继承完整本机环境，验证就假了。
# 可靠办法：临时把「当前进程」的环境变量换成剥净的一套 → 起子进程（继承这份）→ 立刻还原。
function Invoke-CleanEnv([scriptblock]$Block) {
    $backup = @{}
    foreach ($e in [System.Environment]::GetEnvironmentVariables().GetEnumerator()) { $backup[$e.Key] = $e.Value }
    try {
        foreach ($k in @($backup.Keys)) { Remove-Item ("Env:" + $k) -ErrorAction SilentlyContinue }
        foreach ($k in $cleanEnv.Keys) { Set-Item ("Env:" + $k) ([string]$cleanEnv[$k]) -ErrorAction SilentlyContinue }
        return (& $Block)
    } finally {
        foreach ($e in [System.Environment]::GetEnvironmentVariables().GetEnumerator()) {
            Remove-Item ("Env:" + $e.Key) -ErrorAction SilentlyContinue
        }
        foreach ($k in $backup.Keys) { Set-Item ("Env:" + $k) ([string]$backup[$k]) -ErrorAction SilentlyContinue }
    }
}

function RunIn([string]$exe, [string[]]$exeArgs, [string]$workDir = $Dir) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = ($exeArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join " "
    $psi.WorkingDirectory = $workDir
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    return (Invoke-CleanEnv {
        $pr = [System.Diagnostics.Process]::Start($psi)
        $out = [string]$pr.StandardOutput.ReadToEnd()
        $err = [string]$pr.StandardError.ReadToEnd()
        $pr.WaitForExit(120000) | Out-Null
        [pscustomobject]@{ Code = $pr.ExitCode; Out = $out.Trim(); Err = $err.Trim() }
    })
}
# ---------------------------------------------------------------- 3. 运行时
Head "3/5 剥净环境里的运行时自检"
$r = RunIn $py @("-c", "import sys;print(sys.version.split()[0]);print(sys.executable)")
if ($r.Code -eq 0) {
    $lines = $r.Out -split "`r?`n"
    if ($lines[0]) { Ok "python $($lines[0])" } else { Bad "python 没输出版本" }
    Assert-True ($lines[1] -like "$Dir*") "sys.executable 指向安装目录"
} else { Bad "python 起不来: $($r.Err)" }

$r = RunIn $py @("-m", "yt_dlp", "--version")
Assert-True ($r.Code -eq 0 -and $r.Out -match '^\d{4}\.') "内置 yt-dlp $($r.Out)"

$r = RunIn $py @("-c", "import process_tree, paths, doctor;print('ok')")
Assert-True ($r.Code -eq 0 -and $r.Out -eq "ok") "app 模块可导入（process_tree / paths / doctor）"

$r = RunIn $py @("-c", "import yt_dlp;print(yt_dlp.version.__version__)")
Assert-True ($r.Code -eq 0) "runtime\lib 里的 yt-dlp 可 import（$($r.Out)）"

$r = RunIn $node @("--version")
Assert-True ($r.Code -eq 0 -and $r.Out -match '^v\d+') "内置 node $($r.Out)"

# ---------------------------------------------------------------- 4. 起服务 + 端点
Head "4/5 剥净环境里起服务并探端点"
$srv = New-Object System.Diagnostics.ProcessStartInfo
$srv.FileName = $pyw
$srv.Arguments = "`"$(Join-Path $appDir 'server.py')`" --port $Port"
$srv.WorkingDirectory = $Dir
$srv.UseShellExecute = $false
$proc = Invoke-CleanEnv { [System.Diagnostics.Process]::Start($srv) }

$base = "http://127.0.0.1:$Port"
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $resp = Invoke-WebRequest -Uri "$base/api/ping" -TimeoutSec 2 -UseBasicParsing
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}
Assert-True $ready "服务在剥净环境里起来了（/api/ping 200）"

if ($ready) {
    foreach ($path in @("/", "/favicon-32.png", "/favicon.ico", "/assets/logo-mark.png")) {
        try {
            $resp = Invoke-WebRequest -Uri "$base$path" -TimeoutSec 5 -UseBasicParsing
            Assert-True ($resp.StatusCode -eq 200) "$path 200（$($resp.RawContentLength) B）"
            if ($path -eq "/") {
                $html = [System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
                Assert-True ($html -match "brand-v2") "首页带品牌样式层 brand-v2"
                Assert-True ($html -match "冰临城下") "首页带作者署名"
            }
        } catch { Bad "$path 请求失败: $($_.Exception.Message)" }
    }
}

Head "4b/5 剥净环境里跑体检"
$r = RunIn $py @("app\doctor.py")
$doctorOut = ($r.Out + " " + $r.Err)
Say "doctor 退出码 $($r.Code)"
Assert-True ($doctorOut -match "cookies") "体检提到 cookies"
Assert-True ($doctorOut -match "Obsidian|vault") "体检提到 Obsidian 库"
# 全新机器只该差这两项；其它关键项（python/node/yt-dlp/浏览器）必须过
Assert-True ($doctorOut -notmatch "\[!!\]\s*(Python|Node|yt-dlp)") "Python/Node/yt-dlp 三项在体检里是过的"

# ---------------------------------------------------------------- 5. 收尾
Head "5/5 卸载并确认删干净"
if ($proc -and -not $proc.HasExited) { try { $proc.Kill() } catch { } }

if ($Keep) {
    Say "-Keep 指定，跳过卸载；安装目录留在 $Dir"
} else {
    $un = Get-ChildItem -Path $Dir -Filter "unins*.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $un) {
        Write-Host "  找不到卸载程序，改用注册表项" -ForegroundColor Yellow
        $key = Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall" -ErrorAction SilentlyContinue |
            ForEach-Object { Get-ItemProperty $_.PSPath } |
            Where-Object { $_.DisplayName -like "*归藏*" } | Select-Object -First 1
        if ($key) {
            $us = $key.QuietUninstallString
            if ($us) {
                $exe, $rest = $us.Split('"').Where({ $_ -ne '' })
                $p2 = [System.Diagnostics.Process]::Start($exe, $rest)
                $p2.WaitForExit(180000) | Out-Null
            }
        }
    } else {
        $p2 = [System.Diagnostics.Process]::Start($un.FullName, "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART")
        $p2.WaitForExit(180000) | Out-Null
        Assert-True ($p2.ExitCode -eq 0) "卸载退出码 0"
    }
    Start-Sleep -Milliseconds 800
    Assert-True (-not (Test-Path (Join-Path $Dir "runtime\python\python.exe"))) "runtime 已删除"
    Assert-True (-not (Test-Path (Join-Path $Dir "bin\node\node.exe"))) "bin\node 已删除"
    Assert-True (-not (Test-Path (Join-Path $Dir "app\server.py"))) "app 已删除"
    Assert-True (-not (Test-Path (Join-Path $Dir "app\tray.py"))) "app\tray.py 已删除"
    Remove-Item $FakeHome -Recurse -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- 结论
Write-Host ""
if ($fails.Count -eq 0) {
    Write-Host "全部通过：$checks 项检查，0 失败" -ForegroundColor Green
    Write-Host "（说明：这不等于真机验证——本机没有可用的 Windows 沙盒/虚拟化，只证明不依赖本机开发环境）"
    exit 0
}
Write-Host "有 $($fails.Count) 项失败（共 $checks 项）：" -ForegroundColor Red
foreach ($f in $fails) { Write-Host "  - $f" -ForegroundColor Red }
exit 1
