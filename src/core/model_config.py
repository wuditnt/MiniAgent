"""
多模型配置 —— 支持任意 OpenAI 兼容接口，内置国内主流服务商模板，一键切换。

为什么要独立一个模块？
  · agent.py 只认「当前用哪个 base_url / api_key / model」这三个值，不关心
    用户配了几家、叫什么名字；这层「多配置 + 当前选中」需要自己持久化。
  · API Key 只存本机 web_data/models.json，不上传任何第三方。

存储结构：
    {"active": "<id>", "providers": [{id, provider, name, base_url, api_key, model, created_at}]}
每条记录 = 一份「服务商 + 模型」组合；active 指向当前生效的那条。
"""

from __future__ import annotations

import json
import os
import time
import uuid

from ._paths import ROOT

_MODELS_FILE = os.path.join(ROOT, "web_data", "models.json")

#: 推理档位全集（low/high/max，三档都表示「开启思考 + 强度」）；
#: 具体厂家支持哪几档由模板的 reasoning.levels 声明，未声明的厂家按通用 effort
#: 处理（发 reasoning_effort，接口不认时由 agent 的降级重试兜底）。
REASONING_LEVELS = ("low", "high", "max")
_DEFAULT_REASONING = "high"

#: 旧档位 → 新三档的迁移表（旧 off 表示不思考，新体系里没有关闭态 → 落到最低档）
_LEGACY_REASONING = {"off": "low", "medium": "high"}

#: 推理参数风格：
#:   effort  → 发 {"reasoning_effort": <取值>}（OpenAI 系标准，通用缺省）
#:   thinking→ 发 {"thinking": {"type": "enabled"}} + {"reasoning_effort": <取值>}（智谱 GLM / 豆包）
#:   switch  → 发 {"enable_thinking": true} + {"reasoning_effort": <取值>}（通义千问兼容模式）
#: values 是「界面档位 → 接口实际取值」的映射，缺省同名直传（如 OpenAI 无 max → 映射成 high）。
def _rspec(levels: tuple | list, style: str = "effort", values: dict | None = None) -> dict:
    spec: dict = {"levels": list(levels), "style": style}
    if values:
        spec["values"] = dict(values)
    return spec


_GENERIC_RSPEC = _rspec(REASONING_LEVELS)   # 通用缺省：三档全开 + effort 风格


def normalize_reasoning(value: str | None) -> str | None:
    """把旧档位 / 非法值归一到新三档（low/high/max）。
    空值返回 None，表示「跟全局默认」（全局默认由 _normalize 保证合法）。
    """
    v = (value or "").strip().lower()
    if not v:
        return None
    v = _LEGACY_REASONING.get(v, v)
    return v if v in REASONING_LEVELS else _DEFAULT_REASONING


