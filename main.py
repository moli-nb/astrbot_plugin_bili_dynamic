"""B 站 UP 主动态订阅推送插件（QQ 官方机器人 Markdown 排版）。

功能概览：

- **全局监控**：所有被任意会话订阅的 UP 主统一去重轮询（多个群订阅同一
  UP 主只会请求一次），水位线（最新已处理动态 id）全局共享；
- **按会话推送**：每个会话（群聊 / 私聊）通过指令独立维护自己的订阅列表，
  UP 主发布新动态时只推送给订阅了它的会话；
- **Markdown 排版**：推送使用 QQ 官方机器人原生 Markdown（``use_markdown_``
  标记），图文 / 视频 / 转发等类型有各自的卡片模板；
- **置顶动态处理**：置顶动态不会作为“新动态”被重复推送（判断依据是动态
  id 水位线而不是列表顺序）；订阅时也不会推送任何历史动态（包括当时的
  置顶动态）；UP 主「新发布并被置顶」的动态会正常推送并带有（置顶）标记；
- **接口降级**：完整动态接口被风控（返回空数据 / 错误页）时自动降级到匿名
  更稳定的图文 opus 接口，保证基本可用。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter, AstrMessageEvent
from astrbot.api.event.filter import CustomFilter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .bili_api import BiliClient
from .md_renderer import format_pic_url, format_ts, render_push_markdown
from .store import SubStore

try:
    # 新版 AstrBot：GreedyStr 用于接收「指令之后的所有剩余文本」（含空格）
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # 兼容旧版：退化为普通字符串参数
    class GreedyStr(str):
        pass

_PLUGIN_NAME = "astrbot_plugin_bili_dynamic"
_MANUAL_CHECK_COOLDOWN = 60  # 「检查」指令冷却（秒）


def _cmd_input(text: str, show: str = "") -> str:
    """生成 QQ 机器人「参数指令」点击标签。

    点击后 ``text`` 会插入到用户输入框（用户自行编辑发送）；``show`` 为
    消息内展示的文本（默认为 text）。
    """
    show_attr = f' show="{show}"' if show else ""
    return f'<qqbot-cmd-input text="{text}"{show_attr} reference="false" />'


def _help_text() -> str:
    """帮助消息文本（指令均为可点击标签）。"""
    return (
        "B站动态订阅插件指令（点击标签填入指令，发送即可）：\n"
        f"{_cmd_input('/bili 订阅 ', '订阅')} 订阅 UP 主动态（UID）\n"
        f"{_cmd_input('/bili 取消订阅 ', '取消订阅')} 取消订阅（UID）\n"
        f"{_cmd_input('/bili 订阅列表', '订阅列表')} 查看本会话订阅列表\n"
        f"{_cmd_input('/bili 测试', '测试')} 立即检查一轮动态更新\n"
        f"{_cmd_input('/bili 状态', '状态')} 查看插件运行状态\n"
        f"{_cmd_input('/bili 帮助', '帮助')} 显示本帮助"
    )


class _BareGroupFilter(CustomFilter):
    """指令组「纯组名调用」拦截器。

    AstrBot 框架对纯组名调用（如仅发送 ``/bili``）的默认行为是抛出
    “参数不足”并渲染指令树。本过滤器在消息恰为纯组名时返回 False，
    让指令组静默跳过匹配，改由「帮助」指令的 ``_BareBiliRedirectFilter``
    将该调用重定向为 ``bili 帮助`` 并响应帮助内容。
    """

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        try:
            msg = (event.message_str or "").strip()
        except Exception:
            return True
        return msg != "bili"


class _BareBiliRedirectFilter(CustomFilter):
    """纯组名调用重定向过滤器（挂在「帮助」子指令上）。

    当消息恰为纯 ``bili``（用户发送 /bili 且已唤醒）时，把消息文本改写为
    ``bili 帮助``，使该调用由「帮助」指令接管响应；其余消息不做处理。
    （AstrBot 唤醒处理本身也会重写该属性以剥离唤醒前缀，此处沿用同一机制。）
    """

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        try:
            if (
                (event.message_str or "").strip() == "bili"
                and getattr(event, "is_at_or_wake_command", False)
            ):
                event.message_str = "bili 帮助"
        except Exception:
            pass
        return True


@register(
    _PLUGIN_NAME,
    "moli",
    "B站 UP 主动态订阅推送（QQ 官方机器人 Markdown 排版）",
    "1.0.0",
)
class BiliDynamicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config: AstrBotConfig = config or {}

        plugin_name = getattr(self, "name", None) or _PLUGIN_NAME
        # 兼容不同 AstrBot 版本：get_astrbot_data_path() 有 str / Path 两种返回
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        self.store = SubStore(data_dir / "state.json")
        self.api = BiliClient(sessdata=str(self.config.get("sessdata") or ""))

        self._poll_task: asyncio.Task | None = None
        self._boot_task: asyncio.Task | None = None
        self._closing = False
        self._poll_lock = asyncio.Lock()
        self._last_check_ts = 0
        self._last_error = ""
        self._last_manual_check = 0.0

        self._boot_task = asyncio.create_task(self._bootstrap())

    async def _bootstrap(self):
        """加载持久化数据并启动全局轮询任务。"""
        await self.store.load()
        if self._closing:
            return
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self):
        """插件被卸载 / 停用时回收后台任务与 HTTP 会话。"""
        self._closing = True
        for task in (self._boot_task, self._poll_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._boot_task = None
        self._poll_task = None
        await self.api.close()

    # ==================================================================
    # 全局轮询
    # ==================================================================

    async def _poll_loop(self):
        """轮询主循环：按配置间隔依次检查所有被订阅的 UP 主。"""
        await asyncio.sleep(12)  # 等待 AstrBot 各平台适配器完全就绪
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = f"轮询异常：{e}"
                logger.error(f"[bili] 轮询异常：{e}", exc_info=True)
            interval = int(self.config.get("poll_interval") or 120)
            await asyncio.sleep(max(30, interval))

    async def _poll_once(self):
        """执行一轮完整检查（自动轮询与手动「检查」指令共用，互斥执行）。"""
        async with self._poll_lock:
            self._last_error = ""  # 新一轮开始，清除上一轮的错误记录
            self._last_check_ts = int(time.time())
            mids = self.store.active_mids()
            for idx, mid in enumerate(mids):
                try:
                    await self._check_one(mid)
                except Exception as e:
                    self._last_error = f"检查 UID{mid} 出错：{e}"
                    logger.error(f"[bili] 检查 UID{mid} 出错：{e}", exc_info=True)
                if idx + 1 < len(mids):
                    # 控制请求节奏，降低被风控的概率
                    await asyncio.sleep(random.uniform(1.0, 2.0))

    async def _check_one(self, mid: str):
        """检查单个 UP 主：拉取动态 -> 与水位线比对 -> 推送给订阅会话。"""
        items = await self.api.fetch_dynamics(mid)
        if items is None:
            self._last_error = f"UID{mid} 动态接口请求失败"
            return
        if not items:
            # 空数据：可能是账号确实没有动态，也可能是匿名接口被风控降级
            # （实测同一 UP 主时好时坏），本轮跳过，下轮再试
            return

        items.sort(key=lambda x: int(x["id"]))
        latest_item = items[-1]
        latest_id = int(latest_item["id"])

        author = latest_item.get("author") or {}
        name = (author.get("name") or "").strip()
        face = (author.get("face") or "").strip()

        up = self.store.get_up(mid) or {}
        if not name:
            name = up.get("name") or f"UID{mid}"
        if not face:
            face = up.get("face") or ""

        baseline = up.get("latest_id")
        if baseline is None:
            # 尚未建立水位线（新激活或此前一直拉取失败）：
            # 只记录基线，不推送历史动态（自然也不会推送当时的置顶动态）
            self.store.upsert_up(mid, name=name, face=face, latest_id=latest_id)
            await self.store.save()
            return

        changed = False

        partial_baseline = bool(up.get("partial_baseline"))
        activated_at = int(up.get("activated_at") or 0)

        # 与水位线比对，收集候选项；若基线取自降级数据（不完整），
        # 发布时间早于订阅时刻的补漏动态按历史处理（仅推进水位线）
        candidates = [it for it in items if int(it["id"]) > int(baseline)]
        new_items = []
        for it in candidates:
            if partial_baseline and activated_at:
                pub_ts = int(it.get("pub_ts") or 0)
                if pub_ts and pub_ts < activated_at:
                    continue
            new_items.append(it)

        # 部分基线使命完成：本轮已拿到全量数据（含图文以外的动态类型）即恢复常规判断
        if partial_baseline and any(it.get("source") == "full" for it in items):
            self.store.upsert_up(mid, partial_baseline=False)
            changed = True

        if candidates:
            max_push = max(1, int(self.config.get("max_push_per_poll") or 5))
            if len(new_items) > max_push:
                # 一次更新过多（例如插件长时间离线后）：只推送最新的几条，避免刷屏
                new_items = new_items[-max_push:]
            push_pinned = bool(self.config.get("push_pinned", True))
            block_vote_join = bool(self.config.get("block_vote_join", True))
            subscribers = self.store.subscribers_of(mid)
            for it in new_items:
                if it.get("pinned") and not push_pinned:
                    # 配置为不推送置顶动态：跳过（水位线仍会正常推进）
                    continue
                if block_vote_join and it.get("vote_join"):
                    # 全局开关：屏蔽「参与投票」类动态（水位线仍会正常推进）
                    continue
                try:
                    await self.api.enrich_dynamic(it)  # 补全投票选项等附加数据
                    md = render_push_markdown(it, up_info={"name": name, "face": face})
                except Exception as e:
                    # 单条渲染失败不阻塞其余动态（例如数据异常）
                    logger.error(
                        f"[bili] 动态 {it.get('id_str')} 渲染失败，已跳过：{e}",
                        exc_info=True,
                    )
                    continue
                for umo in subscribers:
                    await self._send_markdown(umo, md)
                    await asyncio.sleep(0.6)
            self.store.upsert_up(mid, latest_id=latest_id)
            changed = True

        if up.get("name") != name or up.get("face") != face:
            self.store.upsert_up(mid, name=name, face=face)
            changed = True

        if changed:
            await self.store.save()

    async def _send_markdown(self, umo: str, content: str):
        """以原生 Markdown 方式向指定会话发送消息。"""
        try:
            chain = MessageChain(chain=[Plain(content)], use_markdown_=True)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.warning(f"[bili] 推送到 {umo} 失败：{e}", exc_info=True)

    # ==================================================================
    # 指令
    # ==================================================================

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """综合判断发送者是否为管理员（框架判定 + 全局 admins_id 名单）。"""
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            admins = (self.context.get_config() or {}).get("admins_id") or []
        except Exception:
            admins = []
        sender_id = str(event.get_sender_id() or "")
        return bool(sender_id) and sender_id in {str(a) for a in admins}

    def _admin_block(self, event: AstrMessageEvent) -> str:
        """开启仅管理员模式且发送者非管理员时，返回提示文本。"""
        if bool(self.config.get("admin_only", False)) and not self._is_admin(event):
            return "仅管理员可以管理订阅（可在插件配置中关闭此限制）。"
        return ""

    @filter.command_group("bili")
    def bili(self):
        """B站动态订阅"""
        pass

    # 拦截「纯 /bili」调用（框架默认会渲染“参数不足”的指令树错误），
    # 使纯组名调用被静默跳过；随后由「帮助」指令的重定向过滤器接管响应。
    bili.custom_filter(_BareGroupFilter)(bili)

    @bili.command("订阅", alias={"关注", "subscribe"})
    async def bili_subscribe(self, event: AstrMessageEvent, target: GreedyStr):
        """订阅一个 UP 主的动态：/bili 订阅 <UID>"""
        block = self._admin_block(event)
        if block:
            yield event.plain_result(block)
            return

        target = str(target or "").strip()
        if not target:
            yield event.plain_result(
                f"用法：/bili 订阅 <UID>\n{_cmd_input('/bili 订阅 ', '点我重试')}"
            )
            return

        # 仅支持 UID（纯数字）方式
        if not re.fullmatch(r"\d+", target):
            yield event.plain_result(
                f"请输入 UP 主的 UID（纯数字）\n{_cmd_input('/bili 订阅 ', '点我重试')}"
            )
            return
        mid = target

        umo = event.unified_msg_origin
        if self.store.is_subscribed(umo, mid):
            yield event.plain_result(
                f"本会话已订阅 {self.store.up_name(mid)}（UID {mid}），无需重复订阅。"
            )
            return

        # 获取 UP 主详细信息（同时校验 UID 是否存在）
        info = await self.api.get_user_detail(mid)
        if info and info.get("not_found"):
            yield event.plain_result(f"未找到 UID {mid} 对应的 UP 主，请检查后重试。")
            return
        info = info or {}
        name = str(info.get("name") or "").strip()
        face = str(info.get("face") or "").strip()

        # 建立 / 刷新监控基线：不推送历史动态（包括当时的置顶动态）。
        # 若该 UP 主已有其它会话在监控，说明水位线在正常运转，直接沿用。
        activated = bool(self.store.subscribers_of(mid))
        existing = self.store.get_up(mid) or {}
        now_ts = int(time.time())
        if not activated:
            # 全新激活：以当前最新动态为水位线
            items = await self.api.fetch_dynamics(mid)
            latest_id = None
            partial = False
            if items:
                newest = max(items, key=lambda x: int(x["id"]))
                latest_id = int(newest["id"])
                name = name or (newest.get("author") or {}).get("name") or ""
                face = face or (newest.get("author") or {}).get("face") or ""
                # 若基线取自降级数据（只覆盖图文动态），标记为“部分基线”：
                # 待全量接口恢复后，发布时间早于订阅时刻的补漏动态会被跳过
                partial = not any(it.get("source") == "full" for it in items)
            if not name:
                name = f"UID{mid}"
            self.store.upsert_up(
                mid,
                name=name,
                face=face,
                latest_id=latest_id,
                activated_at=now_ts,
                partial_baseline=partial,
            )
        else:
            if not name:
                name = f"UID{mid}"
            self.store.upsert_up(
                mid, name=name, face=face, latest_id=existing.get("latest_id")
            )
        self.store.add_sub(umo, mid)
        await self.store.save()

        # 订阅成功卡片（Markdown）：头像+订阅成功 / 名称+等级 / UID / 数据统计
        head = ""
        if face:
            head += f"![avatar #15px #15px]({format_pic_url(face, 15, 15)}) "
        head += "订阅成功~"
        lines = [head]
        name_line = f"**{name}**"
        if "level" in info:
            name_line += f" Lv{info['level']}"
        lines.append(name_line)
        lines.append(f"{mid}")
        stat_line1 = []
        if info.get("follower"):
            stat_line1.append(f"粉丝数 {info['follower']}")
        if info.get("following"):
            stat_line1.append(f"关注数 {info['following']}")
        if stat_line1:
            lines.append(" ".join(stat_line1))
        stat_line2 = []
        if info.get("videos"):
            stat_line2.append(f"视频数 {info['videos']}")
        if info.get("likes"):
            stat_line2.append(f"获赞数 {info['likes']}")
        if stat_line2:
            lines.append(" ".join(stat_line2))
        yield event.plain_result("\n".join(lines))

    @bili.command("取消订阅", alias={"取关", "unsubscribe"})
    async def bili_unsubscribe(self, event: AstrMessageEvent, target: GreedyStr):
        """取消订阅一个 UP 主：/bili 取消订阅 <UID>"""
        block = self._admin_block(event)
        if block:
            yield event.plain_result(block)
            return

        umo = event.unified_msg_origin
        subs = self.store.get_subs(umo)
        if not subs:
            yield event.plain_result("本会话还没有订阅任何 UP 主。")
            return

        target = str(target or "").strip()
        if not target:
            yield event.plain_result(
                f"用法：/bili 取消订阅 <UID>\n{_cmd_input('/bili 取消订阅 ', '点我重试')}"
            )
            return

        # 仅支持 UID（纯数字）方式
        if not re.fullmatch(r"\d+", target):
            yield event.plain_result(
                f"请输入 UP 主的 UID（纯数字）\n{_cmd_input('/bili 取消订阅 ', '点我重试')}"
            )
            return
        mid = target

        if not self.store.is_subscribed(umo, mid):
            yield event.plain_result(f"本会话未订阅该 UP 主（UID {mid}）。")
            return

        up = self.store.get_up(mid) or {}
        name = up.get("name") or f"UID{mid}"
        face = str(up.get("face") or "").strip()
        self.store.remove_sub(umo, mid)
        await self.store.save()

        # 取消成功卡片：取消成功 + 头像 + 名称/UID
        head = ""
        if face:
            head += f"![avatar #15px #15px]({format_pic_url(face, 15, 15)})"
        head += f"**{name}** {mid}"
        yield event.plain_result(f"取消成功\n{head}")

    @bili.command("订阅列表", alias={"列表", "list"})
    async def bili_list(self, event: AstrMessageEvent):
        """查看本会话订阅的所有 UP 主（名字 / UID / 取消按钮 各并排一排）"""
        mids = self.store.get_subs(event.unified_msg_origin)
        if not mids:
            yield event.plain_result(
                "本会话还没有订阅任何 UP 主。\n使用 /bili 订阅 <UID> 来添加。"
            )
            return
        names = []
        uids = []
        buttons = []
        for m in mids:
            names.append(self.store.up_name(m).replace("|", "丨"))
            uids.append(str(m))
            buttons.append(_cmd_input(f"/bili 取消订阅 {m}", "取消订阅"))
        lines = [
            f"本会话已订阅 {len(mids)} 个 UP 主：",
            "",
            "| " + " | ".join(names) + " |",
            "|" + "|".join([":---:"] * len(names)) + "|",
            "| " + " | ".join(uids) + " |",
            "| " + " | ".join(buttons) + " |",
        ]
        yield event.plain_result("\n".join(lines))

    @bili.command("测试", alias={"检查", "刷新", "check"})
    async def bili_check(self, event: AstrMessageEvent):
        """立即触发一次全局检查（手动触发一轮动态更新检查）"""
        if self._poll_lock.locked():
            yield event.plain_result("正在检查中，请稍后再试。")
            return
        now = time.time()
        if now - self._last_manual_check < _MANUAL_CHECK_COOLDOWN:
            wait = int(_MANUAL_CHECK_COOLDOWN - (now - self._last_manual_check)) + 1
            yield event.plain_result(f"操作太频繁，请 {wait} 秒后再试。")
            return
        self._last_manual_check = now
        asyncio.create_task(self._poll_once())
        yield event.plain_result("已触发一次检查，如有新动态会推送到对应会话。")

    @bili.command("状态", alias={"status"})
    async def bili_status(self, event: AstrMessageEvent):
        """查看插件运行状态"""
        mids = self.store.active_mids()
        last = format_ts(self._last_check_ts, "%Y-%m-%d %H:%M:%S") if self._last_check_ts else "尚未检查"
        lines = [
            "B站动态订阅状态",
            f"监控中 UP 主：{len(mids)} 个",
            f"订阅会话数：{self.store.session_count()} 个",
            f"上次检查时间：{last}",
            f"最近错误：{self._last_error or '无'}",
        ]
        yield event.plain_result("\n".join(lines))

    @bili.command("帮助", alias={"help"})
    @filter.custom_filter(_BareBiliRedirectFilter)
    async def bili_help(self, event: AstrMessageEvent):
        """显示插件帮助（指令均为可点击标签；也响应纯 /bili 调用）"""
        yield event.plain_result(_help_text())
