"""
联网搜索工具测试 —— DeepSeek Search 后端 + 后端选择，不碰网络。

覆盖：
  · 后端选择：默认 deepseek，可在系统配置里切换（立即生效）；
  · DeepSeek Key 复用：优先取「模型配置」里 DeepSeek 那条的 api_key；
  · _search_deepseek：请求体合法（query/count/need_content）、
    用 summary 作为摘要、结果条数受 max_results 约束。
"""

from __future__ import annotations

import os
import tempfile

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

import core.model_config as mc  # noqa: E402
import core.settings as st  # noqa: E402
from plugins import web_search as ws  # noqa: E402


def _isolate() -> None:
    """把 models.json / settings.json 指到临时目录，避免读写真实 web_data。"""
    root = tempfile.mkdtemp(prefix="miniagent-ws-test-")
    mc._MODELS_FILE = os.path.join(root, "models.json")
    st._SETTINGS_FILE = os.path.join(root, "settings.json")


def _seed_ollama() -> None:
    """基线：只放一条非 DeepSeek 配置，避免 _load_raw 的环境变量兜底干扰。"""
    mc._save_raw({
        "active": "o1",
        "providers": [{
            "id": "o1", "provider": "ollama", "name": "qwen2.5:7b",
            "base_url": "http://127.0.0.1:11434/v1", "api_key": "",
            "model": "qwen2.5:7b", "created_at": 0,
        }],
        "reasoning": "high",
    })


def test_backend_selection() -> None:
    _isolate()
    # 未配置 → 默认 DeepSeek
    assert ws._backend() == "deepseek"
    # 切换后立即生效（没有缓存）
    st.update({"search_provider": "tavily"})
    assert ws._backend() == "tavily"
    st.update({"search_provider": "bing"})
    assert ws._backend() == "bing"
    st.reset()
    assert ws._backend() == "deepseek"


def test_deepseek_key_reuse() -> None:
    _isolate()
    _seed_ollama()
    mc.create_entry("deepseek", "d1", "https://api.deepseek.com/v1", "sk-first", "deepseek-flash")
    # 当前生效的还不是 DeepSeek → 取该服务商第一条有 key 的记录
    assert ws._deepseek_key() == "sk-first"
    # 当前生效的那条优先
    second = mc.create_entry(
        "deepseek", "d2", "https://api.deepseek.com/v1", "sk-second", "deepseek-v4-pro"
    )
    mc.set_active(second["id"])
    assert ws._deepseek_key() == "sk-second"
    # 其他厂商的配置不会被误用
    assert mc.provider_key("zhipu") == ""


def test_search_deepseek_request_and_parse() -> None:
    _isolate()
    _seed_ollama()
    mc.create_entry("deepseek", "d", "https://api.deepseek.com/v1", "sk-test", "deepseek-flash")

    seen: dict = {}

    def fake_http(url: str, payload: dict, key: str) -> dict:
        seen.update(url=url, payload=payload, key=key)
        return {
            "results": [
                {"title": "A", "url": "https://a.example", "summary": "长摘要A", "snippet": "短"},
                {"title": "B", "url": "https://b.example", "summary": "", "snippet": "短B"},
            ]
        }

    orig = ws._http_json
    ws._http_json = fake_http
    try:
        out = ws._search_deepseek("mini-agent", 2)
    finally:
        ws._http_json = orig

    assert seen["url"] == ws.DEEPSEEK_SEARCH_URL
    assert seen["key"] == "sk-test", "联网鉴权应复用 DeepSeek 模型配置的 key"
    assert seen["payload"] == {"query": "mini-agent", "count": 2, "need_content": False}
    assert out == [
        ("A", "https://a.example", "长摘要A"),   # 优先用 summary
        ("B", "https://b.example", "短B"),        # 没有 summary 时退回 snippet
    ]


if __name__ == "__main__":
    test_backend_selection()
    test_deepseek_key_reuse()
    test_search_deepseek_request_and_parse()
    print("---")
    print("全部通过 ✅")
