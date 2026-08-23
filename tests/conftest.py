"""测试公共夹具：路径注入与常用假对象（对齐仓库其他技能的 conftest 惯例）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1].parent
sys.path.insert(0, str(ROOT / "common_core" / "src"))
sys.path.insert(0, str(ROOT / "nl2sql_skill" / "src"))


class FakeLLM:
    """可编程的假 LLM：按顺序返回预设 JSON；记录调用次数供断言。"""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def chat_json(self, messages: list[dict], *, system_prompt: str = "") -> dict:
        """弹出下一个预设响应；耗尽后返回空字典模拟解析失败。"""
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return {}


class FakeCache:
    """进程内字典缓存，接口与 RedisCache 对齐（get/set）。"""

    def __init__(self) -> None:
        self.store: dict[tuple, str] = {}

    def get(self, scope: str, material: str, *, tenant_id: str = "", kb_id: str = "") -> str | None:
        return self.store.get((tenant_id, scope, material))

    def set(self, scope: str, material: str, value: str, *, ttl=None, tenant_id: str = "", kb_id: str = "") -> bool:
        self.store[(tenant_id, scope, material)] = value
        return True
