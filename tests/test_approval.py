"""
授权审批的安全默认值测试 —— 纯函数 + 假 asker，不碰网络、不烧 token。

核心契约：审批是安全控制，「没有确认通道」不等于「用户同意」。
  · 无 asker      → 默认拒绝（fail-closed），除非显式 MINI_AGENT_ALLOW_NO_ASKER=1；
  · asker 返回真  → 批准；
  · asker 返回假  → 拒绝；
  · asker 抛异常  → 按拒绝处理，不外抛。
"""

from __future__ import annotations

import os

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区

from core import approval  # noqa: E402


def _clear_opt_in() -> None:
    os.environ.pop(approval._NO_ASKER_ENV, None)


def test_no_asker_denies_by_default() -> None:
    """无确认通道时必须拒绝 —— 这是原来 fail-open 的回归点。"""
    approval.set_asker(None)
    _clear_opt_in()
    try:
        out = approval.request_approval("删除整个目录")
        assert out.startswith("[denied]"), out
        assert "没有人工确认通道" in out
    finally:
        _clear_opt_in()


def test_no_asker_opt_in_allows() -> None:
    """脚本/无头批处理显式 opt-in 后才放行。"""
    approval.set_asker(None)
    os.environ[approval._NO_ASKER_ENV] = "1"
    try:
        out = approval.request_approval("批量重命名")
        assert out.startswith("[approved]"), out
    finally:
        _clear_opt_in()


def test_asker_approve_and_reject() -> None:
    try:
        approval.set_asker(lambda _r: True)
        assert approval.request_approval("x").startswith("[approved]")
        approval.set_asker(lambda _r: False)
        assert approval.request_approval("x").startswith("[denied]")
    finally:
        approval.set_asker(None)


def test_asker_failure_is_denied() -> None:
    def boom(_r):
        raise RuntimeError("桥炸了")

    try:
        approval.set_asker(boom)
        out = approval.request_approval("x")
        assert out.startswith("[denied]"), out
        assert "询问失败" in out
    finally:
        approval.set_asker(None)


if __name__ == "__main__":
    for _t in (test_no_asker_denies_by_default, test_no_asker_opt_in_allows,
               test_asker_approve_and_reject, test_asker_failure_is_denied):
        _t()
    print("✓ 审批默认值 4 项通过")
