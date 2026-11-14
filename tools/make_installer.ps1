<#
归藏 · 安装包构建（B 阶段）
做三件事：
  1. 确保 runtime\ 便携环境在（没有就先跑 make_portable.ps1）
  2. 把要发出去的东西拼进 build\payload（不含 .venv / work / 测试 / 缓存）
  3. 用 Inno Setup 编出 dist\归藏Setup-<版本>.exe
用法：
  powershell -ExecutionPolicy Bypass -File tools\make_installer.ps1
  powershell -ExecutionPolicy Bypass -File tools\make_installer.ps1 -Version 1.0.1 -SkipCompile
#>
[CmdletBinding()]
param(
    # 留空 = 用 tools\version.iss 里的版本；传了就顺手写回 version.iss（版本号唯一来源）
    [string]$Version = "",
    [switch]$SkipPortable,
    [switch]$SkipCompile,
    [string]$Iscc = "",
    # 产物输出到别处（不传就是 dist\）
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root    = Split-Path -Parent $PSScriptRoot
$Payload = Join-Path $Root "build\payload"
$Dist    = if ($OutDir) { $OutDir } else { Join-Path $Root "dist" }
$Runtime = Join-Path $Root "runtime"
$VerFile = Join-Path $PSScriptRoot "version.iss"

function Head($m) { Write-Host ""; Write-Host "[$m]" -ForegroundColor Cyan }
function Say($m)  { Write-Host "  $m" }
function Die($m)  { Write-Host "  $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- 0. 版本号
if (-not (Test-Path $VerFile)) { Die "缺 tools\version.iss（版本号唯一来源）" }
$verText = [System.IO.File]::ReadAllText($VerFile)
$m = [regex]::Match($verText, '#define\s+AppVersion\s+"([^"]+)"')
if (-not $m.Success) { Die "tools\version.iss 里找不到 #define AppVersion `"...`"" }
$fileVer = $m.Groups[1].Value
if ($Version) {
    if ($Version -ne $fileVer) {
        $verText = [regex]::Replace($verText, '#define\s+AppVersion\s+"[^"]*"', "#define AppVersion `"$Version`"")
        [System.IO.File]::WriteAllText($VerFile, $verText, (New-Object System.Text.UTF8Encoding($true)))
        Say "已把 tools\version.iss 的版本号 $fileVer -> $Version"
    }
} else {
    $Version = $fileVer
}

Write-Host "归藏 · 安装包构建"
Write-Host "版本    : $Version（来源 tools\version.iss）"
Write-Host "项目目录: $Root"
Write-Host "输出到  : $Dist"

# ---------------------------------------------------------------- 1. 便携环境
Head "1/3 便携运行环境"
if (-not $SkipPortable -and -not (Test-Path (Join-Path $Runtime "python\python.exe"))) {
    Say "runtime 不存在，先构建"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "make_portable.ps1")
    if ($LASTEXITCODE -ne 0) { Die "make_portable.ps1 失败" }
}
if (-not (Test-Path (Join-Path $Runtime "python\python.exe"))) { Die "runtime\python\python.exe 还是不存在" }
$pyMB = (Get-ChildItem $Runtime -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Say ("runtime {0:N1} MB" -f $pyMB)

# ---------------------------------------------------------------- 2. 拼 payload
Head "2/3 拼装 payload（只带运行需要的东西）"
if (Test-Path $Payload) { Remove-Item $Payload -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Payload | Out-Null

function Copy-Part($rel, [switch]$Optional) {
    $src = Join-Path $Root $rel
    if (-not (Test-Path $src)) {
        if ($Optional) { Say "跳过 $rel（没有）"; return }
        Die "缺少 $rel"
    }
    $dst = Join-Path $Payload $rel
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
    Copy-Item $src $dst -Recurse -Force
    Say "带 $rel"
}

Copy-Part "app"
Copy-Part "bin"
Copy-Part "runtime"
Copy-Part "references" -Optional
Copy-Part "scripts"    -Optional
Copy-Part "INSTALL.md"

# tools 只带「用户可能真会用到的两个脚本」，构建脚本和缓存不发给用户
$toolsDst = Join-Path $Payload "tools"
New-Item -ItemType Directory -Force -Path $toolsDst | Out-Null
foreach ($t in @("bootstrap.ps1", "make_portable.ps1")) {
    $src = Join-Path $PSScriptRoot $t
    if (Test-Path $src) { Copy-Item $src (Join-Path $toolsDst $t) -Force }
}
Say "带 tools\（bootstrap.ps1 / make_portable.ps1）"

# 清掉一切不该发的东西
Get-ChildItem $Payload -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
Get-ChildItem $Payload -Recurse -File -Include "*.pyc", "*.pyo" -ErrorAction SilentlyContinue |
    Remove-Item -Force
Get-ChildItem $Payload -Recurse -Directory -Filter ".cache" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
foreach ($junk in @("app\work", "work", ".venv", "tests", "docs", "dist", "build")) {
    $p = Join-Path $Payload $junk
    if (Test-Path $p) { Remove-Item $p -Recurse -Force; Say "剔除 $junk" }
}

$sizeMB = (Get-ChildItem $Payload -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
$files  = (Get-ChildItem $Payload -Recurse -File | Measure-Object).Count
Say ("payload {0:N1} MB / {1} 个文件" -f $sizeMB, $files)

# ---------------------------------------------------------------- 3. 编译
Head "3/3 编译安装包"
if ($SkipCompile) { Say "按参数跳过编译"; exit 0 }

if (-not $Iscc) {
    $cands = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $Iscc = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) {
    Die "找不到 ISCC.exe。装一个 Inno Setup 6 再来：winget install JRSoftware.InnoSetup"
}
Say "编译器: $Iscc"

$issSrc = Join-Path $PSScriptRoot "installer.iss"
# 语言文件放在 tools\ 下，编译时把它带上（不发给用户）
$zh = Join-Path $PSScriptRoot "ChineseSimplified.isl"
if (-not (Test-Path $zh)) { Die "缺 tools\ChineseSimplified.isl（中文安装界面）" }

New-Item -ItemType Directory -Force -Path $Dist | Out-Null
# 直接编译 installer.iss（版本号走 #include "version.iss"，不再生成临时副本）
& $Iscc "/Qp" "/O$Dist" $issSrc
$code = $LASTEXITCODE
if ($code -ne 0) { Die "ISCC 编译失败（退出码 $code）" }

$setup = Get-ChildItem $Dist -Filter "归藏Setup-$Version.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $setup) { Die "没找到产物 $Dist\归藏Setup-$Version.exe" }
Write-Host ""
Write-Host ("完成：{0}（{1:N1} MB）" -f $setup.FullName, ($setup.Length / 1MB)) -ForegroundColor Green
exit 0
