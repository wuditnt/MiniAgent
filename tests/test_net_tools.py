"""
网络工具双实现测试 —— 云端 DeepSeek Fetch / 本地抓取 / 失败回退，不碰网络。

覆盖：
  · 模式分派：local 只走本地，cloud 优先云端；
  · 云端异常自动回退本地（含未配置 Key）；
  · 非 http(s) 直接拒绝；
  · DeepSeek Fetch 的请求体与响应解析（mock urlopen）。
"""

from __future__ import annotations

import json

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

from core import net_mode  # noqa: E402
from builtin_tools import net_tools as nt  # noqa: E402


def test_dispatch_local_and_cloud() -> None:
    calls: list[str] = []
    orig_c, orig_l = nt._cloud_fetch, nt._local_fetch
    nt._cloud_fetch = lambda url: (calls.append("cloud"), "CLOUD")[1]
    nt._local_fetch = lambda url: (calls.append("local"), "LOCAL")[1]
    try:
        net_mode.set_mode("local")
        assert nt.fetch_url("https://example.com") == "LOCAL"
        assert calls == ["local"], calls

        calls.clear()
        net_mode.set_mode("cloud")
        assert nt.fetch_url("https://example.com") == "CLOUD"
        assert calls == ["cloud"], calls
    finally:
        nt._cloud_fetch, nt._local_fetch = orig_c, orig_l
        net_mode.reset()
    print("✓ 分派：local 只走本地，cloud 优先云端")


def test_cloud_fallback_local() -> None:
    def boom(url: str) -> str:
        raise RuntimeError("403 Forbidden")

    orig_c, orig_l = nt._cloud_fetch, nt._local_fetch
    nt._cloud_fetch = boom
    nt._local_fetch = lambda url: "LOCAL"
    try:
        net_mode.set_mode("cloud")
        assert nt.fetch_url("https://example.com") == "LOCAL", "云端失败应回退本地"
    finally:
        nt._cloud_fetch, nt._local_fetch = orig_c, orig_l
        net_mode.reset()
    print("✓ 回退：云端异常时自动改用本地抓取")


def test_reject_non_http() -> None:
    assert nt.fetch_url("ftp://example.com/a").startswith("[error]")
    print("✓ 拒绝：非 http(s) 直接返回 error")


def test_cloud_fetch_request_and_parse() -> None:
    seen: dict = {}

    class FakeResp:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def read(self, n: int) -> bytes:
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        seen["auth"] = req.get_header("Authorization")
        shared = {"title": "标题A", "content": "# 正文\n这里是内容"}
        return FakeResp(shared)

    orig_open = nt.urllib.request.urlopen
    orig_key = nt._deepseek_key
    nt.urllib.request.urlopen = fake_urlopen
    nt._deepseek_key = lambda: "sk-test"
    try:
        out = nt._cloud_fetch("https://example.com/a")
    finally:
        nt.urllib.request.urlopen = orig_open
        nt._deepseek_key = orig_key

    assert seen["url"] == nt.DEEPSEEK_FETCH_URL
    assert seen["body"] == {"url": "https://example.com/a"}
    assert seen["auth"] == "Bearer sk-test"
    assert "标题A" in out and "正文" in out
    print("✓ 云端：请求 URL/请求体/鉴权头正确，Markdown 正文被保留")


if __name__ == "__main__":
    test_dispatch_local_and_cloud()
    test_cloud_fallback_local()
    test_reject_non_http()
    test_cloud_fetch_request_and_parse()
    print("---")
    print("全部通过 ✅")
