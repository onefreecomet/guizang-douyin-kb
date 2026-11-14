<#
  归藏 · 一键准备环境（A 阶段：让「换台电脑也能装起来」成立）

  用法：
      powershell -ExecutionPolicy Bypass -File tools\bootstrap.ps1

  可选参数：
      -SkipNode       不准备 node（已有 node，或暂时不打算下载视频）
      -SkipShortcut   不创建桌面快捷方式 / 开机自启
      -Yes            全自动，不询问

  它会做四件事，每一步都可重复执行（已经好的就跳过）：
      1. 找 Python 3.10+ 建 .venv，装 yt-dlp
      2. 把 node.exe 放进 bin\node\（yt-dlp 解抖音要用；优先本地复制，其次官网下载）
      3. 建桌面快捷方式与开机自启
      4. 跑一遍体检，告诉你还差什么

  它不会碰你的 cookies、Obsidian 库和已有配置。
#>
param(
    [switch]$SkipNode,
    [switch]$SkipShortcut,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Root     = Split-Path -Parent $PSScriptRoot
$Venv     = Join-Path $Root '.venv'
$Py       = Join-Path $Venv 'Scripts\python.exe'
$Bin      = Join-Path $Root 'bin'
$NodeDir  = Join-Path $Bin 'node'
$NodeExe  = Join-Path $NodeDir 'node.exe'

function Step($n, $text) { Write-Host ""; Write-Host "[$n/4] $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "      OK   $text" -ForegroundColor Green }
function Warn($text) { Write-Host "      注意 $text" -ForegroundColor Yellow }
function Die($text)  { Write-Host "      失败 $text" -ForegroundColor Red; exit 1 }

# 问一个解释器要版本号，拿不到或低于 3.10 就返回 $null
function Get-PythonVersion($exe, $prefix) {
    if (-not $exe) { return $null }
    try {
        $out = & $exe @($prefix + @('-c', 'import sys;print(sys.version_info[0]*100+sys.version_info[1])')) 2>$null
    } catch { return $null }
    if (-not $out) { return $null }
    $num = 0
    if ([int]::TryParse((@($out) -join '').Trim(), [ref]$num) -and $num -ge 310) { return $num }
    return $null
}

# 正常机器上没配 PATH 也能找到 Python 的地方，按优先级排
function Find-SystemPython {
    $tries = @()
    foreach ($v in @('3.13', '3.12', '3.11', '3.10')) {
        $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($pyLauncher) { $tries += ,@($pyLauncher.Source, @("-$v")) }
    }
    foreach ($name in @('python', 'python3')) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c) { $tries += ,@($c.Source, @()) }
    }
    $globs = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
        (Join-Path $env:APPDATA 'uv\python\cpython-3*\python.exe'),
        (Join-Path $env:ProgramFiles 'Python3*\python.exe'),
        'C:\Python3*\python.exe'
    )
    foreach ($g in $globs) {
        foreach ($f in (Get-ChildItem $g -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
            $tries += ,@($f.FullName, @())
        }
    }
    $best = $null; $bestScore = 0
    foreach ($t in $tries) {
        $num = Get-PythonVersion $t[0] $t[1]
        if (-not $num) { continue }
        # 首选 3.10~3.13（这几档是跑过全套测试的）；更新的版本能用但不优先
        $minor = $num % 100
        if ($num -le 313) { $score = 1000 + $minor } else { $score = 500 - $minor }
        if ($score -gt $bestScore) { $bestScore = $score; $best = $t; $bestNum = $num }
    }
    if ($best) { return @{ Exe = $best[0]; Prefix = $best[1]; Num = $bestNum } }
    return $null
}

function Find-Uv {
    $c = Get-Command uv -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $guess = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path $guess) { return $guess }
    return $null
}

Write-Host "归藏 · 环境准备" -ForegroundColor White
Write-Host "安装目录: $Root"

# ---------------------------------------------------------------- 1. Python
Step 1 "Python 3.10+ / yt-dlp"

