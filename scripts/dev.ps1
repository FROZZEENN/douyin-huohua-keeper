# =============================================================================
# 开发环境辅助脚本（Windows 原生，不需要 Git Bash）
#
#   .\scripts\dev.ps1 setup      建虚拟环境、装依赖、下载 Chromium
#   .\scripts\dev.ps1 run        本地启动（会弹出真实浏览器窗口）
#   .\scripts\dev.ps1 run-head   本地启动（无头，贴近生产）
#   .\scripts\dev.ps1 test       跑测试
#   .\scripts\dev.ps1 cov        跑测试 + 覆盖率
#   .\scripts\dev.ps1 lint       静态检查
#   .\scripts\dev.ps1 fmt        格式化
#   .\scripts\dev.ps1 check      自检：环境、依赖、存储、通知通道
#   .\scripts\dev.ps1 clean      清掉缓存和临时产物
#
# 上面两个 run 默认**不启用定时任务**（只起工作台）。同一个抖音账号不该
# 有两个实例各跑各的定时 —— 既是风控风险，也会互相顶号。确实要本地定时：
#
#   .\scripts\dev.ps1 run --with-scheduler
#
# 如果执行策略拦住了脚本（PowerShell 默认会拦），用这个跑：
#   powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1 run
# =============================================================================

param(
    [Parameter(Position = 0)]
    [string]$Command = "",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

$ErrorActionPreference = "Stop"

$Root = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { (Get-Location).Path }
Set-Location $Root

$VenvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { ".venv" }
$Py = Join-Path $Root (Join-Path $VenvDir "Scripts\python.exe")

function Write-Log([string]$Message) {
    Write-Host "[dev] $Message" -ForegroundColor Cyan
}

function Stop-WithError([string]$Message) {
    Write-Host "[dev] $Message" -ForegroundColor Red
    exit 1
}

function Assert-Venv {
    if (-not (Test-Path $Py)) {
        Stop-WithError "虚拟环境不存在，先跑：.\scripts\dev.ps1 setup"
    }
}

# 本地启动的公共默认值。
#
# 默认**不启用定时任务**：默认值必须安全 —— 谁都不想「只是想本机看一眼」，
# 结果它到点真的给所有好友发了消息，而服务器那边也在发（同一账号两个实例）。
function Set-LocalDefaults([string]$Flag, [string]$CommandName) {
    if (-not $env:HUOHUA_HOST) { $env:HUOHUA_HOST = "127.0.0.1" }
    if ($Flag -eq "--with-scheduler") {
        Write-Log "! 已显式启用定时任务 —— 它会真的按时发送消息"
    } else {
        $env:HUOHUA_ENABLE_SCHEDULER = "false"
        Write-Log "定时任务已关闭（只起工作台，不会自动发送）"
        Write-Log "确实要本机定时：.\scripts\dev.ps1 $CommandName --with-scheduler"
    }
}

function Invoke-Setup {
    $py = "python"
    if (-not (Get-Command $py -ErrorAction SilentlyContinue)) {
        Stop-WithError "找不到 python（本项目需要 3.11+）"
    }
    & $py -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "Python 版本过低（需要 3.11+）"
    }

    Write-Log "创建虚拟环境 $VenvDir"
    & $py -m venv $VenvDir
    Assert-Venv

    Write-Log "升级 pip"
    & $Py -m pip install --upgrade pip setuptools wheel

    Write-Log "安装运行依赖"
    & $Py -m pip install -r requirements.txt

    Write-Log "安装开发依赖"
    & $Py -m pip install -r requirements-dev.txt

    Write-Log "以可编辑模式安装本项目"
    & $Py -m pip install -e .

    # 两个都得装：有头模式用 chromium，**无头模式用的是 chromium-headless-shell**。
    # 只装 chromium 的话，无头跑起来会报 "Executable doesn't exist"。
    Write-Log "下载 Chromium + headless shell（约 200MB，只做一次）"
    & $Py -m playwright install chromium chromium-headless-shell

    if (-not (Test-Path (Join-Path $Root ".env"))) {
        Write-Log "从模板生成 .env"
        Copy-Item (Join-Path $Root ".env.example") (Join-Path $Root ".env")
        Write-Log "本机自用可以不填 HUOHUA_TOKEN（那是给公网访问用的门禁）"
    }

    Write-Log "完成。下一步：.\scripts\dev.ps1 run"
}

function Invoke-Run {
    Assert-Venv
    Write-Log "本地启动（有头模式，方便观察浏览器动作）"
    if (-not $env:HUOHUA_PORT) { $env:HUOHUA_PORT = "8787" }
    Write-Log "工作台地址 http://127.0.0.1:$($env:HUOHUA_PORT)"
    if (-not $env:HUOHUA_HEADLESS) { $env:HUOHUA_HEADLESS = "false" }
    Set-LocalDefaults ($Rest | Select-Object -First 1) "run"
    & $Py -m douyin_huohua_keeper
}

function Invoke-RunHeadless {
    Assert-Venv
    Write-Log "本地启动（无头模式，贴近生产行为）"
    $env:HUOHUA_HEADLESS = "true"
    Set-LocalDefaults ($Rest | Select-Object -First 1) "run-head"
    & $Py -m douyin_huohua_keeper
}

switch ($Command) {
    "setup" {
        Invoke-Setup
    }
    "run" {
        Invoke-Run
    }
    "run-head" {
        Invoke-RunHeadless
    }
    "test" {
        Assert-Venv
        Write-Log "跑测试"
        & $Py -m pytest @Rest
    }
    "cov" {
        Assert-Venv
        Write-Log "跑测试 + 覆盖率"
        & $Py -m pytest --cov --cov-report=term-missing --cov-report=html
        Write-Log "HTML 报告：htmlcov/index.html"
    }
    "lint" {
        Assert-Venv
        Write-Log "ruff check"
        & $Py -m ruff check src tests scripts
        Write-Log "ruff format --check"
        & $Py -m ruff format --check src tests scripts
    }
    "fmt" {
        Assert-Venv
        Write-Log "ruff format"
        & $Py -m ruff format src tests scripts
        Write-Log "ruff check --fix"
        & $Py -m ruff check --fix src tests scripts
    }
    "check" {
        Assert-Venv
        Write-Log "运行自检"
        & $Py scripts/healthcheck.py
    }
    "clean" {
        Write-Log "清理缓存与临时产物"
        foreach ($name in @("__pycache__", ".pytest_cache", ".ruff_cache", "htmlcov", "build", "dist")) {
            Get-ChildItem -Path $Root -Recurse -Directory -Filter $name -ErrorAction SilentlyContinue |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
        foreach ($pattern in @(".coverage", ".coverage.*", "*.pyc")) {
            Get-ChildItem -Path $Root -Recurse -File -Filter $pattern -ErrorAction SilentlyContinue |
                Remove-Item -Force -ErrorAction SilentlyContinue
        }
        Write-Log "注意：没有动 data/ —— 那里面是你的登录态和配置，要删请手动来"
    }
    default {
        Get-Content $PSCommandPath | Select-Object -Skip 1 -First 20 |
            ForEach-Object { $_ -replace '^# ?', '' }
        exit 1
    }
}
