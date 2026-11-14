<#
归藏 · 便携运行环境构建（B 阶段）
把「不需要用户装 Python」运行起来所需的东西拼进 runtime\：
  runtime\python\   Python 嵌入式发行版（免安装、不写注册表、不碰 PATH）
  runtime\lib\      yt-dlp（纯 Python 包，升级＝整个目录换掉）
  bin\node\         node.exe（yt-dlp 解抖音 JS 挑战用）
用法：
  powershell -ExecutionPolicy Bypass -File tools\make_portable.ps1
  powershell -ExecutionPolicy Bypass -File tools\make_portable.ps1 -PythonVersion 3.13.15 -Force
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.13.15",
    [string]$YtDlpVersion  = "",          # 留空＝装最新
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root    = Split-Path -Parent $PSScriptRoot          # 项目根目录
$Cache   = Join-Path $PSScriptRoot ".cache"
$Runtime = Join-Path $Root "runtime"
$PyDir   = Join-Path $Runtime "python"
$LibDir  = Join-Path $Runtime "lib"
$BinDir  = Join-Path $Root "bin"

function Say($msg)  { Write-Host "  $msg" }
function Head($msg) { Write-Host ""; Write-Host "[$msg]" -ForegroundColor Cyan }
function Die($msg)  { Write-Host "  $msg" -ForegroundColor Red; exit 1 }

function Get-HostPython {
    foreach ($c in @("python", "python3")) {
        $p = Get-Command $c -ErrorAction SilentlyContinue
        if ($p) { return $p.Source }
    }
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    Die "本机找不到 python，没法用 pip 装 yt-dlp。请先装一个 Python 3.10+。"
}

Write-Host "归藏 · 便携运行环境构建"
Write-Host "项目目录: $Root"
Write-Host "Python  : $PythonVersion"

# ---------------------------------------------------------------- 1. Python 嵌入式包
Head "1/4 Python 嵌入式发行版"
New-Item -ItemType Directory -Force -Path $Cache | Out-Null
$zipName = "python-$PythonVersion-embed-amd64.zip"
$zipPath = Join-Path $Cache $zipName
if ((Test-Path $zipPath) -and -not $Force) {
    Say "已有缓存: $zipName"
} else {
    $url = "https://www.python.org/ftp/python/$PythonVersion/$zipName"
    Say "下载 $url"
    try {
        Invoke-WebRequest $url -OutFile $zipPath -UseBasicParsing -TimeoutSec 300
    } catch {
        Die "下载失败：$($_.Exception.Message)`n如果是网络问题，可手动下载后放到 tools\.cache\$zipName"
    }
    Say ("OK    {0:N1} MB" -f ((Get-Item $zipPath).Length / 1MB))
}