if (Test-Path $Py) {
    Ok "已有虚拟环境，直接用: $Venv"
} else {
    $found = Find-SystemPython
    if ($found) {
        Ok ("找到 Python {0} : {1}" -f [math]::Floor($found.Num / 100), ($found.Num % 100))
        Warn "正在创建虚拟环境（第一次要几十秒）"
        & $found.Exe @($found.Prefix + @('-m', 'venv', $Venv))
    } else {
        $uv = Find-Uv
        if ($uv) {
            Warn "系统里没有 Python，改用 uv 下载 3.13（约 30 MB）"
            & $uv python install 3.13
            $cand = Get-ChildItem (Join-Path $env:APPDATA 'uv\python\cpython-3.13*\python.exe') -ErrorAction SilentlyContinue |
                    Sort-Object FullName -Descending | Select-Object -First 1
            if ($cand) {
                Warn "正在创建虚拟环境"
                & $cand.FullName -m venv $Venv
            }
        }
    }

    if (-not (Test-Path $Py)) {
        Write-Host @"
      失败 没有找到 Python 3.10+。三种办法任选：
            1. 装 Python：https://www.python.org/downloads/windows/
               安装时务必勾选 "Add python.exe to PATH"，然后重跑本脚本。
            2. 装 uv：`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
               再重跑本脚本，它会自动用 uv 装 Python。
            3. 已经有 Python 但不在 PATH 里：手动指一下
               `$env:PYTHON = "C:\完整路径\python.exe"` 后重跑。
"@ -ForegroundColor Red
        exit 1
    }
    Ok "虚拟环境已建好: $Venv"
}

# 允许用户用 $env:PYTHON 指定
if ($env:PYTHON -and (Test-Path $env:PYTHON) -and -not (Test-Path $Py)) {
    & $env:PYTHON -m venv $Venv
}

Warn "安装 / 升级 yt-dlp"
& $Py -m pip install --upgrade pip --quiet --disable-pip-version-check
& $Py -m pip install --upgrade yt-dlp --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Die "pip 安装 yt-dlp 失败（检查网络或代理）" }
$ytver = (& $Py -m yt_dlp --version) 2>$null
Ok "yt-dlp $ytver"

# ---------------------------------------------------------------- 2. node
Step 2 "node（下载抖音视频要用）"

if ($SkipNode) {
    Warn "按参数跳过 node 准备"
} elseif (Test-Path $NodeExe) {
    Ok "已有 node: $NodeExe"
} else {
    New-Item -ItemType Directory -Force -Path $NodeDir | Out-Null
    $copied = $false

    $localNode = Get-Command node -ErrorAction SilentlyContinue
    if ($localNode) {
        Copy-Item $localNode.Source $NodeExe -Force
        $copied = $true
        Ok "从本机 node 复制: $($localNode.Source)"
    }

    if (-not $copied) {
        $legacyBase = Join-Path $env:USERPROFILE '.workbuddy\binaries\node\versions'
        if (Test-Path $legacyBase) {
            $legacy = Get-ChildItem $legacyBase -Directory |
                      Where-Object { Test-Path (Join-Path $_.FullName 'node.exe') } |
                      Sort-Object Name -Descending | Select-Object -First 1
            if ($legacy) {
                Copy-Item (Join-Path $legacy.FullName 'node.exe') $NodeExe -Force
                $copied = $true
                Ok "从旧环境复制: $($legacy.FullName)"
            }
        }
    }

    if (-not $copied) {
        $candidates = @(
            (Join-Path $env:PROGRAMFILES 'nodejs\node.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'nodejs\node.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\nodejs\node.exe')
        )
        foreach ($c in $candidates) {
            if ($c -and (Test-Path $c)) {
                Copy-Item $c $NodeExe -Force
                $copied = $true
                Ok "从安装目录复制: $c"
                break
            }
        }
    }

    if (-not $copied) {
        try {
            Warn "本机没有现成 node，正在从 nodejs.org 下载 v22 LTS（约 30 MB）"
            $index = Invoke-RestMethod -Uri 'https://nodejs.org/dist/index.json' -TimeoutSec 30
            $entry = $index | Where-Object { $_.version -like 'v22.*' } | Select-Object -First 1
            if (-not $entry) { throw "拿不到 v22 版本号" }
            $ver = $entry.version
            $zip = Join-Path $env:TEMP "node-$ver-win-x64.zip"
            $url = "https://nodejs.org/dist/$ver/node-$ver-win-x64.zip"
            Invoke-WebRequest -Uri $url -OutFile $zip -TimeoutSec 600
            $tmp = Join-Path $env:TEMP "guizang-node-$ver"
            if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            Copy-Item (Join-Path $tmp "node-$ver-win-x64\node.exe") $NodeExe -Force
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
            $copied = $true
            Ok "已下载 node $ver"
        } catch {
            Warn "自动下载 node 失败：$($_.Exception.Message)"
            Warn "手动办法：去 https://nodejs.org/zh-cn/download 下 Windows 免安装版，"
            Warn "把里面的 node.exe 放到: $NodeExe"
        }
    }
}

if (Test-Path $NodeExe) {
    $nv = (& $NodeExe --version) 2>$null
    Ok "node $nv"
}

# ---------------------------------------------------------------- 3. 快捷方式
Step 3 "桌面快捷方式 / 开机自启"

if ($SkipShortcut) {
    Warn "按参数跳过"
} else {
    & $Py (Join-Path $Root 'app\create_shortcut.py') '--startup'
    if ($LASTEXITCODE -eq 0) { Ok "快捷方式已创建" } else { Warn "快捷方式创建失败，可稍后手动跑 app\create_shortcut.py" }
}

# ---------------------------------------------------------------- 4. 体检
Step 4 "体检"
Write-Host ""
$env:PYTHONIOENCODING = 'utf-8'
& $Py (Join-Path $Root 'app\doctor.py')
$doctorCode = $LASTEXITCODE

Write-Host ""
if ($doctorCode -eq 0) {
    Write-Host "准备完成。双击桌面的「归藏」就能用。" -ForegroundColor Green
} else {
    Write-Host "环境基本就位，但还有关键项没过（见上面打叉的那几行）。" -ForegroundColor Yellow
    Write-Host "照着提示补齐后再跑一次本脚本即可，已经好的步骤会自动跳过。" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "常用命令：" -ForegroundColor White
Write-Host "  体检    .venv\Scripts\python.exe app\doctor.py"
Write-Host "  启动    .venv\Scripts\pythonw.exe app\launcher.py"
Write-Host "  测试    .venv\Scripts\python.exe -m unittest discover -s tests"
exit $doctorCode
