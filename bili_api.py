"""B 站接口封装（异步，aiohttp）。

提供以下能力：

1. ``fetch_dynamics``：拉取指定 UP 主的最新动态。优先请求完整动态接口；
   由于匿名状态下该接口存在概率性风控（偶发返回空数据或错误页），
   失败后自动降级到匿名更稳定的图文 opus 接口；
2. ``enrich_dynamic`` / ``fetch_vote_info``：补全动态附加内容（投票标题与选项）；
3. ``get_user_detail``：获取 UP 主昵称、头像、等级与粉丝等统计数据。

所有请求都会尽可能携带匿名 Cookie（buvid3/buvid4），以降低风控概率；
若在插件配置中填写了 SESSDATA，也会一并携带，可显著提高完整动态接口
的稳定性。
"""

from __future__ import annotations

import asyncio
import json

import aiohttp

from astrbot.api import logger

from .dynamic_parser import (
    fmt_count,
    normalize_url,
    parse_full_item,
    parse_opus_item,
)

# 浏览器 UA：B 站接口对非常规 UA 的风控更严格
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 完整动态接口建议携带的 features 参数
_FEATURES = (
    "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,"
    "onlyfansAssetsV2,ugcDelete,onlyfansQaCard"
)

_FULL_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
_OPUS_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/feed/space"
_VOTE_API = "https://api.vc.bilibili.com/vote_svr/v1/vote_svr/vote_info"
_CARD_API = "https://api.bilibili.com/x/web-interface/card"
_SPI_API = "https://api.bilibili.com/x/frontend/finger/spi"


