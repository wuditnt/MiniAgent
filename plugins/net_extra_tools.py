"""
网络工具增强组。

fetch_url 只做 GET 文本抓取，这里补：
  http_post    POST/PUT/DELETE + JSON
  dns_lookup   域名解析
  ping         ICMP 连通性（用系统命令）
  check_port   TCP 端口连通性
"""

from __future__ import annotations

import json as _json
import socket

from core import check_command, run_process, tool

PLUGIN_INFO = {"name": "网络增强", "version": "1.0", "author": "mini-agent"}


@tool(
    "http_post",
    "向 URL 发送 POST/PUT/DELETE 等请求，可带 JSON body，返回状态码和响应文本。",
    params={
        "url": {"type": "string", "description": "目标 URL"},
        "method": {"type": "string", "description": "HTTP 方法：POST/PUT/DELETE/PATCH", "default": "POST"},
        "body": {"type": "string", "description": "请求体（JSON 字符串或任意文本）", "default": ""},
        "headers": {"type": "string", "description": "请求头 JSON，如 {\"Authorization\": \"Bearer x\"}", "default": ""},
        "timeout": {"type": "integer", "description": "超时秒数", "default": 30},
    },
    required=["url"],
)
def http_post(url: str, method: str = "POST", body: str = "", headers: str = "", timeout: int = 30) -> str:
    import urllib.request
    import urllib.error

    data = body.encode("utf-8") if body else None
    hdrs = {"User-Agent": "mini-agent/1.0"}
    if headers:
        try:
            hdrs.update(_json.loads(headers))
        except _json.JSONDecodeError:
            return "[error] headers 不是合法 JSON"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return f"[HTTP {resp.status}]\n{text}"
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        return f"[HTTP {e.code}]\n{text}"
    except urllib.error.URLError as e:
        return f"[error] 请求失败：{e.reason}"


@tool(
    "dns_lookup",
    "域名解析：把域名解析为 IP，或把 IP 反查为域名。",
    params={
        "host": {"type": "string", "description": "域名或 IP"},
        "reverse": {"type": "boolean", "description": "True=IP 反查域名", "default": False},
    },
    required=["host"],
)
def dns_lookup(host: str, reverse: bool = False) -> str:
    try:
        if reverse:
            name, aliases, ips = socket.gethostbyaddr(host)
            return f"[reverse] {host}\n  主域名: {name}\n  别名: {', '.join(aliases) or '无'}\n  IP: {', '.join(ips)}"
        else:
            infos = socket.getaddrinfo(host, None)
            ips = sorted({i[4][0] for i in infos})
            return f"[lookup] {host}\n  IP: {', '.join(ips)}"
    except socket.gaierror as e:
        return f"[error] DNS 解析失败：{e}"
    except socket.herror as e:
        return f"[error] 反查失败：{e}"


@tool(
    "ping",
    "测试到目标主机的连通性和延迟（用系统 ping 命令）。",
    params={
        "host": {"type": "string", "description": "主机名或 IP"},
        "count": {"type": "integer", "description": "发送包数", "default": 4},
    },
    required=["host"],
)
def ping(host: str, count: int = 4) -> str:
    import sys

    if sys.platform == "win32":
        cmd = check_command(f"ping -n {count} {host}")
    else:
        cmd = check_command(f"ping -c {count} {host}")
    code, out, err, _ = run_process(cmd, shell=True, timeout=15)
    if code != 0 and not out:
        return f"[error] ping 失败：{err}"
    return out or err


@tool(
    "check_port",
    "检查目标主机的 TCP 端口是否开放。",
    params={
        "host": {"type": "string", "description": "主机名或 IP"},
        "port": {"type": "integer", "description": "端口号"},
        "timeout": {"type": "integer", "description": "超时秒数", "default": 5},
    },
    required=["host", "port"],
)
def check_port(host: str, port: int, timeout: int = 5) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.close()
        return f"[open] {host}:{port} 端口开放"
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        return f"[closed] {host}:{port} 端口不可达：{e}"
