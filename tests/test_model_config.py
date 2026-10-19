"""
推理等级通用化 + 会话级模型解析测试 —— 纯函数 / 临时配置文件，不碰网络。

覆盖：
  · reasoning_spec：各厂家声明的等级与参数风格（thinking/switch/effort），
    未知厂家回退通用 effort 三档；
  · _apply_reasoning：三种参数风格的实际落参、界面档位到接口取值的重映射
    （values）、厂家不支持的等级一律不发；
  · 默认值与迁移：缺省 high、旧 off→low / medium→high、非法值回退 high；
  · resolve_model：按 id 解析、空/失效 id 回退全局 active、
    空字段回退环境变量。
"""

from __future__ import annotations

import os
import tempfile

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_MODEL", "env-model")
os.environ.setdefault("OPENAI_BASE_URL", "http://env-url/v1")

import core.model_config as mc  # noqa: E402
from agent import _apply_reasoning, _apply_thinking_off  # noqa: E402


def _isolate() -> str:
    """把 models.json 指到临时目录，避免读写真实 web_data。"""
    mc._MODELS_FILE = os.path.join(
        tempfile.mkdtemp(prefix="miniagent-mc-test-"), "models.json"
    )
    return mc._MODELS_FILE


# ---------------------------------------------------------------- spec 解析

def test_reasoning_spec_styles() -> None:
    """厂家声明 → 等级集合与参数风格；未知厂家 → 通用 effort 三档。"""
    # 智谱 GLM：thinking 开启 + reasoning_effort，三档
    assert mc.reasoning_spec("zhipu") == {
        "levels": ["low", "high", "max"], "style": "thinking",
    }
    # 豆包（火山方舟）：thinking 开启，接口只认 low/high 两档
    assert mc.reasoning_spec("doubao") == {
        "levels": ["low", "high"], "style": "thinking",
    }
    # 通义千问兼容模式：enable_thinking 开关，三档
    assert mc.reasoning_spec("qwen") == {
        "levels": ["low", "high", "max"], "style": "switch",
    }
    # OpenAI：effort 风格，但没有 max → 界面三档映射到 low/medium/high
    assert mc.reasoning_spec("openai") == {
        "levels": ["low", "high", "max"], "style": "effort",
        "values": {"low": "low", "high": "medium", "max": "high"},
    }
    # DeepSeek 等未单独声明的厂家：通用三档 effort
    generic = {"levels": ["low", "high", "max"], "style": "effort"}
    assert mc.reasoning_spec("deepseek") == generic
    assert mc.reasoning_spec("no-such-vendor") == generic
    assert mc.reasoning_spec("") == generic


# ---------------------------------------------------------------- 参数映射

def test_apply_reasoning_effort() -> None:
    """effort 风格：等级直传 reasoning_effort。"""
    spec = mc.reasoning_spec("deepseek")
    p: dict = {}
    _apply_reasoning(p, spec, "high")
    assert p == {"reasoning_effort": "high"}
    p = {}
    _apply_reasoning(p, spec, "max")
    assert p == {"reasoning_effort": "max"}


def test_apply_reasoning_values_remap() -> None:
    """values 映射：界面档位与接口取值不同的厂家按 values 重映射。"""
    spec = mc.reasoning_spec("openai")
    p: dict = {}
    _apply_reasoning(p, spec, "max")
    assert p == {"reasoning_effort": "high"}, "OpenAI 无 max → 映射成 high"
    p = {}
    _apply_reasoning(p, spec, "high")
    assert p == {"reasoning_effort": "medium"}


def test_apply_reasoning_thinking_and_switch() -> None:
    """thinking / switch 风格：开启思考 + reasoning_effort 调力度。"""
    think = mc.reasoning_spec("zhipu")
    p: dict = {}
    _apply_reasoning(p, think, "high")
    assert p == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}

    switch = mc.reasoning_spec("qwen")
    p = {}
    _apply_reasoning(p, switch, "max")
    assert p == {"enable_thinking": True, "reasoning_effort": "max"}