if (Test-Path $PyDir) { Remove-Item $PyDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $PyDir | Out-Null
Expand-Archive -Path $zipPath -DestinationPath $PyDir -Force

# 嵌入式发行版默认把 pythonXXX.zip 和 . 放进 sys.path，且注释掉 import site。
# 我们改写成：标准库 + 程序目录 + runtime\lib（放 yt-dlp）+ app（程序模块），并打开 site。
# 注意 ..\..\app 这一条不能少：._pth 一旦存在，Python 就不再自动把「脚本所在目录」放进 sys.path，
# 于是 server.py 里的 `from process_tree import ...` 会 ModuleNotFoundError。
# ._pth 里的相对路径以 python.exe 所在目录为基准，runtime\python 往上两级就是安装根目录。
$verTag = ($PythonVersion -split '\.')[0..1] -join ''
$pthFile = Join-Path $PyDir "python$verTag._pth"
$pth = @"
python$verTag.zip
.
..\lib
..\..\app
import site
"@
Set-Content -Path $pthFile -Value $pth -Encoding ASCII -NoNewline:$false
Say "已写 $([IO.Path]::GetFileName($pthFile))"

$py = Join-Path $PyDir "python.exe"
if (-not (Test-Path $py)) { Die "解压后没有 python.exe，包结构不对" }
Say ("Python " + (& $py -c "import sys;print(sys.version.split()[0])"))

# ---------------------------------------------------------------- 2. yt-dlp
Head "2/4 yt-dlp（装进 runtime\lib，升级就是换这个目录）"
$hostPy = Get-HostPython
if (Test-Path $LibDir) { Remove-Item $LibDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $LibDir | Out-Null
$spec = if ($YtDlpVersion) { "yt-dlp==$YtDlpVersion" } else { "yt-dlp" }
Say "pip install $spec --target runtime\lib"
& $hostPy -m pip install --quiet --upgrade --no-compile --target $LibDir $spec
if ($LASTEXITCODE -ne 0) { Die "pip 安装 yt-dlp 失败" }
Say "yt-dlp " + (& $py -m yt_dlp --version)

# ---------------------------------------------------------------- 3. node
Head "3/4 node.exe"
$nodeDst = Join-Path $BinDir "node"
New-Item -ItemType Directory -Force -Path $nodeDst | Out-Null
$nodeExe = Join-Path $nodeDst "node.exe"
if ((Test-Path $nodeExe) -and -not $Force) {
    Say "已有 node.exe，跳过（要换请加 -Force）"
} else {
    $cands = @()
    $legacy = Join-Path $HOME ".workbuddy\binaries\node\versions"
    if (Test-Path $legacy) {
        $cands += Get-ChildItem $legacy -Directory |
                  Where-Object { Test-Path (Join-Path $_.FullName "node.exe") } |
                  Sort-Object Name -Descending |
                  ForEach-Object { Join-Path $_.FullName "node.exe" }
    }
    $sysNode = Get-Command node -ErrorAction SilentlyContinue
    if ($sysNode) { $cands += $sysNode.Source }
    foreach ($p in @("$env:ProgramFiles\nodejs\node.exe")) { if (Test-Path $p) { $cands += $p } }

    $src = $cands | Select-Object -First 1
    if (-not $src) {
        # 兜底：从 nodejs.org 下 v22 的 zip
        Say "本机没有 node，从 nodejs.org 下载"
        $idx = (Invoke-WebRequest "https://nodejs.org/dist/index.json" -UseBasicParsing).Content | ConvertFrom-Json
        $rel = $idx | Where-Object { $_.version -like "v22.*" } | Select-Object -First 1
        $zip = "node-$($rel.version)-win-x64.zip"
        $zp  = Join-Path $Cache $zip
        if (-not (Test-Path $zp)) {
            Invoke-WebRequest "https://nodejs.org/dist/$($rel.version)/$zip" -OutFile $zp -UseBasicParsing -TimeoutSec 300
        }
        $tmp = Join-Path $Cache "node-extract"
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
        Expand-Archive $zp -DestinationPath $tmp -Force
        $src = Get-ChildItem $tmp -Recurse -Filter node.exe | Select-Object -First 1 -ExpandProperty FullName
    }
    Copy-Item $src $nodeExe -Force
    $lic = Join-Path (Split-Path -Parent $src) "LICENSE"
    if (Test-Path $lic) { Copy-Item $lic (Join-Path $nodeDst "LICENSE") -Force }
}
Say ("node " + (& $nodeExe --version))

# ---------------------------------------------------------------- 4. 自检
Head "4/4 自检"
$env:PYTHONIOENCODING = "utf-8"
& $py (Join-Path $Root "app\doctor.py")
$code = $LASTEXITCODE
Say "体检退出码: $code"
Say ""
Say "=== 体积 ==="
foreach ($d in @($PyDir, $LibDir, $nodeDst)) {
    if (Test-Path $d) {
        $mb = (Get-ChildItem $d -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
        Say ("{0,-40} {1,7:N1} MB" -f (Resolve-Path $d).Path.Replace($Root + "\", ""), $mb)
    }
}
Write-Host ""
Write-Host "完成。启动方式：" -ForegroundColor Green
Write-Host "  runtime\python\pythonw.exe app\launcher.py"
Write-Host "  或 app\start.bat"
exit 0
