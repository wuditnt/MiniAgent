"""
联网搜索工具测试 —— DeepSeek Search 后端 + 后端选择，不碰网络。

覆盖：
  · 后端选择：默认 deepseek，可在系统配置里切换（立即生效）；
  · DeepSeek 联网 Key 独立配置：只读 settings，不引用「模型配置」里的 Key；
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


def test_deepseek_key_independent() -> None:
    """联网 Key 与「模型配置」完全解耦：模型里有 DeepSeek Key 也不会被使用。"""
    _isolate()
    _seed_ollama()
    mc.create_entry("deepseek", "d1", "https://api.deepseek.com/v1", "sk-model", "deepseek-flash")
    assert ws._deepseek_key() == "", "不应引用模型配置里的 DeepSeek Key"

    # 联网搜索页独立配置的 Key 生效（自动去首尾空白）
    st.update({"deepseek_search_api_key": "  sk-search  "})
    assert ws._deepseek_key() == "sk-search"


def test_search_deepseek_request_and_parse() -> None:
    _isolate()
    _seed_ollama()
    st.update({"deepseek_search_api_key": "sk-test"})

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
    assert seen["key"] == "sk-test", "联网鉴权应使用联网搜索页独立配置的 Key"
    assert seen["payload"] == {"query": "mini-agent", "count": 2, "need_content": False}
    assert out == [
        ("A", "https://a.example", "长摘要A"),   # 优先用 summary
        ("B", "https://b.example", "短B"),        # 没有 summary 时退回 snippet
    ]


if __name__ == "__main__":
    test_backend_selection()
    test_deepseek_key_independent()
    test_search_deepseek_request_and_parse()
    print("---")
    print("全部通过 ✅")
