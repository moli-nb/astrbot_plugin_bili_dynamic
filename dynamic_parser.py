"""B 站动态原始数据处理。

将 B 站接口返回的原始 JSON 条目解析为插件内部统一使用的标准结构，
供渲染与存储逻辑使用。支持两类数据源：

1. 完整动态接口 ``polymer/web-dynamic/v1/feed/space``：字段完整，包含作者、
   发布时间、置顶标记、图片宽高以及各种动态类型（图文 / 视频 / 转发 /
   专栏 / 文字等）；
2. 降级图文接口 ``polymer/web-dynamic/v1/opus/feed/space``：匿名可用但字段
   较少，只覆盖图文动态，且不带作者、发布时间与置顶信息。

标准结构（dict）字段说明::

    {
        "id": int,                  # 动态 id（整数，用于比较先后顺序）
        "id_str": str,              # 动态 id 原始字符串
        "kind": str,                # draw/word/video/forward/article/music/live/other
        "pinned": bool,             # 是否为置顶动态
        "vote_join": bool,          # 是否为「参与投票」类动态（转发型，可被全局开关屏蔽）
        "author": {"name", "face"}, # 作者昵称与头像
        "pub_ts": int,              # 发布时间戳（秒），未知时为 0
        "text": str,                # 正文文字
        "title": str,               # 标题（图文 / 专栏可能有）
        "pics": [{"url", "width", "height"}],  # 图片列表
        "video": {...} | None,      # 视频信息（kind=video 时）
        "forward": {...} | None,    # 被转发的原动态（kind=forward 时，递归结构）
        "article": {...} | None,    # 专栏信息（kind=article 时）
        "music": {...} | None,      # 音乐信息（kind=music 时）
        "additional": {...} | None, # 附加卡片（投票 / 关联视频等，见下）
        "stat": {"like", "comment", "forward"},  # 互动数据（已格式化为展示字符串）
        "jump_url": str,            # 跳转链接
        "source": "full" | "opus",  # 数据来源
    }

``additional`` 附加卡片字段（一条动态至多一种）::

    - 投票：{"kind": "vote", "vote_id", "title", "join_num", "end_ts",
             "options": [{"idx", "desc", "count"}]}（options 由接口补拉，可能为空）
    - 关联视频：{"kind": "video", "title", "cover", "duration", "play",
                 "danmaku", "url"}
"""

from __future__ import annotations


def normalize_url(url) -> str:
    """补全 B 站返回的相对协议 URL，并统一为 https。"""
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url


def _text_of(node) -> str:
    """从 ``{"text": "..."}`` 结构中取出文字。"""
    if isinstance(node, dict):
        return str(node.get("text") or "")
    return ""


def _rich_text_of(node, exclude_types: tuple[str, ...] = ()) -> str:
    """从含 rich_text_nodes 的结构中重建文本（可排除特定类型的节点）。

    投票等内联节点会被排除，避免与下方附加卡片重复展示；没有节点信息时
    退回直接使用 text 字段。
    """
    if not isinstance(node, dict):
        return ""
    nodes = node.get("rich_text_nodes")
    if not isinstance(nodes, list) or not nodes:
        return str(node.get("text") or "")
    parts = []
    for item in nodes:
        if not isinstance(item, dict):
            continue
        if item.get("type") in exclude_types:
            continue
        parts.append(str(item.get("text") or ""))
    return "".join(parts)


def _stat_count(stat: dict, key: str):
    """从 module_stat 中取出某类互动数量（int 或 None）。"""
    node = stat.get(key)
    if isinstance(node, dict):
        return node.get("count")
    return None