#: 内置服务商模板：base_url + 常见模型名 + 是否需要 API Key + 推理档位适配
PROVIDER_TEMPLATES: dict[str, dict] = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "no_key": False,
        "reasoning": _GENERIC_RSPEC,     # 官方 API 支持 reasoning_effort（low/high/max）
    },
    "zhipu": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-air", "glm-4-flash"],
        "no_key": False,
        # GLM-5.3：thinking 开启 + reasoning_effort 仅支持 max/high/low（正好三档）
        "reasoning": _rspec(REASONING_LEVELS, "thinking"),
    },
    "kimi": {
        "name": "Kimi (月之暗面)",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "no_key": False,
        "reasoning": _GENERIC_RSPEC,
    },
    "qwen": {
        "name": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo"],
        "no_key": False,
        # 兼容模式：enable_thinking 开启思考 + 新模型支持 reasoning_effort 调力度
        "reasoning": _rspec(REASONING_LEVELS, "switch"),
    },
    "doubao": {
        "name": "豆包 (火山方舟)",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-pro-32k", "doubao-lite-32k"],
        "no_key": False,
        # 火山方舟：thinking.type 开关 + reasoning_effort 只认 low/high 两档
        "reasoning": _rspec(("low", "high"), "thinking"),
    },
    "qianfan": {
        "name": "百度千帆",
        "base_url": "https://qianfan.baidubce.com/v2",
        "models": ["ernie-4.0-8k", "ernie-3.5-8k"],
        "no_key": False,
        "reasoning": _GENERIC_RSPEC,
    },
    "siliconflow": {
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
        "no_key": False,
        "reasoning": _GENERIC_RSPEC,
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "no_key": False,
        "reasoning": _rspec(REASONING_LEVELS, values={   # OpenAI 的 reasoning_effort 没有 max
            "low": "low", "high": "medium", "max": "high",
        }),
    },
    "ollama": {
        "name": "Ollama (本地)",
        "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5:7b", "llama3.1:8b"],
        "no_key": True,
        "reasoning": _GENERIC_RSPEC,
    },
    "vllm": {
        "name": "vLLM (本地)",
        "base_url": "http://localhost:8000/v1",
        "models": [],
        "no_key": True,
        "reasoning": _GENERIC_RSPEC,
    },
    "custom": {
        "name": "自定义",
        "base_url": "",
        "models": [],
        "no_key": True,
        "reasoning": _GENERIC_RSPEC,
    },
}


def templates() -> dict:
    """给前端的内置模板列表。"""
    return PROVIDER_TEMPLATES


def mask_key(key: str) -> str:
    """脱敏：只保留后 4 位，前端回显用。真实 key 永不下发。"""
    key = key or ""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return "****" + key[-4:]


# ---------------------------------------------------------------- 读写

def _default_entry() -> dict:
    """首次使用时（无配置文件）按环境变量兜底造一条 DeepSeek 记录。
    显示名称默认等于模型 ID（用户可在配置页改成更好读的名字）。
    """
    tpl = PROVIDER_TEMPLATES["deepseek"]
    model = os.environ.get("OPENAI_MODEL") or tpl["models"][0]
    return {
        "id": "default",
        "provider": "deepseek",
        "name": model,
        "base_url": os.environ.get("OPENAI_BASE_URL") or tpl["base_url"],
        "api_key": os.environ.get("OPENAI_API_KEY") or "",
        "model": model,
        "created_at": time.time(),
    }


def _load_raw() -> dict:
    if not os.path.exists(_MODELS_FILE):
        return {"active": "default", "providers": [_default_entry()]}
    try:
        with open(_MODELS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"active": "default", "providers": [_default_entry()]}
    if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        return {"active": "default", "providers": [_default_entry()]}
    return data


def _normalize(data: dict) -> dict:
    """补齐缺失字段 + 保证 active 指向有效记录 + 至少一条记录。"""
    items = [w for w in data.get("providers", []) if isinstance(w, dict) and w.get("id")]
    if not items:
        items = [_default_entry()]
    for w in items:
        w.setdefault("provider", "custom")
        w.setdefault("base_url", "")
        w.setdefault("api_key", "")
        w.setdefault("model", "")
        w.setdefault("created_at", 0)
        # 显示名称空 → 回填模型 ID（与新建时的默认行为一致）
        if not w.get("name"):
            w["name"] = w["model"] or PROVIDER_TEMPLATES.get(w["provider"], {}).get("name", "模型")
    data["providers"] = items
    ids = {w["id"] for w in items}
    if data.get("active") not in ids:
        data["active"] = items[0]["id"]
    # 推理档位：旧时代的 off/medium 与非法值一律归一到新三档（low/high/max）
    data["reasoning"] = normalize_reasoning(data.get("reasoning")) or _DEFAULT_REASONING
    return data


def _save_raw(data: dict) -> None:
    os.makedirs(os.path.dirname(_MODELS_FILE), exist_ok=True)
    tmp = _MODELS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _MODELS_FILE)


# ---------------------------------------------------------------- 查询

def _entries() -> list[dict]:
    return _normalize(_load_raw())["providers"]


