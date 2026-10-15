"""Shared test isolation: purge credentials, disable dotenv, block external sockets.

Imported by conftest.py (pytest) and recovery_worker.py (subprocess).
Only mutates the current process environment — never user files or system env.
"""
import os
import socket
import ipaddress

# 1. Disable dotenv auto-load before any agentlab import.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

# 2. Purge all model / search / grant / proxy / live keys inherited from parent process.
_KEY_PATTERNS = (
    "API_KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD",
    "GRANT", "COOKIE", "SESSION_KEY", "AUTH",
)
_ENV_PREFIXES = (
    "QWEN_", "KIMI_", "DEEPSEEK_", "GLM_", "XIAOMI_", "OPENAI_", "ANTHROPIC_",
    "TAVILY_", "AGENTLAB_", "OPENROUTER_", "TOGETHER_", "GROQ_", "MISTRAL_",
)
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

for _name in list(os.environ):
    _upper = _name.upper()
    if any(_upper.endswith(p) or p in _upper for p in _KEY_PATTERNS):
        if any(_upper.startswith(p) for p in _ENV_PREFIXES) or _upper.endswith(("_API_KEY", "_TOKEN", "_SECRET")):
            os.environ.pop(_name, None)
    if _name in _PROXY_VARS:
        os.environ.pop(_name, None)

# Belt-and-suspenders: explicit known names.
for _name in (
    "QWEN_API_KEY", "KIMI_API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY",
    "XIAOMI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "TAVILY_API_KEY", "OPENROUTER_API_KEY",
    "AGENTLAB_PROVIDER", "AGENTLAB_SEARCH", "AGENTLAB_TOOL_MODE",
    "AGENTLAB_HOST", "AGENTLAB_PORT", "AGENTLAB_DATA_DIR",
):
    os.environ.pop(_name, None)

# Re-set after purge so every entrypoint sees it.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

# Never allow the module-level ASGI application to open the real data directory.
# Every test process, including recovery subprocesses, owns a separate directory.
import tempfile
os.environ["AGENTLAB_DATA_DIR"] = tempfile.mkdtemp(prefix="agentlab-test-")

# 3. Block unregistered external connections at socket level.
#    Loopback (127.0.0.1 / ::1) is allowed for local test fixtures.
_original_socket = socket.socket
_allowed_hosts = frozenset({"127.0.0.1", "::1", "localhost", "testserver"})


class _BlockedSocket(_original_socket):
    def connect(self, address):
        if isinstance(address, tuple) and len(address) >= 2:
            host = address[0]
            if isinstance(host, bytes):
                host = host.decode("ascii", errors="replace")
            try:
                ip = ipaddress.ip_address(host)
                if not ip.is_loopback:
                    raise OSError(
                        f"Test isolation: blocked external connection to {host}:{address[1]}"
                    )
            except ValueError:
                if host not in _allowed_hosts:
                    raise OSError(
                        f"Test isolation: blocked external connection to {host}:{address[1]}"
                    )
        return super().connect(address)


socket.socket = _BlockedSocket

# 4. Ensure project root on sys.path for `python -m pytest` from any cwd.
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
