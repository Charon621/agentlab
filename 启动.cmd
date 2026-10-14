@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import fastapi, langgraph, langgraph.checkpoint.sqlite, httpx" 2>nul
if errorlevel 1 (
  echo 首次启动：安装本项目的隔离依赖，请保持联网。
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
echo AgentLab 正在启动。默认打开 http://127.0.0.1:8811 ，关闭窗口即可停止服务。
start "" http://127.0.0.1:8811
".venv\Scripts\python.exe" run.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo 启动失败。请查看上方错误，确认 Python 3.10+、网络和端口。详细操作见 README.md。
pause
exit /b 1