def get_entry(pid: str) -> dict | None:
    for w in _entries():
        if w["id"] == pid:
            return w
    return None


def active_id() -> str:
    return _normalize(_load_raw())["active"]


def active_entry() -> dict | None:
    return get_entry(active_id())


def reasoning_level() -> str:
    """当前全局推理等级（low/high/max）；新会话/未显式设置的会话用它做缺省。"""
    return _normalize(_load_raw())["reasoning"]


def reasoning_spec(provider: str) -> dict:
    """厂家的推理参数说明：支持哪些等级（levels）+ 参数风格（style）+ 取值映射（values）。
    未知厂家 / 未声明 → 通用 effort（三档，接口不认时由 agent 降级重试兜底）。
    """
    tpl = PROVIDER_TEMPLATES.get(provider) or {}
    spec = tpl.get("reasoning")
    if not isinstance(spec, dict) or not spec.get("levels"):
        return dict(_GENERIC_RSPEC)
    return spec


def provider_key(provider: str) -> str:
    """取某服务商配置里的 API Key（供联网搜索等旁路能力复用）。

    优先当前生效的那条，其次该服务商第一条有 Key 的记录；都没有返回空串。
    """
    data = _normalize(_load_raw())
    items = data["providers"]
    active = next((w for w in items if w["id"] == data["active"]), None)
    if active and active.get("provider") == provider and active.get("api_key"):
        return active["api_key"]
    for w in items:
        if w.get("provider") == provider and w.get("api_key"):
            return w["api_key"]
    return ""


def resolve_model(mid: str | None = None) -> dict:
    """按 id 解析一份模型配置；id 为空 / 已被删掉 → 回退全局 active。
    返回 {model_id, provider, name, base_url, api_key, model}，空字段回退环境变量。
    供会话层（webui._Session.ensure_agent）按会话维度取模型。
    """
    w = get_entry(mid) if mid else None
    if w is None:
        w = active_entry() or {}
    return {
        "model_id": w.get("id") or "",
        "provider": w.get("provider", "custom"),
        "name": w.get("name", ""),
        "base_url": w.get("base_url") or os.environ.get("OPENAI_BASE_URL", ""),
        "api_key": w.get("api_key") or os.environ.get("OPENAI_API_KEY", ""),
        "model": w.get("model") or os.environ.get("OPENAI_MODEL", ""),
    }


def list_public() -> list[dict]:
    """给前端的列表：api_key 换成脱敏串，另给 has_key / is_active。"""
    act = active_id()
    out = []
    for w in _entries():
        out.append({
            "id": w["id"],
            "provider": w["provider"],
            "name": w["name"],
            "base_url": w["base_url"],
            "model": w["model"],
            "api_key_masked": mask_key(w["api_key"]),
            "has_key": bool(w["api_key"]),
            "is_active": w["id"] == act,
            "created_at": w["created_at"],
        })
    return out


def resolve_active() -> dict:
    """交给 agent 的三个值：base_url / api_key / model。

    记录里的字段若为空，回退到环境变量 —— 这样「只填一半」的配置
    以及「没写配置文件、纯用 .env」的老用法都能正常工作。
    """
    w = active_entry() or {}
    return {
        "name": w.get("name", ""),
        "base_url": w.get("base_url") or os.environ.get("OPENAI_BASE_URL", ""),
        "api_key": w.get("api_key") or os.environ.get("OPENAI_API_KEY", ""),
        "model": w.get("model") or os.environ.get("OPENAI_MODEL", ""),
    }


# ---------------------------------------------------------------- 变更

