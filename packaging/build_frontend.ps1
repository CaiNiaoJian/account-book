# =============================================================================
# AccountBook Desktop · 前端构建脚本
# -----------------------------------------------------------------------------
# 职责：把 web/ 的 React 源码构建为后端可托管的静态产物，
#       并输出到 src/accountbook/resources/web_dist（供 PyInstaller 收集）。
#
# 为什么产物要放进 Python 包内：
#   打包时只需收集一个已知目录，避免"忘记拷贝前端 → 装完打开是空白页"
#   这类最难排查的事故。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File packaging\build_frontend.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build_frontend.ps1 -SkipInstall
# =============================================================================

[CmdletBinding()]
param(
    # 跳过 npm install（依赖未变动时可显著加快构建）
    [switch]$SkipInstall,

    # npm 镜像源；留空则使用本机 npm 配置
    [string]$Registry = 'https://registry.npmmirror.com'
)

$ErrorActionPreference = 'Stop'

# ---- 路径解析：脚本可能被从任意目录调用，因此全部基于脚本位置推导 ----------
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$webDir = Join-Path $repoRoot 'web'
$outDir = Join-Path $repoRoot 'src\accountbook\resources\web_dist'

Write-Host "[build_frontend] 仓库根目录: $repoRoot" -ForegroundColor Cyan
Write-Host "[build_frontend] 前端目录  : $webDir" -ForegroundColor Cyan
Write-Host "[build_frontend] 产物目录  : $outDir" -ForegroundColor Cyan

if (-not (Test-Path (Join-Path $webDir 'package.json'))) {
    throw "未找到 web/package.json，请确认脚本位于 packaging/ 目录下"
}

# ---- 环境检查 -------------------------------------------------------------
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) { throw '未检测到 Node.js。前端构建需要 Node 18+（推荐 20/22 LTS）。' }
Write-Host "[build_frontend] Node 版本: $(& node --version)" -ForegroundColor DarkGray

# ---- 依赖安装 -------------------------------------------------------------
Push-Location $webDir
try {
    if (-not $SkipInstall) {
        $installArgs = @('install', '--no-audit', '--no-fund')
        if ($Registry) { $installArgs += "--registry=$Registry" }
        Write-Host "[build_frontend] 安装依赖..." -ForegroundColor Cyan
        & npm @installArgs
        if ($LASTEXITCODE -ne 0) { throw "npm install 失败（退出码 $LASTEXITCODE）" }
    }

    # ---- 类型检查 + 构建 --------------------------------------------------
    # package.json 的 build 脚本已包含 `tsc --noEmit`，因此类型错误会直接中断构建。
    # 这是刻意的：宁可构建失败，也不要把类型不一致的产物发给用户。
    Write-Host "[build_frontend] 类型检查并构建..." -ForegroundColor Cyan
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw "前端构建失败（退出码 $LASTEXITCODE）" }
}
finally {
    Pop-Location
}

# ---- 结果校验 -------------------------------------------------------------
$indexFile = Join-Path $outDir 'index.html'
if (-not (Test-Path $indexFile)) {
    throw "构建完成但未找到 $indexFile —— 请检查 vite.config.ts 的 build.outDir"
}

$assetsDir = Join-Path $outDir 'assets'
$assetCount = if (Test-Path $assetsDir) { (Get-ChildItem $assetsDir -File).Count } else { 0 }
$totalSize = (Get-ChildItem $outDir -Recurse -File | Measure-Object -Property Length -Sum).Sum

Write-Host ''
Write-Host '[build_frontend] 构建完成' -ForegroundColor Green
Write-Host ("  产物文件数: {0}（其中 assets {1} 个）" -f (Get-ChildItem $outDir -Recurse -File).Count, $assetCount)
Write-Host ("  产物体积  : {0:N2} MB" -f ($totalSize / 1MB))