def fmt_count(value) -> str:
    """把互动数字格式化为展示字符串。

    - None / 空串 -> ""
    - 字符串原样返回（B 站部分接口已返回 "2.3万" 之类的格式化结果）
    - 整数 -> 12345 显示为 "1.2万"
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    try:
        v = int(value)
    except (TypeError, ValueError):
        return str(value)
    if v >= 100000000:
        text = f"{v / 100000000:.1f}亿"
    elif v >= 10000:
        text = f"{v / 10000:.1f}万"
    else:
        return str(v)
    return text.replace(".0万", "万").replace(".0亿", "亿")


def _parse_pics(nodes, url_key: str = "url") -> list[dict]:
    """解析图片数组（兼容 opus.pics 与 draw.items 两种结构）。"""
    out = []
    for pic in nodes or []:
        if not isinstance(pic, dict):
            continue
        url = normalize_url(pic.get(url_key) or pic.get("url") or pic.get("src"))
        if not url:
            continue
        out.append(
            {
                "url": url,
                "width": int(pic.get("width") or 0),
                "height": int(pic.get("height") or 0),
            }
        )
    return out


def parse_full_item(item: dict) -> dict | None:
    """解析完整动态接口中的单条动态；无法解析时返回 None。"""
    if not isinstance(item, dict):
        return None

    id_str = str(item.get("id_str") or "")
    if not id_str.isdigit():
        return None

    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    dynamic = modules.get("module_dynamic") or {}
    major = dynamic.get("major") or {}
    stat = modules.get("module_stat") or {}
    tag_text = str((modules.get("module_tag") or {}).get("text") or "")

    dyn_type = str(item.get("type") or "")
    major_type = str(major.get("type") or "")

    name = str(author.get("name") or "")
    face = normalize_url(author.get("face"))

    result = {
        "id": int(id_str),
        "id_str": id_str,
        "kind": "other",
        "pinned": ("置顶" in tag_text) or ("置頂" in tag_text),
        "vote_join": False,  # 是否为「参与投票」类动态（见转发分支）
        "author": {"name": name, "face": face},
        "pub_ts": int(author.get("pub_ts") or 0),
        "text": "",
        "title": "",
        "pics": [],
        "video": None,
        "forward": None,
        "article": None,
        "music": None,
        "additional": None,
        "stat": {
            "like": fmt_count(_stat_count(stat, "like")),
            "comment": fmt_count(_stat_count(stat, "comment")),
            "forward": fmt_count(_stat_count(stat, "forward")),
        },
        "jump_url": f"https://www.bilibili.com/opus/{id_str}",
        "source": "full",
    }

    # 附加卡片（投票 / 关联视频等，一条动态至多一种）
    additional = dynamic.get("additional") or {}
    add_type = str(additional.get("type") or "")
    if add_type == "ADDITIONAL_TYPE_VOTE":
        vote = additional.get("vote") or {}
        result["additional"] = {
            "kind": "vote",
            "vote_id": str(vote.get("vote_id") or ""),
            "title": str(vote.get("desc") or "").strip(),
            "join_num": fmt_count(vote.get("join_num")),
            "end_ts": int(vote.get("end_time") or 0),
            "options": [],  # 选项列表由 bili_api.enrich_dynamic 补拉后填入
        }
    elif add_type == "ADDITIONAL_TYPE_UGC":
        ugc = additional.get("ugc") or {}
        ugc_stat = ugc.get("stat") or {}
        result["additional"] = {
            "kind": "video",
            "title": str(ugc.get("title") or "").strip(),
            "cover": normalize_url(ugc.get("cover")),
            "duration": str(ugc.get("duration") or ""),
            "play": fmt_count(ugc_stat.get("play")),
            "danmaku": fmt_count(ugc_stat.get("danmaku")),
            "url": normalize_url(ugc.get("jump_url")),
        }

    desc_text = _text_of(dynamic.get("desc"))

    if dyn_type == "DYNAMIC_TYPE_FORWARD" or isinstance(item.get("orig"), dict):
        # 转发动态：正文是转发时写的评论，被转发的原动态在 orig 字段中
        result["kind"] = "forward"
        result["text"] = desc_text
        # 识别「参与投票」动态：用户参与投票后 B 站自动生成的转发式动态，
        # 正文固定为“我参与了投票”+ 内联投票卡片 + “我投给了xx”
        desc_nodes = (dynamic.get("desc") or {}).get("rich_text_nodes") or []
        result["vote_join"] = "我参与了投票" in desc_text and any(
            isinstance(n, dict) and n.get("type") == "RICH_TEXT_NODE_TYPE_VOTE"
            for n in desc_nodes
        )
        parsed_orig = parse_full_item(item.get("orig") or {})
        if parsed_orig:
            result["forward"] = parsed_orig

    elif major_type == "MAJOR_TYPE_ARCHIVE" or dyn_type == "DYNAMIC_TYPE_AV":
        # 视频投稿动态
        archive = major.get("archive") or {}
        bvid = str(archive.get("bvid") or "")
        archive_stat = archive.get("stat") or {}
        video_url = normalize_url(archive.get("jump_url")) or (
            f"https://www.bilibili.com/video/{bvid}" if bvid else result["jump_url"]
        )
        result["kind"] = "video"
        result["text"] = desc_text
        result["jump_url"] = video_url
        result["video"] = {
            "bvid": bvid,
            "title": str(archive.get("title") or "").strip(),
            "cover": normalize_url(archive.get("cover")),
            "duration": str(archive.get("duration_text") or ""),
            "play": fmt_count(archive_stat.get("play")),
            "danmaku": fmt_count(archive_stat.get("danmaku")),
            "url": video_url,
        }

    elif major_type == "MAJOR_TYPE_OPUS" or dyn_type == "DYNAMIC_TYPE_DRAW":
        # 图文动态（新结构 major.opus；旧结构 major.draw 也作兼容）
        result["kind"] = "draw"
        opus = major.get("opus") or {}
        if opus:
            if not result["text"]:
                # 过滤内联的投票节点，避免与下方的投票卡片重复展示
                result["text"] = _rich_text_of(
                    opus.get("summary"), exclude_types=("RICH_TEXT_NODE_TYPE_VOTE",)
                )
            result["title"] = str(opus.get("title") or "").strip()
            result["pics"] = _parse_pics(opus.get("pics"))
            result["jump_url"] = normalize_url(opus.get("jump_url")) or result["jump_url"]
        # 旧结构兼容：正文与图片也可能位于 desc 与 major.draw
        if not result["text"]:
            result["text"] = desc_text
        if not result["pics"]:
            draw = major.get("draw") or {}
            result["pics"] = _parse_pics(draw.get("items"), url_key="src")

    elif major_type == "MAJOR_TYPE_ARTICLE" or dyn_type == "DYNAMIC_TYPE_ARTICLE":
        # 专栏文章动态
        article = major.get("article") or {}
        article_url = normalize_url(article.get("jump_url")) or result["jump_url"]
        result["kind"] = "article"
        result["text"] = desc_text
        result["jump_url"] = article_url
        result["article"] = {
            "title": str(article.get("title") or "").strip(),
            "desc": str(article.get("desc") or "").strip(),
            "url": article_url,
        }

    elif major_type == "MAJOR_TYPE_MUSIC" or dyn_type == "DYNAMIC_TYPE_MUSIC":
        # 音频 / 音乐动态
        music = major.get("music") or {}
        result["kind"] = "music"
        result["text"] = desc_text
        result["music"] = {
            "title": str(music.get("title") or "").strip(),
        }

    else:
        # 纯文字动态及未知类型统一兜底
        if dyn_type == "DYNAMIC_TYPE_WORD" or not major_type or major_type == "MAJOR_TYPE_NONE":
            result["kind"] = "word"
        elif dyn_type in ("DYNAMIC_TYPE_LIVE", "DYNAMIC_TYPE_LIVE_RCMD"):
            result["kind"] = "live"
        else:
            result["kind"] = "other"
        result["text"] = desc_text or _text_of(major.get("desc"))

    return result


def parse_opus_item(item: dict) -> dict | None:
    """解析降级图文接口中的单条动态（字段较少）。"""
    if not isinstance(item, dict):
        return None

    opus_id = str(item.get("opus_id") or "")
    if not opus_id.isdigit():
        return None

    cover = item.get("cover") or {}
    pics = _parse_pics([cover]) if isinstance(cover, dict) else []

    stat = item.get("stat") or {}

    return {
        "id": int(opus_id),
        "id_str": opus_id,
        "kind": "draw",
        "pinned": False,
        "vote_join": False,
        "author": {"name": "", "face": ""},
        "pub_ts": 0,
        "text": str(item.get("content") or ""),
        "title": "",
        "pics": pics,
        "video": None,
        "forward": None,
        "article": None,
        "music": None,
        "additional": None,
        "stat": {
            "like": fmt_count(stat.get("like")),
            "comment": "",
            "forward": "",
        },
        "jump_url": normalize_url(item.get("jump_url"))
        or f"https://www.bilibili.com/opus/{opus_id}",
        "source": "opus",
    }
