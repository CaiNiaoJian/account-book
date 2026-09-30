# =============================================================================
# AccountBook Desktop · 后端打包脚本（PyInstaller · onedir）
# -----------------------------------------------------------------------------
# 产物：dist\AccountBook\AccountBook.exe 及其依赖目录
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File packaging\build_backend.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build_backend.ps1 -SkipFrontend
#
# 设计要点
#   * **版本一致性校验**：把 __init__.py 的 __version__ 与 version_info.txt 比对，
#     不一致直接中止。版本号对不上是发布事故的常见来源，必须机器校验而非靠人记。
#   * **前端产物校验**：若 web_dist 缺少 index.html 就中止 —— 
#     否则会打包出一个"打开是空白页"的安装包，且极难排查。
# =============================================================================

[CmdletBinding()]
param(
    # 跳过前端构建（前端未改动时使用；仍会校验产物是否存在）
    [switch]$SkipFrontend,

    # 打包完成后立即启动一次做冒烟验证（默认开启）
    [switch]$NoSmokeTest
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

function Write-Step([string]$text) { Write-Host "`n[build_backend] $text" -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host "[build_backend] $text" -ForegroundColor Green }

# ---- 1. 选择解释器：优先使用仓库内的 .venv ---------------------------------
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { (Get-Command python).Source }
Write-Host "[build_backend] 使用解释器: $python"

# ---- 2. 前端产物 -----------------------------------------------------------
Write-Step '检查前端构建产物'
$webDist = Join-Path $repoRoot 'src\accountbook\resources\web_dist'
$webIndex = Join-Path $webDist 'index.html'

if (-not $SkipFrontend) {
    & (Join-Path $scriptDir 'build_frontend.ps1')
    if ($LASTEXITCODE -ne 0) { throw "前端构建失败（退出码 $LASTEXITCODE）" }
}

if (-not (Test-Path $webIndex)) {
    throw "缺少前端产物 $webIndex —— 请先运行 packaging\build_frontend.ps1（不要加 -SkipFrontend）"
}
Write-Ok "前端产物就绪：$webDist"

# ---- 3. 版本一致性校验 -----------------------------------------------------
Write-Step '校验版本号一致性'
$initFile = Join-Path $repoRoot 'src\accountbook\__init__.py'
$versionMatch = Select-String -Path $initFile -Pattern '^__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $versionMatch) { throw "无法从 $initFile 解析 __version__" }
$version = $versionMatch.Matches[0].Groups[1].Value

$versionFile = Join-Path $scriptDir 'version_info.txt'
$fileVersionMatch = Select-String -Path $versionFile -Pattern "StringStruct\('FileVersion',\s*'([^']+)'\)" | Select-Object -First 1
$fileVersion = $fileVersionMatch.Matches[0].Groups[1].Value

# Windows 版本资源固定为四段（a.b.c.d），而 __version__ 通常只有三段。
# 因此统一补齐到四段再比较；同时要求版本号是纯数字 ——
# 带 "beta"/"rc" 的字符串无法写入 PE 版本资源，必须在打包前改成纯数字版本。
function Get-NormalizedVersion([string]$value) {
    if ($value -notmatch '^\d+(\.\d+){0,3}$') {
        throw "版本号 '$value' 不是纯数字点分格式。写入 PE 版本资源要求纯数字，请使用形如 0.2.0 的版本号。"
    }
    $parts = $value.Split('.')
    while ($parts.Count -lt 4) { $parts += '0' }
    return ($parts[0..3] -join '.')
}

if ((Get-NormalizedVersion $version) -ne (Get-NormalizedVersion $fileVersion)) {
    throw "版本号不一致：__init__.py=$version 与 version_info.txt=$fileVersion 归一化后不同。请先同步两者再打包。"
}
Write-Ok "版本号一致：$version（PE 资源写作 $(Get-NormalizedVersion $version)）"

# ---- 4. PyInstaller --------------------------------------------------------
Write-Step '运行 PyInstaller'
Push-Location $repoRoot
try {
    & $python -m PyInstaller --noconfirm --clean (Join-Path $scriptDir 'accountbook.spec')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（退出码 $LASTEXITCODE）" }
}
finally {
    Pop-Location
}

$exePath = Join-Path $repoRoot 'dist\AccountBook\AccountBook.exe'
if (-not (Test-Path $exePath)) { throw "未找到打包产物 $exePath" }

$sizeMb = (Get-ChildItem (Join-Path $repoRoot 'dist\AccountBook') -Recurse -File |
    Measure-Object -Property Length -Sum).Sum / 1MB
Write-Ok ("打包完成：{0}（目录总大小 {1:N1} MB）" -f $exePath, $sizeMb)

# ---- 5. 冒烟测试 -----------------------------------------------------------
# 目的：确认打包产物**真的能启动并服务**，而不是只确认文件存在。
#
# 两个必须注意的实现细节（都是踩过的坑）：
#   1. **不要用 `-WindowStyle Hidden` + 轮询 `$proc.HasExited`**：
#      GUI 子系统程序的进程对象状态在此组合下不可靠，会把"正常运行"
#      误判为"已退出"。
#      改为重定向 stdout/stderr 到文件 + `WaitForExit(超时)`
#      —— 这既避免了误判，也顺带把启动输出留存下来便于排查。
#   2. **匹配模式只用 ASCII**：日志是 UTF-8 无 BOM，而 Windows PowerShell 5.1
#      默认按 ANSI 读取无 BOM 文件，用中文做 `Select-String` 模式会静默匹配失败。
#      因此这里只匹配 `http://127.0.0.1:<port>` 这类纯 ASCII 片段。
if (-not $NoSmokeTest) {
    Write-Step '冒烟测试：启动打包产物并检查本地服务'

    # 冒烟测试的数据目录放在**仓库内**而不是 %TEMP%：
    #   1. 受限环境下子进程可能没有系统临时目录的写权限，会得到误导性的
    #      "数据目录不可写" 报错（那是应用的正确行为，但会干扰构建验证）；
    #   2. 放在仓库内便于失败后直接查看残留的日志与 stdout。
    # 该目录已在 .gitignore 中排除。
    $smokeRoot = Join-Path $repoRoot '.smoke-test'
    $smokeData = Join-Path $smokeRoot ([guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $smokeData | Out-Null
    $stdoutFile = Join-Path $smokeData 'stdout.txt'
    $stderrFile = Join-Path $smokeData 'stderr.txt'
    $logFile = Join-Path $smokeData 'logs\accountbook.log'

    $proc = Start-Process -FilePath $exePath `
        -ArgumentList @('--serve-only', '--data-dir', $smokeData) `
        -PassThru -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile

    try {
        # 等待日志里出现监听地址（最多 45 秒）
        $deadline = (Get-Date).AddSeconds(45)
        $baseUrl = $null
        while ((Get-Date) -lt $deadline) {
            if (Test-Path $logFile) {
                $match = Select-String -Path $logFile -Pattern 'http://127\.0\.0\.1:(\d+)' -Encoding UTF8 |
                    Select-Object -First 1
                if ($match) { $baseUrl = "http://127.0.0.1:" + $match.Matches[0].Groups[1].Value; break }
            }
            $proc.Refresh()
            if ($proc.HasExited) { break }
            Start-Sleep -Milliseconds 400
        }

        if (-not $baseUrl) {
            $tail = ''
            if (Test-Path $stderrFile) { $tail = (Get-Content $stderrFile -Tail 20) -join "`n" }
            throw "打包产物未能在 45 秒内启动本地服务。`n最近输出：`n$tail"
        }

        $health = Invoke-RestMethod -Uri "$baseUrl/health" -TimeoutSec 10
        if ($health.status -ne 'ok') { throw "健康检查返回异常：$($health | ConvertTo-Json -Compress)" }

        # 顺带确认前端产物被正确打包：无令牌访问首页应得到 401（说明托管链路生效），
        # 若返回 200 且内容为兜底页，说明 web_dist 没被打进去。
        $indexStatus = 0
        try {
            Invoke-WebRequest -Uri "$baseUrl/" -TimeoutSec 10 -UseBasicParsing | Out-Null
            $indexStatus = 200
        }
        catch { $indexStatus = [int]$_.Exception.Response.StatusCode }

        if ($indexStatus -ne 401) {
            throw "首页在无令牌情况下返回 $indexStatus（期望 401）—— 请检查 web_dist 是否被正确打包"
        }

        Write-Ok "冒烟测试通过：health=$($health.status) version=$($health.version) 首页无令牌状态码=$indexStatus"
    }
    finally {
        $proc.Refresh()
        if (-not $proc.HasExited) { $proc | Stop-Process -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 500
        Remove-Item -LiteralPath $smokeData -Recurse -Force -ErrorAction SilentlyContinue
    }
}
