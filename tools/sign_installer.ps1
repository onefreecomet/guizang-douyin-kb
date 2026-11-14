<#
归藏 · 给安装包做代码签名（可选）

为什么需要：Windows 对没有 Authenticode 签名的 exe 会在首次运行时弹
「Windows 已保护你的电脑」（SmartScreen）。签名之后，证书受信时这个提示就消失。

没有证书也能用，只是每次首启要手动点「更多信息 → 仍要运行」。证书有两种：
  - OV（组织/个人验证）证书：便宜，但新证书仍要累积下载声誉，初期可能还弹
  - EV（扩展验证）证书：贵，签完立刻不弹，需要 USB 硬件令牌
这里是「拿到证书之后怎么签」，不是「怎么买证书」。

用法：
  # 1) 用 pfx 文件（比如从证书机构下载的 .pfx）
  $env:GZ_PFX_PASSWORD = "你的密码"
  powershell -ExecutionPolicy Bypass -File tools\sign_installer.ps1 `
      -Pfx "D:\certs\guizang.pfx"

  # 2) 用已装进证书库的证书（EV 令牌插入后常见）
  powershell -ExecutionPolicy Bypass -File tools\sign_installer.ps1 `
      -Thumbprint "A1B2C3..."

  # 3) 不传参数 = 只看当前签名状态
  powershell -ExecutionPolicy Bypass -File tools\sign_installer.ps1
#>
[CmdletBinding()]
param(
    [string]$Setup = "",
    [string]$Thumbprint = "",
    [string]$Pfx = "",
    [string]$PfxPassword = "",
    [string]$Timestamp = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot

function Say($m) { Write-Host "  $m" }
function Die($m) { Write-Host "  $m" -ForegroundColor Red; exit 1 }

if (-not $Setup) {
    $cand = Get-ChildItem (Join-Path $Root "dist") -Filter "*.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $cand) { Die "dist\ 里没有 exe，先用 tools\make_installer.ps1 打一个" }
    $Setup = $cand.FullName
}
if (-not (Test-Path $Setup)) { Die "找不到 $Setup" }

Write-Host "归藏 · 安装包签名"
Say "目标: $Setup"

# ------------------------------------------------------------------ 现状
$sig = Get-AuthenticodeSignature $Setup
Say "当前状态: $($sig.Status)  $($sig.StatusMessage)"
if ($sig.SignerCertificate) {
    Say "签名者: $($sig.SignerCertificate.Subject)"
}

if (-not $Thumbprint -and -not $Pfx) {
    Write-Host ""
    Write-Host "没提供证书，跳过签名。真要签：-Pfx <文件> 或 -Thumbprint <指纹>" -ForegroundColor Yellow
    Write-Host "未签名的代价：别人首次运行会看到 SmartScreen 提示，需点「更多信息 → 仍要运行」。"
    exit 0
}

# ------------------------------------------------------------------ 找 signtool
$signtool = (Get-Command signtool.exe -ErrorAction SilentlyContinue).Source
if (-not $signtool) {
    $roots = @("$env:ProgramFiles(x86)\Windows Kits\10\bin", "$env:ProgramFiles\Windows Kits\10\bin")
    foreach ($r in $roots) {
        if (Test-Path $r) {
            $signtool = Get-ChildItem $r -Recurse -Filter "signtool.exe" -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match "\\x64\\" } |
                Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
            if ($signtool) { break }
        }
    }
}
if (-not $signtool) {
    Die "找不到 signtool.exe。装 Windows SDK 的「Windows SDK Signing Tools」，或改用 Inno Setup 自带的 SignTool 配置。"
}
Say "signtool: $signtool"

# ------------------------------------------------------------------ 签名
if ($Pfx) {
    if (-not (Test-Path $Pfx)) { Die "找不到 pfx: $Pfx" }
    if (-not $PfxPassword) { $PfxPassword = $env:GZ_PFX_PASSWORD }
    if (-not $PfxPassword) { Die "pfx 需要密码：-PfxPassword 或环境变量 GZ_PFX_PASSWORD" }
    $signArgs = @("sign", "/fd", "sha256", "/tr", $Timestamp, "/td", "sha256",
              "/f", $Pfx, "/p", $PfxPassword, $Setup)
} else {
    $signArgs = @("sign", "/fd", "sha256", "/tr", $Timestamp, "/td", "sha256",
              "/sha1", $Thumbprint, $Setup)
}

Say "开始签名…"
& $signtool @signArgs
if ($LASTEXITCODE -ne 0) { Die "signtool 签名失败（退出码 $LASTEXITCODE）" }

Say "校验签名…"
& $signtool verify /pa /v $Setup
if ($LASTEXITCODE -ne 0) {
    Write-Host "  签名已写入，但 verify 未通过（常见于根证书未被本机信任）" -ForegroundColor Yellow
}

$sig = Get-AuthenticodeSignature $Setup
Write-Host ""
Write-Host "完成：$($sig.Status)" -ForegroundColor Green
if ($sig.SignerCertificate) { Say "签名者: $($sig.SignerCertificate.Subject)" }
Say "时间戳: $($sig.TimeStamperCertificate.Subject)"
exit 0
