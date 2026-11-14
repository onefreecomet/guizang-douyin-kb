<#
安装包冒烟测试（顺序执行，不要和别的安装/卸载并行跑，会互相踩）
  1. 静默装到 C:\gz-inst-test（故意用不带空格的路径，省掉引号解析的坑）
  2. 查文件、查运行时（python / yt-dlp / node 全部来自安装目录）
  3. doctor 体检
  4. 起服务、探接口
  5. 静默卸载，确认删干净
用法：powershell -ExecutionPolicy Bypass -File tools\verify_installer.ps1 [-Port 8799] [-Keep]

坑记录：
  · 安装包不能直接从工作区 D:\...\dist 启动（退出码 1、连日志都不生成），必须先拷到本地盘再跑。
  · 测试里有中文输出，脚本本身要 UTF-8 BOM。
#>
[CmdletBinding()]
param(
    [string]$Setup = "",
    [int]$Port = 8799,
    [switch]$Keep
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Setup) {
    $Setup = (Get-ChildItem (Join-Path $Root "dist") -Filter "归藏Setup-*.exe" |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
if (-not $Setup -or -not (Test-Path $Setup)) { Write-Host "找不到安装包，先跑 tools\make_installer.ps1" -ForegroundColor Red; exit 1 }

$InstallDir = "C:\gz-inst-test"
$HomeDir    = "C:\gz-inst-home"
$Stage      = "C:\gz-setup-stage.exe"
$LogFile    = "C:\gz-install.log"
$lnkStartup = Join-Path ([Environment]::GetFolderPath('Startup')) "归藏守护.lnk"
$lnkDesktop = Join-Path ([Environment]::GetFolderPath('Desktop')) "归藏.lnk"
$regKey     = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8C3F1A64-2B7D-4E19-9A55-6D2E7C4B18A1}_is1"

$script:fails = 0
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

Write-Host "安装包: $Setup"
Write-Host ("体积  : {0:N1} MB" -f ((Get-Item $Setup).Length / 1MB))

Step "清场"
foreach ($p in @($InstallDir, $HomeDir)) { if (Test-Path $p) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue } }
foreach ($p in @($lnkStartup, $lnkDesktop, $LogFile, $Stage)) { if (Test-Path $p) { Remove-Item $p -Force -ErrorAction SilentlyContinue } }
if (Test-Path $regKey) { Remove-Item $regKey -Recurse -Force -ErrorAction SilentlyContinue }
Ok "干净了"

Step "静默安装到 $InstallDir"
Copy-Item $Setup $Stage -Force
$code = RunExe $Stage ('/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOICONS /LANG=chinese ' +
    '/MERGETASKS="!desktopicon,!startup" /LOG=' + $LogFile + ' /DIR=' + $InstallDir)
Check ($code -eq 0) "安装退出码 = $code"
Check (Test-Path "$InstallDir\unins000.exe") "装到了指定目录"
Check (-not (Test-Path $lnkStartup)) "没建开机自启快捷方式"
Check (-not (Test-Path $lnkDesktop)) "没建桌面快捷方式"

Step "文件清单"
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

Step "运行时（必须全部来自安装目录）"
$py   = "$InstallDir\runtime\python\python.exe"
$pyw  = "$InstallDir\runtime\python\pythonw.exe"
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
Check ($v -match 'ok') "app 模块可导入（_pth 里 ..\..\app 生效）"

Step "doctor 体检（新装机器应该只剩 cookies / 库两项）"
New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
$env:GUIZANG_HOME = $HomeDir
$doc = (& $py "$InstallDir\app\doctor.py" 2>&1 | Out-String)
$docCode = $LASTEXITCODE
Check ($doc -match 'Python' -and $doc -match 'yt-dlp') "doctor 正常输出"
Check ($docCode -eq 1) "doctor 退出码 1（有关键项没配，符合全新安装）"
($doc -split "`n") | Select-Object -First 12 | ForEach-Object { Write-Host "    $_" }

Step "起服务并探接口（端口 $Port）"
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $pyw
$psi.Arguments = '"' + $InstallDir + '\app\server.py" --port ' + $Port
$psi.WorkingDirectory = $InstallDir
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$srv = [System.Diagnostics.Process]::Start($psi)
$up = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try { if ((Invoke-WebRequest "http://127.0.0.1:$Port/api/ping" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { $up = $true; break } } catch { }
}
Check $up "/api/ping 通"
if ($up) {
    foreach ($u in @("/", "/assets/logo-mark.png", "/favicon-32.png", "/favicon.ico")) {
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$Port$u" -UseBasicParsing -TimeoutSec 6
            Check ($r.StatusCode -eq 200) ("{0} {1}（{2} 字节）" -f $u, $r.StatusCode, $r.RawContentLength)
        } catch { Bad "$u 不通" }
    }
    $r = Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing
    Check ($r.Content -match 'brand-v2') "首页带新样式层"
    Check ($r.Content -match 'by 冰临城下') "首页带作者署名"
}
try { $srv.Kill() } catch { }
Get-Process python, pythonw -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir) } |
    ForEach-Object { try { $_.Kill() } catch { } }
Start-Sleep -Milliseconds 800
Remove-Item Env:GUIZANG_HOME -ErrorAction SilentlyContinue

if ($Keep) {
    Write-Host ""
    Write-Host "按 -Keep 保留 $InstallDir 不卸载" -ForegroundColor Yellow
} else {
    Step "静默卸载"
    $code = RunExe "$InstallDir\unins000.exe" '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
    Check ($code -eq 0) "卸载退出码 = $code"
    Start-Sleep -Seconds 3
    foreach ($rel in @("runtime\python\python.exe", "bin\node\node.exe", "app\server.py", "app\tray.py")) {
        if (Test-Path (Join-Path $InstallDir $rel)) { Start-Sleep -Seconds 2 }
        Check (-not (Test-Path (Join-Path $InstallDir $rel))) "$rel 已删除"
    }
    Check (-not (Test-Path $regKey)) "卸载注册表项已清"
    if (Test-Path (Join-Path $InstallDir "work")) { Write-Host "  注意：work 用户数据被保留" }
    Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
}
foreach ($p in @($HomeDir, $Stage)) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host ""
if ($script:fails -eq 0) { Write-Host "全部通过" -ForegroundColor Green; exit 0 }
Write-Host "有 $($script:fails) 项失败" -ForegroundColor Red
exit 1
