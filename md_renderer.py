"""把标准动态数据渲染为 QQ 官方机器人可用的 Markdown 文本。

渲染遵循 QQ 机器人官方 Markdown 语法：

- 标题 ``#``、加粗 ``**``、块引用 ``>``、链接 ``[text](url)``；
- 图片语法为 ``![alt #宽px #高px](url)``，其中宽高为显示尺寸，
  图片 URL 必须是公网可访问地址（QQ 服务端会下载转存）；
- 多张图片使用表格排版为一行横向排列。

注意：QQ 官方文档未明确列出表格与代码块语法，但实测渲染良好；
如后续发现所运行的 QQ 版本不支持，可在此模块中调整模板。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# 北京时间（B 站服务器时间）
_TZ = timezone(timedelta(hours=8))

# 正文最大展示长度，超出部分截断，避免消息过长
_MAX_TEXT_LEN = 500
# 转发摘要最大展示长度
_MAX_FORWARD_LEN = 200

# 图片显示尺寸（像素）
_IMG_H_MULTI = 80       # 多图模式每张图片高度
_IMG_H_SINGLE = 120     # 单图模式图片高度（也是单图的高度上限）
_IMG_H_MULTI_MAX = 400  # 多图模式窄长竖图的高度上限

# 动态类型 -> 展示名称
_KIND_NAMES = {
    "draw": "图文动态",
    "word": "文字动态",
    "video": "视频投稿",
    "forward": "转发动态",
    "article": "专栏文章",
    "music": "音乐",
    "live": "直播",
    "other": "动态",
}

# 推送标题（按动态类型区分，未列出的类型使用默认标题）
_KIND_TITLES = {
    "video": "有新视频！",
}
_DEFAULT_TITLE = "有新动态！"

# 正文代码块的语言标识（按动态类型区分，未列出的类型使用默认标识）
_KIND_CODE_LABELS = {
    "video": "视频简介",
}
_DEFAULT_CODE_LABEL = "动态内容"


def render_push_markdown(dynamic: dict, up_info: dict | None = None) -> str:
    """渲染一条动态的推送 Markdown 文本。

    Args:
        dynamic: dynamic_parser 解析出的标准结构。
        up_info: 可选，``{"name": str, "face": str}``；降级模式（opus 接口）
            动态自身不带作者信息时，用订阅时缓存的 UP 信息补齐。

    Returns:
        完整的 Markdown 文本（末尾带一个换行符）。
    """
    kind = dynamic.get("kind") or "other"
    title = _KIND_TITLES.get(kind, _DEFAULT_TITLE)
    lines: list[str] = [f"# {title}", ""]

    author = dynamic.get("author") or {}
    fallback = up_info or {}
    name = (author.get("name") or fallback.get("name") or "").strip()
    face = (author.get("face") or fallback.get("face") or "").strip()

    # 第一行：头像 + UP 昵称
    head = ""
    if face:
        head += f"![avatar #15px #15px]({format_pic_url(face, 15, 15)})"
    if name:
        head += f"**{name}**"
    if head:
        lines.append(head)

    # 第二行：发布时间与动态类型
    kind_cn = _KIND_NAMES.get(kind, "动态")
    pin_suffix = "（置顶）" if dynamic.get("pinned") else ""
    time_text = format_ts(dynamic.get("pub_ts") or 0)
    if kind == "forward":
        action = "转发了动态"
    else:
        action = f"发布了{kind_cn}"
    if time_text:
        lines.append(f"于 {time_text}")
        lines.append(f"{action}{pin_suffix}")
    else:
        lines.append(f"{action}{pin_suffix}")
    lines.append("")

    # 正文内容
    body = _render_body(dynamic)
    if body:
        lines.extend(body)
        lines.append("")  # 正文块后保留空行，避免表格/引用块吞并后续行

    # 附加卡片（投票 / 关联视频），前后用分隔线包裹
    extra = _render_additional(dynamic.get("additional"))
    if extra:
        lines.append("---")
        lines.append("")
        lines.extend(extra)
        lines.append("")
        lines.append("---")
        lines.append("")

    # 互动数据行
    stat_line = _render_stat(dynamic)
    if stat_line:
        lines.append(stat_line)

    # 跳转链接
    url = (dynamic.get("jump_url") or "").strip()
    if url:
        label = {"video": "查看视频", "article": "查看专栏"}.get(kind, "查看动态")
        link_line = f"[{label}]({url})"
        if kind == "video":
            # 追加网页版播放器直链（点击直接播放）
            bvid = str((dynamic.get("video") or {}).get("bvid") or "").strip()
            if bvid:
                link_line += (
                    " [直接播放](https://www.bilibili.com/blackboard/"
                    f"html5mobileplayer.html?bvid={bvid}&page=1&autoplay=1)"
                )
        lines.append(link_line)

    text = "\n".join(lines).strip("\n")
    return text + "\n"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def format_ts(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """时间戳（秒）-> 格式化时间字符串，无效时返回空串。"""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), _TZ).strftime(fmt)
    except (OverflowError, OSError, ValueError):
        return ""


def _truncate(text: str, max_len: int = _MAX_TEXT_LEN) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "……"


def _fence_for(text: str) -> str:
    """为正文选择不会与内容冲突的代码块围栏。"""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _code_block(
    text: str,
    label: str = _DEFAULT_CODE_LABEL,
    max_len: int | None = _MAX_TEXT_LEN,
) -> list[str]:
    """正文用代码块包裹：保留原始换行，同时避免内容里的符号破坏排版。

    代码块开头围栏带语言标识（如「动态内容」「视频简介」）。
    ``max_len`` 为 None 时不做截断（视频简介完整显示）。
    """
    text = (text or "").strip()
    if max_len and len(text) > max_len:
        text = text[:max_len] + "……"
    if not text:
        return []
    fence = _fence_for(text)
    return [f"{fence}{label}", text, fence]


def _render_body(dynamic: dict) -> list[str]:
    """根据动态类型生成正文部分（标题 / 文字 / 图片 / 视频等）。"""
    kind = dynamic.get("kind") or "other"
    code_label = _KIND_CODE_LABELS.get(kind, _DEFAULT_CODE_LABEL)
    out: list[str] = []

    text = (dynamic.get("text") or "").strip()

    if kind == "video":
        video = dynamic.get("video") or {}
        title = (video.get("title") or "").strip()
        if title:
            out.extend(_code_block(title, "标题", max_len=None))
            out.append("")
        cover = (video.get("cover") or "").strip()
        if cover:
            out.extend([f"![cover #280px #158px]({format_pic_url(cover, 280, 158)})", ""])
        if text:
            # 视频简介完整显示（不截断）
            out.extend(_code_block(text, code_label, max_len=None))
            out.append("")
        play_parts = []
        if video.get("play"):
            play_parts.append(f"播放 {video['play']}")
        if video.get("danmaku"):
            play_parts.append(f"弹幕 {video['danmaku']}")
        if video.get("duration"):
            play_parts.append(f"时长 {video['duration']}")
        if play_parts:
            out.append(" · ".join(play_parts))

    elif kind == "forward":
        if text:
            out.extend(_code_block(text, code_label))
            out.append("")
        fwd = dynamic.get("forward") or {}
        if fwd:
            summary = _forward_summary(fwd)
            if summary:
                fwd_name = (fwd.get("author") or {}).get("name") or "未知"
                quoted = [f"> **转发自 @{fwd_name}**："]
                for line in summary.splitlines():
                    quoted.append(f"> {line}" if line.strip() else ">")
                out.extend(quoted)

    elif kind == "draw":
        title = (dynamic.get("title") or "").strip()
        if title:
            out.extend(_code_block(title, "标题", max_len=None))
            out.append("")
        if text:
            out.extend(_code_block(text, code_label))
            out.append("")
        out.extend(_render_pics(dynamic.get("pics") or []))

    elif kind == "article":
        article = dynamic.get("article") or {}
        title = (article.get("title") or "").strip()
        if title:
            out.extend(_code_block(title, "标题", max_len=None))
            out.append("")
        desc = (article.get("desc") or text or "").strip()
        if desc:
            out.extend(_code_block(desc, code_label))

    else:  # word / music / live / other
        if text:
            out.extend(_code_block(text, code_label))

    while out and not out[-1].strip():
        out.pop()
    return out


def _render_stat(dynamic: dict) -> str:
    """互动数据行：``点赞 x · 评论 y · 转发 z``（缺失项自动省略）。"""
    stat = dynamic.get("stat") or {}
    parts = []
    for key, label in (("like", "点赞"), ("comment", "评论"), ("forward", "转发")):
        value = stat.get(key)
        if value:
            parts.append(f"{label} {value}")
    return " · ".join(parts)


def _render_additional(additional) -> list[str]:
    """渲染动态的附加卡片（投票 / 关联视频等）。"""
    if not isinstance(additional, dict):
        return []
    kind = additional.get("kind")
    if kind == "vote":
        return _render_vote_block(additional)
    if kind == "video":
        return _render_ugc_block(additional)
    return []


def _render_vote_block(vote: dict) -> list[str]:
    """投票卡片：标题 + 选项（复选样式，含票数）+ 参与人数。"""
    title = (vote.get("title") or "").strip()
    out = [f"**投票**：{title}" if title else "**投票**"]
    for opt in vote.get("options") or []:
        if not isinstance(opt, dict):
            continue
        desc = str(opt.get("desc") or "").strip()
        if not desc:
            continue
        count = str(opt.get("count") or "").strip()
        suffix = f"（{count} 票）" if count else ""
        out.append(f"- [ ] {desc}{suffix}")
    join_num = str(vote.get("join_num") or "").strip()
    if join_num:
        out.append(f"已有 {join_num} 人参与")
    return out


def _render_ugc_block(ugc: dict) -> list[str]:
    """关联视频卡片：标题 + 封面 + 播放数据 + 链接。"""
    out: list[str] = []
    title = (ugc.get("title") or "").strip()
    if title:
        out.extend([f"**附视频**：{title}", ""])
    cover = (ugc.get("cover") or "").strip()
    if cover:
        out.extend([f"![video #200px #112px]({format_pic_url(cover, 200, 112)})", ""])
    parts = []
    if ugc.get("play"):
        parts.append(f"播放 {ugc['play']}")
    if ugc.get("danmaku"):
        parts.append(f"弹幕 {ugc['danmaku']}")
    if ugc.get("duration"):
        parts.append(f"时长 {ugc['duration']}")
    if parts:
        out.append(" · ".join(parts))
    url = (ugc.get("url") or "").strip()
    if url:
        out.append(f"[查看视频]({url})")
    while out and not out[-1].strip():
        out.pop()
    return out


def format_pic_url(url: str, disp_w: int | None = None, disp_h: int | None = None) -> str:
    """返回图片原始 URL（不追加 CDN 缩略参数，保证清晰度）。

    说明：此前会为 B 站图床 URL 追加 ``@宽w_高h_1c.webp`` 缩略参数以减小
    体积，但缩略后显示偏糊，现改为直接使用原图链接。
    ``disp_w`` / ``disp_h`` 参数保留以兼容调用方，当前不参与处理。
    """
    return url


def _calc_display_size(width: int, height: int, target_h: int, single: bool) -> tuple[int, int]:
    """按原始宽高比计算图片显示尺寸，避免极端长图 / 全景图被压扁变形。

    - 常规图片：高度取目标高度，宽度按比例换算；
    - 极窄长图（如条漫 / 招聘长图）：宽度取下限，高度按比例放宽（限制最大值）；
    - 超宽图（全景图）：宽度取上限，高度按比例缩小（限制最小值）。
    """
    max_w = 320 if single else 200
    min_w = 36
    aspect = width / height  # 宽高比

    disp_w = round(target_h * aspect)
    if disp_w < min_w:
        # 太窄的竖长图：固定宽度，高度按比例放宽（限制最大值）
        # 单图上限 120px；多图（表格）保持原上限 400px
        limit = _IMG_H_SINGLE if single else _IMG_H_MULTI_MAX
        return min_w, max(target_h, min(round(min_w / aspect), limit))
    if disp_w > max_w:
        # 超宽横图：固定宽度，高度按比例（下限 36px）
        return max_w, max(36, min(round(max_w / aspect), target_h))
    return disp_w, target_h


def _is_animated_pic(url: str) -> bool:
    """判断图片 URL 是否为动图（GIF）。"""
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith(".gif") or ".gif@" in path


def _render_pics(pics: list[dict]) -> list[str]:
    """渲染图片：单图直接一行；多图排成一行表格。"""
    if not pics:
        return []

    single = len(pics) == 1
    target_h = _IMG_H_SINGLE if single else _IMG_H_MULTI

    cells = []
    for idx, pic in enumerate(pics, start=1):
        url = pic.get("url") or ""
        if not url:
            continue
        if not single and _is_animated_pic(url):
            # 动图（GIF）在表格中不渲染缩略图，改为跳转链接
            cells.append(f"[点我查看动图]({url})")
            continue
        width = int(pic.get("width") or 0)
        height = int(pic.get("height") or 0)
        if width > 0 and height > 0:
            disp_w, disp_h = _calc_display_size(width, height, target_h, single)
        else:
            disp_w, disp_h = target_h, target_h
        cells.append(
            f"![{idx} #{disp_w}px #{disp_h}px]({format_pic_url(url, disp_w, disp_h)})"
        )

    if not cells:
        return []

    if single:
        # 单图不装表格，直接一行
        return [cells[0]]

    n = len(cells)
    header = "| " + " | ".join(f"图{i}" for i in range(1, n + 1)) + " |"
    sep = "|" + "|".join([":---:"] * n) + "|"
    row = "| " + " | ".join(cells) + " |"
    return [header, sep, row]


def _forward_summary(fwd: dict) -> str:
    """生成被转发动态的摘要文字。"""
    kind = fwd.get("kind")
    text = (fwd.get("text") or "").strip()

    if kind == "video":
        video = fwd.get("video") or {}
        title = (video.get("title") or "").strip()
        parts = []
        if title:
            parts.append(f"视频：{title}")
        if text:
            parts.append(text)
        return _truncate("\n".join(parts), _MAX_FORWARD_LEN)

    if kind == "article":
        article = fwd.get("article") or {}
        title = (article.get("title") or "").strip()
        if title:
            return _truncate(f"专栏：{title}", _MAX_FORWARD_LEN)

    if kind == "draw":
        lines = []
        if text:
            lines.append(text)
        n = len(fwd.get("pics") or [])
        if n:
            lines.append(f"[图片 x{n}]")
        return _truncate("\n".join(lines), _MAX_FORWARD_LEN)

    return _truncate(text, _MAX_FORWARD_LEN)