def test_apply_reasoning_unsupported_level() -> None:
    """厂家不支持的等级（如豆包只有 low/high 却收到 max）→ 不发参数。"""
    p: dict = {}
    _apply_reasoning(p, mc.reasoning_spec("doubao"), "max")
    assert p == {}
    # 完全不支持推理的厂家（空 levels）同样不发
    p = {}
    _apply_reasoning(p, {"levels": [], "style": "effort"}, "high")
    assert p == {}


def test_thinking_off_payloads() -> None:
    """快速对话：按厂家风格显式关闭思考（不发参数≠不思考）。"""
    p: dict = {}
    _apply_thinking_off(p, mc.reasoning_spec("deepseek"))
    assert p == {"reasoning_effort": "none"}, "DeepSeek 用 effort=none 关闭思考"
    p = {}
    _apply_thinking_off(p, mc.reasoning_spec("zhipu"))
    assert p == {"thinking": {"type": "disabled"}}
    p = {}
    _apply_thinking_off(p, mc.reasoning_spec("qwen"))
    assert p == {"enable_thinking": False}
    # 未声明厂家按通用 effort 风格
    p = {}
    _apply_thinking_off(p, None)
    assert p == {"reasoning_effort": "none"}


# ---------------------------------------------------------------- 默认与迁移

def test_normalize_reasoning() -> None:
    """空值 → None（跟全局默认）；旧档位/非法值 → 新三档。"""
    assert mc.normalize_reasoning(None) is None
    assert mc.normalize_reasoning("") is None
    assert mc.normalize_reasoning("off") == "low"
    assert mc.normalize_reasoning("medium") == "high"
    assert mc.normalize_reasoning("max") == "max"
    assert mc.normalize_reasoning("bogus") == "high"


def test_default_and_migration() -> None:
    _isolate()
    # 没写过配置 → 默认 high
    assert mc.reasoning_level() == "high"
    # 旧档位 off → low；非法值 → high
    mc._save_raw({"active": "default", "providers": [], "reasoning": "off"})
    assert mc.reasoning_level() == "low"
    mc._save_raw({"active": "default", "providers": [], "reasoning": "bogus"})
    assert mc.reasoning_level() == "high"
    # 正常往返
    assert mc.set_reasoning("max") == "max"
    assert mc.reasoning_level() == "max"
    # 非法值写入被拒
    try:
        mc.set_reasoning("off")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("off 已废弃，set_reasoning 应拒绝")


# ---------------------------------------------------------------- 会话解析

def test_resolve_model() -> None:
    _isolate()
    # 断言与环境值相对（pytest 全量收集时其他套件可能改写过 OPENAI_* 环境变量）
    env_key = os.environ.get("OPENAI_API_KEY", "")
    e = mc.create_entry("ollama", "本地小模型", "http://127.0.0.1:11434/v1", "", "qwen2.5:7b")
    pid = e["id"]

    # 按 id 精确解析
    r = mc.resolve_model(pid)
    assert r["model_id"] == pid
    assert r["provider"] == "ollama"
    assert r["model"] == "qwen2.5:7b"
    # 空字段回退环境变量（api_key 为空串 → 环境变量）
    assert r["api_key"] == env_key
    assert r["base_url"] == "http://127.0.0.1:11434/v1"

    # 空 id / 已删除 id → 回退全局 active
    r = mc.resolve_model("")
    assert r["model_id"] == "default"
    r = mc.resolve_model("no-such-id")
    assert r["model_id"] == "default"
    # active 也可能空字段（缺 key 的本地服务商）→ 环境变量兜底
    assert r["api_key"] == env_key


if __name__ == "__main__":
    test_reasoning_spec_styles()
    test_apply_reasoning_effort()
    test_apply_reasoning_values_remap()
    test_apply_reasoning_thinking_and_switch()
    test_apply_reasoning_unsupported_level()
    test_thinking_off_payloads()
    test_normalize_reasoning()
    test_default_and_migration()
    test_resolve_model()
    print("---")
    print("全部通过 ✅")