def create_entry(provider: str, name: str, base_url: str, api_key: str, model: str) -> dict:
    provider = provider if provider in PROVIDER_TEMPLATES else "custom"
    tpl = PROVIDER_TEMPLATES[provider]
    base_url = (base_url or tpl["base_url"]).strip()
    model = (model or "").strip()
    if not base_url:
        raise ValueError("接口地址不能为空")
    if not model:
        raise ValueError("模型名不能为空")
    if not api_key and not tpl.get("no_key"):
        raise ValueError("请填写 API Key")
    data = _normalize(_load_raw())
    item = {
        "id": uuid.uuid4().hex[:12],
        "provider": provider,
        # 显示名称留空时等于模型 ID（前端也会在输入模型名时自动带出）
        "name": (name or model).strip() or model,
        "base_url": base_url,
        "api_key": api_key or "",
        "model": model,
        "created_at": time.time(),
    }
    data["providers"].append(item)
    _save_raw(data)
    return item


def update_entry(
    pid: str,
    name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """更新一条记录。api_key=None 表示不改（前端留空即视为不改）。"""
    data = _normalize(_load_raw())
    target = next((w for w in data["providers"] if w["id"] == pid), None)
    if target is None:
        raise ValueError("模型配置不存在")
    if model is not None:
        if not model.strip():
            raise ValueError("模型名不能为空")
        target["model"] = model.strip()
    if name is not None:
        # 显示名称留空时回到模型 ID（与新建时的默认规则一致）
        target["name"] = name.strip() or target["model"]
    if base_url is not None:
        if not base_url.strip():
            raise ValueError("接口地址不能为空")
        target["base_url"] = base_url.strip()
    if api_key is not None and api_key != "":     # 空串 = 保持原 key
        target["api_key"] = api_key
    _save_raw(data)
    return target


def delete_entry(pid: str) -> None:
    data = _normalize(_load_raw())
    if len(data["providers"]) <= 1:
        raise ValueError("至少保留一个模型配置")
    before = len(data["providers"])
    data["providers"] = [w for w in data["providers"] if w["id"] != pid]
    if len(data["providers"]) == before:
        raise ValueError("模型配置不存在")
    if data.get("active") == pid:
        data["active"] = data["providers"][0]["id"]
    _save_raw(data)


def set_reasoning(level: str) -> str:
    """设置全局推理档位；非法值抛 ValueError。"""
    if level not in REASONING_LEVELS:
        raise ValueError("推理档位必须是 low / high / max 之一")
    data = _normalize(_load_raw())
    data["reasoning"] = level
    _save_raw(data)
    return level


def set_active(pid: str) -> dict:
    data = _normalize(_load_raw())
    if not any(w["id"] == pid for w in data["providers"]):
        raise ValueError("模型配置不存在")
    data["active"] = pid
    _save_raw(data)
    return get_entry(pid)     # type: ignore[return-value]


# ---------------------------------------------------------------- 连接测试

def test_entry(pid: str, timeout: float = 8.0) -> dict:
    """按已存配置做连接测试。"""
    w = get_entry(pid)
    if not w:
        return {"ok": False, "error": "模型配置不存在"}
    return test_values(w["base_url"], w["api_key"], timeout=timeout)


def test_values(base_url: str, api_key: str, timeout: float = 8.0) -> dict:
    """按裸参数做连接测试（供「保存前先测」用）。

    返回 {ok: bool, models: [...], error: str}。
    为什么用 GET /models？OpenAI 兼容规范要求实现它，且不消耗 token。
    部分服务商不暴露该接口，此时退化为「能建连即视为通过」。
    """
    import urllib.error
    import urllib.request

    base = (base_url or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "error": "接口地址为空"}
    url = base + "/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": True, "models": [], "note": "接口未返回标准 JSON，但连接正常"}
        models = [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
        return {"ok": True, "models": models or []}
    except urllib.error.HTTPError as e:
        # 401/403 说明能连上但鉴权失败 —— 分开报，方便定位
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        hint = "API Key 无效或无权限" if e.code in (401, 403) else f"HTTP {e.code}"
        return {"ok": False, "error": f"{hint}：{detail}" if detail else hint}
    except Exception as e:  # noqa: BLE001 —— 网络/DNS/超时统一处理
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
