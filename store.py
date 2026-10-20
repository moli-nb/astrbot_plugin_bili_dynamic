"""插件数据存储。

数据结构（state.json）::

    {
        "version": 1,
        "subs": {
            "<会话 unified_msg_origin>": {
                "mids": ["12345678", ...],   # 该会话订阅的 UP 主 UID
                "updated_at": 1792070000
            }
        },
        "ups": {
            "<mid>": {
                "name": "UP 昵称",
                "face": "头像 URL",
                "latest_id": 1234567890,   # 已处理到的最新动态 id（全局水位线）
                "activated_at": 1792070000,  # 最近一次激活监控的时间戳
                "partial_baseline": false,   # 基线是否取自降级（不完整）数据
                "updated_at": 1792070000
            }
        }
    }

- ``subs`` 记录“哪个会话订阅了哪些 UP 主”，推送时按此分发；
- ``ups`` 是全局的 UP 主监控状态，多个会话订阅同一 UP 主时共享水位线，
  保证同一动态不会重复推送、也不会漏推。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from astrbot.api import logger


class SubStore:
    """订阅关系与 UP 状态的 JSON 文件存储（原子写入）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {"version": 1, "subs": {}, "ups": {}}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    async def load(self):
        """从磁盘加载数据；文件不存在或损坏时使用空数据。"""
        try:
            if self.path.exists():
                raw = await asyncio.to_thread(self.path.read_text, "utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    self.data["version"] = data.get("version", 1)
                    self.data["subs"] = data.get("subs") or {}
                    self.data["ups"] = data.get("ups") or {}
                logger.info(f"[bili] 数据已加载：{len(self.data['subs'])} 个订阅会话")
        except Exception as e:
            logger.error(f"[bili] 读取数据文件失败：{e}", exc_info=True)

    async def save(self):
        """保存数据到磁盘（先写临时文件再原子替换）。"""
        async with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                text = json.dumps(self.data, ensure_ascii=False, indent=2)
                await asyncio.to_thread(tmp.write_text, text, "utf-8")
                await asyncio.to_thread(tmp.replace, self.path)
            except Exception as e:
                logger.error(f"[bili] 保存数据文件失败：{e}", exc_info=True)

    # ------------------------------------------------------------------
    # 会话订阅关系
    # ------------------------------------------------------------------

    def get_subs(self, umo: str) -> list[str]:
        """返回某会话订阅的所有 UP 主 mid。"""
        node = self.data["subs"].get(umo) or {}
        return [str(m) for m in (node.get("mids") or [])]

    def is_subscribed(self, umo: str, mid: str) -> bool:
        return str(mid) in self.get_subs(umo)

    def add_sub(self, umo: str, mid: str):
        node = self.data["subs"].setdefault(umo, {"mids": [], "updated_at": 0})
        mids = node.setdefault("mids", [])
        if str(mid) not in mids:
            mids.append(str(mid))
        node["updated_at"] = int(time.time())

    def remove_sub(self, umo: str, mid: str) -> bool:
        node = self.data["subs"].get(umo)
        if not node:
            return False
        mids = node.get("mids") or []
        if str(mid) not in mids:
            return False
        mids.remove(str(mid))
        node["updated_at"] = int(time.time())
        if not mids:
            self.data["subs"].pop(umo, None)
        return True

    def subscribers_of(self, mid: str) -> list[str]:
        """返回订阅了某 UP 主的所有会话（umo）。"""
        mid = str(mid)
        return [
            umo
            for umo, node in self.data["subs"].items()
            if mid in (node.get("mids") or [])
        ]

    def active_mids(self) -> list[str]:
        """所有被至少一个会话订阅的 mid（全局监控集合）。"""
        mids = set()
        for node in self.data["subs"].values():
            for m in node.get("mids") or []:
                mids.add(str(m))
        return sorted(mids)

    def session_count(self) -> int:
        return len(self.data["subs"])

    # ------------------------------------------------------------------
    # UP 主全局状态
    # ------------------------------------------------------------------

    def get_up(self, mid: str) -> dict | None:
        return self.data["ups"].get(str(mid))

    def upsert_up(self, mid: str, **fields) -> dict:
        node = self.data["ups"].setdefault(str(mid), {})
        node.update(fields)
        node["updated_at"] = int(time.time())
        return node

    def up_name(self, mid: str) -> str:
        node = self.get_up(mid) or {}
        return node.get("name") or f"UID{mid}"
