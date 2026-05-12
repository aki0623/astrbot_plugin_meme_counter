from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star


PLUGIN_ID = "astrbot_plugin_meme_counter"
STATS_KEY = "meme_counter_stats_v1"
ANIMATED_IMAGE_EXTS = {".gif", ".webp", ".apng", ".tgs"}


class MemeCounterPlugin(Star):
    """统计群聊中表情包发送次数。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.count_images = bool(config.get("count_images", True))
        self.count_faces = bool(config.get("count_faces", True))
        self.default_top_limit = int(config.get("default_top_limit", 10) or 10)
        self.ignore_bot_self = bool(config.get("ignore_bot_self", True))
        self._lock = asyncio.Lock()
        self._midnight_task: asyncio.Task | None = None

    async def initialize(self):
        self._midnight_task = asyncio.create_task(self._midnight_loop())

    async def terminate(self):
        if self._midnight_task:
            self._midnight_task.cancel()
            try:
                await self._midnight_task
            except asyncio.CancelledError:
                pass

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def collect_group_memes(self, event: AstrMessageEvent):
        """监听群聊消息，累计表情包次数。"""
        if self.ignore_bot_self and event.get_sender_id() == event.get_self_id():
            return

        memes = await self._extract_memes(event)
        if not memes:
            return

        group_key = self._get_group_key(event)
        user_id = event.get_sender_id() or "unknown"
        user_name = event.get_sender_name() or user_id

        async with self._lock:
            stats = await self._load_stats()
            group_stats = self._ensure_group_stats(stats, group_key)
            self._roll_group_if_new_day(group_stats)
            group_stats["date"] = self._today()
            group_stats["session"] = event.unified_msg_origin
            group_stats["bot_id"] = event.get_self_id() or "0"
            group_stats["total"] += len(memes)
            group_stats["updated_at"] = int(time.time())

            user_stats = group_stats["users"].setdefault(
                user_id,
                {"name": user_name, "count": 0},
            )
            user_stats["name"] = user_name
            user_stats["count"] += len(memes)

            for meme in memes:
                meme_stats = group_stats["memes"].setdefault(
                    meme["id"],
                    {
                        "kind": meme["kind"],
                        "label": meme["label"],
                        "source": meme["source"],
                        "count": 0,
                    },
                )
                if meme.get("source"):
                    meme_stats["source"] = meme["source"]
                meme_stats["count"] += 1

            await self.put_kv_data(STATS_KEY, stats)

    @filter.command("表情统计", alias={"表情包统计", "meme_stats"})
    async def show_stats(self, event: AstrMessageEvent, limit: int = 0):
        """查看本群表情包统计。"""
        if not self._is_group_event(event):
            yield event.plain_result("表情统计只支持在群聊中使用。")
            return

        limit = self._normalize_limit(limit)
        group_stats = await self._get_current_group_stats(event)
        if not group_stats or group_stats.get("total", 0) <= 0:
            yield event.plain_result("本群还没有统计到表情包。")
            return

        lines = [
            "本群表情包统计",
            f"总发送次数：{group_stats['total']}",
            f"参与人数：{len(group_stats['users'])}",
            f"不同表情：{len(group_stats['memes'])}",
            "",
            f"发送者 Top {limit}",
        ]
        lines.extend(self._format_user_ranking(group_stats, limit))
        lines.append("")
        lines.append(f"表情 Top {limit}")
        lines.extend(self._format_meme_ranking(group_stats, limit))

        yield event.plain_result("\n".join(lines))

    @filter.command("表情排行", alias={"meme_rank", "表情包排行"})
    async def show_user_rank(self, event: AstrMessageEvent, limit: int = 0):
        """查看本群表情包发送者排行。"""
        if not self._is_group_event(event):
            yield event.plain_result("表情排行只支持在群聊中使用。")
            return

        limit = self._normalize_limit(limit)
        group_stats = await self._get_current_group_stats(event)
        if not group_stats or group_stats.get("total", 0) <= 0:
            yield event.plain_result("本群还没有统计到表情包。")
            return

        lines = [f"本群表情包发送者 Top {limit}"]
        lines.extend(self._format_user_ranking(group_stats, limit))
        yield event.plain_result("\n".join(lines))

    @filter.command("表情包前五", alias={"表情前五", "meme_top5_forward"})
    async def show_top5_forward(self, event: AstrMessageEvent):
        """用合并转发消息输出本群前 5 表情包及次数。"""
        if not self._is_group_event(event):
            yield event.plain_result("表情包前五只支持在群聊中使用。")
            return

        group_stats = await self._get_current_group_stats(event)
        if not group_stats or group_stats.get("total", 0) <= 0:
            yield event.plain_result("本群还没有统计到表情包。")
            return

        top_memes = self._get_top_memes(group_stats, 5)
        if not top_memes:
            yield event.plain_result("本群还没有可展示的表情包排行。")
            return

        bot_id = event.get_self_id() or "0"
        nodes = []
        for idx, item in enumerate(top_memes, start=1):
            count = int(item.get("count", 0))
            label = item.get("label") or "未知表情"
            content = self._build_forward_node_content(idx, item, count, label)
            nodes.append(
                Comp.Node(
                    uin=bot_id,
                    name=f"Top {idx}",
                    content=content,
                ),
            )

        yield event.chain_result([Comp.Nodes(nodes)])

    @filter.command("我的表情", alias={"my_memes", "我的表情包"})
    async def show_my_stats(self, event: AstrMessageEvent):
        """查看自己在本群发送的表情包次数。"""
        if not self._is_group_event(event):
            yield event.plain_result("我的表情只支持在群聊中使用。")
            return

        group_stats = await self._get_current_group_stats(event)
        user_id = event.get_sender_id() or "unknown"
        user_stats = (group_stats or {}).get("users", {}).get(user_id)
        count = int((user_stats or {}).get("count", 0))
        total = int((group_stats or {}).get("total", 0))
        percent = (count / total * 100) if total else 0

        yield event.plain_result(
            f"{event.get_sender_name() or user_id} 在本群已发送表情包 {count} 次，"
            f"占本群统计总数 {percent:.1f}%。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置表情统计", alias={"reset_meme_stats", "重置表情包统计"})
    async def reset_stats(self, event: AstrMessageEvent):
        """管理员重置本群表情包统计。"""
        if not self._is_group_event(event):
            yield event.plain_result("重置表情统计只支持在群聊中使用。")
            return

        group_key = self._get_group_key(event)
        async with self._lock:
            stats = await self._load_stats()
            existed = group_key in stats
            stats.pop(group_key, None)
            await self.put_kv_data(STATS_KEY, stats)

        if existed:
            yield event.plain_result("已重置本群表情包统计。")
        else:
            yield event.plain_result("本群暂无可重置的表情包统计。")

    async def _extract_memes(self, event: AstrMessageEvent) -> list[dict[str, str]]:
        memes = []
        for component in event.get_messages():
            kind = self._component_kind(component)
            if kind == "image" and self.count_images:
                source = self._component_source(component)
                content_hash = await self._media_content_hash(component)
                memes.append(self._build_meme_record("image", source, content_hash))
            elif kind == "animation" and self.count_images:
                source = self._component_source(component)
                content_hash = await self._media_content_hash(component)
                memes.append(
                    self._build_meme_record("animation", source, content_hash)
                )
            elif kind == "face" and self.count_faces:
                source = self._face_source(component)
                memes.append(self._build_meme_record("face", source))
        return memes

    def _component_kind(self, component: Any) -> str:
        raw_type = getattr(component, "type", "")
        value = getattr(raw_type, "value", raw_type)
        name = str(value).lower()
        if name.endswith(".image") or name == "image":
            return "image"
        if name.endswith(".video") or name == "video":
            source = self._component_source(component)
            return "animation" if self._looks_like_animated_image(source) else ""
        if name.endswith(".file") or name == "file":
            source = self._component_source(component)
            return "animation" if self._looks_like_animated_image(source) else ""
        if name.endswith(".face") or name == "face":
            return "face"
        return ""

    def _component_source(self, component: Any) -> str:
        data = getattr(component, "data", {}) or {}
        for attr in ("url", "file", "file_", "path", "name"):
            value = getattr(component, attr, None) or data.get(attr)
            if value:
                return str(value)
        return repr(component)

    def _face_source(self, component: Any) -> str:
        data = getattr(component, "data", {}) or {}
        value = getattr(component, "id", None) or data.get("id") or data.get("face_id")
        if value is None:
            return repr(component)
        return str(value)

    async def _media_content_hash(self, component: Any) -> str | None:
        try:
            convert = getattr(component, "convert_to_base64", None)
            if callable(convert):
                bs64_data = await convert()
                if bs64_data:
                    raw = base64.b64decode(bs64_data.removeprefix("base64://"))
                    return hashlib.sha256(raw).hexdigest()
        except Exception as exc:
            logger.debug(f"媒体内容哈希计算失败，回退到来源哈希: {exc}")

        try:
            get_file = getattr(component, "get_file", None)
            if callable(get_file):
                path = await get_file()
                digest = self._hash_local_file(path)
                if digest:
                    return digest
        except Exception as exc:
            logger.debug(f"文件内容哈希计算失败，回退到来源哈希: {exc}")

        try:
            convert_file = getattr(component, "convert_to_file_path", None)
            if callable(convert_file):
                path = await convert_file()
                digest = self._hash_local_file(path)
                if digest:
                    return digest
        except Exception as exc:
            logger.debug(f"媒体文件哈希计算失败，回退到来源哈希: {exc}")

        source = self._component_source(component)
        try:
            if source.startswith("base64://"):
                raw = base64.b64decode(source.removeprefix("base64://"))
                return hashlib.sha256(raw).hexdigest()
            if source.startswith("file:///"):
                path = source[8:]
                return self._hash_local_file(path)
            return self._hash_local_file(source)
        except Exception as exc:
            logger.debug(f"本地媒体哈希计算失败，回退到来源哈希: {exc}")

        return None

    def _hash_local_file(self, path: str | None) -> str | None:
        if not path:
            return None
        if path.startswith("file:///"):
            path = path[8:]
        if not os.path.exists(path):
            return None
        hasher = hashlib.sha256()
        with open(path, "rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _looks_like_animated_image(self, source: str) -> bool:
        source_lower = str(source or "").split("?", 1)[0].lower()
        _, ext = os.path.splitext(source_lower)
        return ext in ANIMATED_IMAGE_EXTS

    def _build_meme_record(
        self,
        kind: str,
        source: str,
        content_hash: str | None = None,
    ) -> dict[str, str]:
        digest = content_hash or hashlib.sha256(
            f"{kind}:{source}".encode("utf-8")
        ).hexdigest()
        if kind == "face":
            label = f"平台表情 {source}"
        elif kind == "animation" and content_hash:
            label = f"动图表情 {digest[:8]}"
        elif kind == "animation":
            label = f"动图表情 {digest[:8]}(来源)"
        elif content_hash:
            label = f"图片表情 {digest[:8]}"
        else:
            label = f"图片表情 {digest[:8]}(来源)"
        return {"id": digest, "kind": kind, "label": label, "source": source}

    def _build_forward_node_content(
        self,
        idx: int,
        item: dict[str, Any],
        count: int,
        label: str,
    ) -> list[Any]:
        source = str(item.get("source") or "").strip()
        kind = str(item.get("kind") or "")

        image = (
            self._image_component_from_source(source)
            if kind in {"image", "animation"}
            else None
        )
        if image is not None:
            return [image, Comp.Plain(f"次数：{count}")]

        return [Comp.Plain(f"表情包：{label}\n次数：{count}")]

    def _image_component_from_source(self, source: str):
        if not source:
            return None
        try:
            if source.startswith(("http://", "https://")):
                return Comp.Image.fromURL(source)
            if source.startswith("base64://"):
                return Comp.Image(source)
            if source.startswith("file:///"):
                return Comp.Image(source)
            return Comp.Image.fromFileSystem(source)
        except Exception:
            return None

    async def _load_stats(self) -> dict[str, Any]:
        stats = await self.get_kv_data(STATS_KEY, {})
        if not isinstance(stats, dict):
            logger.warning("表情包统计数据格式异常，已重置为空数据。")
            return {}
        return stats

    async def _get_current_group_stats(
        self,
        event: AstrMessageEvent,
    ) -> dict[str, Any] | None:
        stats = await self._load_stats()
        group_key = self._get_group_key(event)
        group_stats = stats.get(group_key)
        if not group_stats:
            return None
        if group_stats.get("date") != self._today():
            session = str(group_stats.get("session") or event.unified_msg_origin)
            bot_id = str(group_stats.get("bot_id") or event.get_self_id() or "0")
            group_stats.clear()
            group_stats.update(self._new_group_stats())
            group_stats["session"] = session
            group_stats["bot_id"] = bot_id
            await self.put_kv_data(STATS_KEY, stats)
        return group_stats

    def _ensure_group_stats(
        self,
        stats: dict[str, Any],
        group_key: str,
    ) -> dict[str, Any]:
        return stats.setdefault(
            group_key,
            self._new_group_stats(),
        )

    def _new_group_stats(self, date: str | None = None) -> dict[str, Any]:
        return {
            "date": date or self._today(),
            "session": "",
            "bot_id": "0",
            "total": 0,
            "users": {},
            "memes": {},
            "updated_at": int(time.time()),
        }

    def _roll_group_if_new_day(self, group_stats: dict[str, Any]) -> None:
        if group_stats.get("date") == self._today():
            return
        group_stats.clear()
        group_stats.update(self._new_group_stats())

    async def _midnight_loop(self) -> None:
        while True:
            await asyncio.sleep(self._seconds_until_next_midnight())
            try:
                await self._settle_finished_day()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"表情包每日结算失败: {exc}", exc_info=True)

    async def _settle_finished_day(self) -> None:
        today = self._today()
        messages: list[tuple[str, MessageChain]] = []

        async with self._lock:
            stats = await self._load_stats()
            for group_stats in stats.values():
                stat_date = str(group_stats.get("date") or today)
                session = str(group_stats.get("session") or "").strip()
                total = int(group_stats.get("total", 0) or 0)
                if stat_date >= today or total <= 0 or not session:
                    self._roll_group_if_new_day(group_stats)
                    continue

                messages.append(
                    (
                        session,
                        self._build_daily_summary_chain(
                            group_stats,
                            stat_date,
                            str(group_stats.get("bot_id") or "0"),
                        ),
                    )
                )
                session_to_keep = session
                bot_id_to_keep = str(group_stats.get("bot_id") or "0")
                group_stats.clear()
                group_stats.update(self._new_group_stats(today))
                group_stats["session"] = session_to_keep
                group_stats["bot_id"] = bot_id_to_keep

            await self.put_kv_data(STATS_KEY, stats)

        for session, chain in messages:
            try:
                await self.context.send_message(session, chain)
            except Exception as exc:
                logger.error(f"发送表情包每日统计失败: session={session}, err={exc}")

    def _build_daily_summary_chain(
        self,
        group_stats: dict[str, Any],
        stat_date: str,
        bot_id: str,
    ) -> MessageChain:
        nodes = [
            Comp.Node(
                uin=bot_id,
                name="每日表情包统计",
                content=[
                    Comp.Plain(
                        f"{stat_date} 表情包统计\n"
                        f"总发送次数：{group_stats.get('total', 0)}\n"
                        f"参与人数：{len(group_stats.get('users', {}))}\n"
                        f"不同表情：{len(group_stats.get('memes', {}))}"
                    )
                ],
            )
        ]
        nodes.extend(self._build_top_meme_nodes(group_stats, bot_id, 5))
        return MessageChain([Comp.Nodes(nodes)])

    def _build_top_meme_nodes(
        self,
        group_stats: dict[str, Any],
        bot_id: str,
        limit: int,
    ) -> list[Any]:
        nodes = []
        for idx, item in enumerate(self._get_top_memes(group_stats, limit), start=1):
            count = int(item.get("count", 0))
            label = item.get("label") or "未知表情"
            nodes.append(
                Comp.Node(
                    uin=bot_id,
                    name=f"Top {idx}",
                    content=self._build_forward_node_content(idx, item, count, label),
                )
            )
        return nodes

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _seconds_until_next_midnight(self) -> float:
        now = datetime.now()
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return max(1.0, (next_midnight - now).total_seconds())

    def _get_group_key(self, event: AstrMessageEvent) -> str:
        group_id = str(event.get_group_id() or "").strip()
        if group_id:
            return f"{event.get_platform_id()}:{group_id}"
        return event.unified_msg_origin

    def _is_group_event(self, event: AstrMessageEvent) -> bool:
        return bool(str(event.get_group_id() or "").strip()) or ":group" in str(
            event.unified_msg_origin
        ).lower()

    def _normalize_limit(self, limit: int) -> int:
        if limit <= 0:
            limit = self.default_top_limit
        return max(1, min(int(limit), 30))

    def _format_user_ranking(
        self,
        group_stats: dict[str, Any],
        limit: int,
    ) -> list[str]:
        users = sorted(
            group_stats.get("users", {}).items(),
            key=lambda item: int(item[1].get("count", 0)),
            reverse=True,
        )
        if not users:
            return ["暂无发送者数据"]

        lines = []
        for idx, (user_id, item) in enumerate(users[:limit], start=1):
            name = item.get("name") or user_id
            count = int(item.get("count", 0))
            lines.append(f"{idx}. {name}：{count} 次")
        return lines

    def _format_meme_ranking(
        self,
        group_stats: dict[str, Any],
        limit: int,
    ) -> list[str]:
        memes = self._get_top_memes(group_stats, limit)
        if not memes:
            return ["暂无表情数据"]

        lines = []
        for idx, item in enumerate(memes, start=1):
            label = item.get("label") or "未知表情"
            count = int(item.get("count", 0))
            lines.append(f"{idx}. {label}：{count} 次")
        return lines

    def _get_top_memes(
        self,
        group_stats: dict[str, Any],
        limit: int,
    ) -> list[dict[str, Any]]:
        memes = sorted(
            group_stats.get("memes", {}).values(),
            key=lambda item: int(item.get("count", 0)),
            reverse=True,
        )
        return memes[:limit]