class BiliClient:
    """B 站接口客户端。一个插件实例共用一个客户端。"""

    def __init__(self, sessdata: str = ""):
        self._sessdata = (sessdata or "").strip()
        self._session: aiohttp.ClientSession | None = None
        self._cookie = ""
        self._cookie_ready = False

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": _UA},
            )
            self._cookie_ready = False
        if not self._cookie_ready:
            self._cookie_ready = True  # 只尝试初始化一次
            await self._init_cookie()
        return self._session

    async def _init_cookie(self):
        """获取匿名 buvid Cookie；失败时静默跳过。"""
        parts: list[str] = []
        try:
            async with self._session.get(_SPI_API) as resp:  # type: ignore[union-attr]
                data = await resp.json(content_type=None)
            spi = (data or {}).get("data") or {}
            if spi.get("b_3"):
                parts.append(f"buvid3={spi['b_3']}")
            if spi.get("b_4"):
                parts.append(f"buvid4={spi['b_4']}")
        except Exception as e:
            logger.debug(f"[bili] 获取匿名 Cookie 失败（不影响使用）：{e}")
        if self._sessdata:
            parts.append(f"SESSDATA={self._sessdata}")
        self._cookie = "; ".join(parts)

    async def _get_json(self, url: str, params: dict | None = None, referer: str = "https://www.bilibili.com/", timeout: int = 12) -> dict | None:
        """GET 请求并解析 JSON；网络异常 / 非 JSON 响应返回 None。"""
        session = await self._ensure_session()
        headers = {"Referer": referer}
        if self._cookie:
            headers["Cookie"] = self._cookie
        try:
            async with session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            return data
        except Exception as e:
            logger.debug(f"[bili] 请求 {url} 失败：{e}")
            return None

    # ------------------------------------------------------------------
    # 动态
    # ------------------------------------------------------------------

    async def fetch_dynamics(self, mid: str | int) -> list[dict] | None:
        """拉取某 UP 主的最新动态（已解析为标准结构）。

        Returns:
            - list：动态列表（可能为空列表，表示确实没有动态）；
            - None：所有接口均请求失败，本轮放弃处理。
        """
        mid = str(mid)

        # 1) 完整动态接口：重试两次（风控降级时也常返回空数据，所以空结果也重试）
        for attempt in range(2):
            data = await self._get_json(
                _FULL_API,
                params={
                    "host_mid": mid,
                    "timezone_offset": "-480",
                    "features": _FEATURES,
                    "web_location": "333.999",
                },
                referer=f"https://space.bilibili.com/{mid}/dynamic",
            )
            if data is not None and data.get("code") == 0:
                raw_items = ((data.get("data") or {}).get("items")) or []
                if raw_items:
                    parsed = [p for p in (parse_full_item(i) for i in raw_items) if p]
                    if parsed:
                        return parsed
            if attempt == 0:
                await asyncio.sleep(1.5)

        # 2) 降级：图文 opus 接口（匿名可用性更高，但只覆盖图文动态）
        data = await self._get_json(
            _OPUS_API,
            params={"host_mid": mid, "type": "all"},
            referer=f"https://space.bilibili.com/{mid}/dynamic",
        )
        if data is not None and data.get("code") == 0:
            raw_items = ((data.get("data") or {}).get("items")) or []
            return [p for p in (parse_opus_item(i) for i in raw_items) if p]
        return None

    # ------------------------------------------------------------------
    # 附加内容（投票等）
    # ------------------------------------------------------------------

    async def enrich_dynamic(self, dynamic: dict) -> None:
        """补全动态中渲染所需的附加数据（当前：投票标题与选项）。

        就地对 ``dynamic["additional"]`` 补充 ``options``，并以投票接口的
        标题为准修正 ``title``（实测动态详情接口的 ``vote.desc`` 偶尔会
        返回“xx人参与”这类展示文案而非真实标题）；拉取失败时保持原值，
        渲染时降级为只显示标题与参与人数。
        """
        additional = (dynamic or {}).get("additional") or {}
        if additional.get("kind") == "vote" and additional.get("vote_id"):
            info = await self.fetch_vote_info(additional["vote_id"])
            if info:
                if info.get("title"):
                    additional["title"] = info["title"]
                if info.get("options"):
                    additional["options"] = info["options"]

    async def fetch_vote_info(self, vote_id: str) -> dict | None:
        """拉取投票详情（标题与选项票数）。

        Returns:
            ``{"title": str, "options": [{"idx", "desc", "count"}]}``
            （count 已格式化为展示字符串）；请求失败返回 None。
        """
        data = await self._get_json(
            _VOTE_API,
            params={"vote_id": str(vote_id)},
            referer="https://www.bilibili.com/",
        )
        if data is None or data.get("code") != 0:
            return None
        info = (data.get("data") or {}).get("info") or {}
        options = []
        for opt in info.get("options") or []:
            if not isinstance(opt, dict):
                continue
            desc = str(opt.get("desc") or "").strip()
            if not desc:
                continue
            options.append(
                {
                    "idx": opt.get("idx"),
                    "desc": desc,
                    "count": fmt_count(opt.get("cnt")),
                }
            )
        return {
            "title": str(info.get("title") or "").strip(),
            "options": options,
        }

    # ------------------------------------------------------------------
    # UP 信息 / 搜索
    # ------------------------------------------------------------------

    async def get_user_detail(self, mid: str | int) -> dict | None:
        """获取 UP 主详细信息（card 接口）。

        Returns:
            - dict：``{"name", "face", "level", "follower", "following",
              "videos", "likes"}``（数值均已格式化为展示字符串）；
            - ``{"not_found": True}``：B 站明确返回该 UID 不存在；
            - ``None``：请求失败（网络 / 风控），无法判断 UID 是否有效。
        """
        mid = str(mid)
        data = await self._get_json(
            _CARD_API,
            params={"mid": mid, "photo": "false"},
            referer=f"https://space.bilibili.com/{mid}",
        )
        if data is None:
            return None
        if data.get("code") == -404:
            return {"not_found": True}
        if data.get("code") != 0:
            return None
        payload = data.get("data") or {}
        card = payload.get("card") or {}
        name = str(card.get("name") or "").strip()
        if not name:
            return None
        level_info = card.get("level_info") or {}
        return {
            "name": name,
            "face": normalize_url(card.get("face")),
            "level": int(level_info.get("current_level") or 0),
            "follower": fmt_count(payload.get("follower")),
            "following": fmt_count(card.get("attention")),
            "videos": fmt_count(payload.get("archive_count")),
            "likes": fmt_count(payload.get("like_num")),
        }

    # ------------------------------------------------------------------

    async def close(self):
        """关闭底层 HTTP 会话。"""
        if self._session and not self._session.closed:
            await self._session.close()
