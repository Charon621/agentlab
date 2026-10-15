$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# 首次启动：创建隔离虚拟环境
if (-not (Test-Path '.\.venv\Scripts\python.exe')) {
  Write-Host '首次启动：创建 Python 虚拟环境...'
  py -3 -m venv .venv
  if (-not $?) { throw '创建虚拟环境失败，请确认已安装 Python 3.10+' }
}

# 依赖自检（缺失时安装）
& '.\.venv\Scripts\python.exe' -c 'import fastapi, langgraph, langgraph.checkpoint.sqlite, httpx' 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host '首次启动：安装本项目的隔离依赖，请保持联网。'
  & '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
  if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络后重试。' }
}

# 端口已被占用：说明服务已在运行，直接打开页面
$listening = Get-NetTCPConnection -LocalPort 8811 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
  try {$health = Invoke-RestMethod -Uri 'http://127.0.0.1:8811/api/health' -TimeoutSec 3} catch {throw '端口 8811 已被未知服务占用，请检查后重试。'}
  if ($health.app -ne 'agentlab') {throw '端口 8811 上运行的不是本项目，请检查后重试。'}
  Write-Host 'AgentLab 服务已在运行，直接打开 http://127.0.0.1:8811'
  Start-Process 'http://127.0.0.1:8811'
  exit 0
}

Write-Host 'AgentLab 正在启动。默认打开 http://127.0.0.1:8811 ，关闭本窗口即可停止服务。'
Start-Process 'http://127.0.0.1:8811'
& '.\.venv\Scripts\python.exe' run.py
exit $LASTEXITCODE
