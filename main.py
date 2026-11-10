"""Pixiv 解析插件：pid 解析 + 关注列表新作自动推送。

依赖配置里的 PHPSESSID Cookie（Pixiv 登录态）。推送使用 follow_latest 接口，
只拉取当前账号关注的人的最新作品，本地去重后推送到已绑定的会话。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

PLUGIN_NAME = "astrbot_plugin_pixiv_parser"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PID_RE = re.compile(r"(?:artworks/|illust_id=|pid[=: ]*|^)(\d{5,12})\b", re.I)
PIXIV_URL_RE = re.compile(r"https?://(?:www\.)?pixiv\.net/(?:artworks/|i/)?(\d{5,12})")
URL_RE = re.compile(r"https?://[^\s，,]+")


@dataclass
class Illust:
    pid: str
    title: str
    user: str
    uid: str
    tags: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.pixiv.net/artworks/{self.pid}"


class PixivClient:
    def __init__(self, session: aiohttp.ClientSession, cookie: str, proxy: str) -> None:
        self.session = session
        self.cookie = cookie
        self.proxy = proxy or None

    def headers(self) -> dict:
        return {
            "User-Agent": UA,
            "Referer": "https://www.pixiv.net/",
            "Accept": "application/json, text/plain, */*",
            "Cookie": self.cookie,
        }

    async def ajax(self, path: str, params: dict | None = None) -> Any:
        url = path if path.startswith("http") else f"https://www.pixiv.net{path}"
        async with self.session.get(
            url, params=params, headers=self.headers(), proxy=self.proxy
        ) as resp:
            if resp.status != 200:
                logger.warning("[pixiv] %s -> HTTP %s", path, resp.status)
                return None
            try:
                return await resp.json(content_type=None)
            except Exception:
                return None

    async def illust(self, pid: str) -> Illust | None:
        data = await self.ajax(f"/ajax/illust/{pid}")
        body = (data or {}).get("body")
        if not isinstance(body, dict):
            return None
        pages_data = await self.ajax(f"/ajax/illust/{pid}/pages")
        pages = []
        for item in (pages_data or {}).get("body") or []:
            url = (item or {}).get("urls", {}).get("original")
            if url:
                pages.append(url)
        if not pages:
            original = (body.get("urls") or {}).get("original")
            if original:
                pages = [original]
        return Illust(
            pid=str(body.get("illustId") or pid),
            title=str(body.get("title") or ""),
            user=str(body.get("userName") or ""),
            uid=str(body.get("userId") or ""),
            tags=[t for t in (body.get("tags") or {}).get("tags", []) if isinstance(t, str)],
            pages=pages,
        )

    async def follow_latest(self, page: int = 1) -> list[dict]:
        data = await self.ajax(
            "/ajax/follow_latest/illust", {"p": page, "mode": "all"}
        )
        body = (data or {}).get("body") or {}
        works = body.get("illusts") or []
        return [w for w in works if isinstance(w, dict) and w.get("illustId")]

    async def image_bytes(self, url: str) -> bytes | None:
        try:
            async with self.session.get(
                url,
                headers={**self.headers(), "Referer": "https://www.pixiv.net/"},
                proxy=self.proxy,
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                return data if len(data) > 512 else None
        except Exception as exc:
            logger.info("[pixiv] 图片下载失败：%s", exc)
            return None


class PixivPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.client: PixivClient | None = None
        self.data_dir: Path | None = None
        self.seen: set[str] = set()
        self.sessions: list[str] = []
        self._push_task: asyncio.Task | None = None
        self._baseline_done = False

    async def initialize(self) -> None:
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        connector = None
        try:
            import socket

            connector = aiohttp.TCPConnector(family=socket.AF_INET)
        except Exception:
            connector = None
        self.session = aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=60)
        )
        self.client = PixivClient(
            self.session,
            str(self.config.get("cookie") or ""),
            str(self.config.get("proxy") or ""),
        )
        self._load_state()
        if self.config.get("push_enabled", False) and self.sessions:
            self._push_task = asyncio.create_task(self._push_loop())

    async def terminate(self) -> None:
        if self._push_task is not None:
            self._push_task.cancel()
        if self.session is not None:
            await self.session.close()

    # -- 状态 ---------------------------------------------------------------
    def _state_file(self) -> Path:
        assert self.data_dir is not None
        return self.data_dir / "state.json"

    def _load_state(self) -> None:
        path = self._state_file()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.seen = set(str(x) for x in data.get("seen", []))
            self.sessions = [str(x) for x in data.get("sessions", [])]
        except Exception:
            logger.warning("[pixiv] 读取 state.json 失败")

    def _save_state(self) -> None:
        if self.data_dir is None:
            return
        try:
            payload = {"seen": list(self.seen)[-5000:], "sessions": self.sessions}
            self._state_file().write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("[pixiv] 保存 state.json 失败：%s", exc)

    # -- 发送 ---------------------------------------------------------------
    async def build_chain(self, illust: Illust, limit: int | None = None) -> list[Any]:
        chain: list[Any] = [
            Plain(
                f"【Pixiv】{illust.title}\n"
                f"作者：{illust.user}（{illust.uid}）\n"
                f"标签：{'、'.join(illust.tags[:8])}\n{illust.url}"
            )
        ]
        if self.client is None:
            return chain
        if limit is None:
            limit = int(self.config.get("max_images") or 4)
        for url in illust.pages[: max(1, limit)]:
            data = await self.client.image_bytes(url)
            if not data:
                continue
            chain.append(Image(file="base64://" + base64.b64encode(data).decode()))
        return chain

    async def send_to(self, session: str, chain: list[Any]) -> None:
        try:
            await self.context.send_message(session, MessageChain(chain))
        except Exception as exc:
            logger.warning("[pixiv] 推送到 %s 失败：%s", session[:30], exc)

    # -- 自动推送 -----------------------------------------------------------
    async def _push_loop(self) -> None:
        interval = max(1, int(self.config.get("poll_interval_minutes") or 30)) * 60
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[pixiv] 推送轮询异常：%s", exc)
            await asyncio.sleep(interval)

    async def _poll_once(self) -> None:
        if self.client is None:
            return
        works = await self.client.follow_latest(1)
        if not works:
            return
        if not self._baseline_done:
            for work in works:
                self.seen.add(str(work.get("illustId")))
            self._baseline_done = True
            self._save_state()
            logger.info("[pixiv] 已建立基线，记录 %d 个作品", len(works))
            return
        new_works = [w for w in works if str(w.get("illustId")) not in self.seen]
        if not new_works:
            return
        for work in reversed(new_works[:limit]):
            pid = str(work.get("illustId"))
            illust = await self.client.illust(pid)
            if illust is None:
                self.seen.add(pid)
                continue
            chain = await self.build_chain(illust)
            for session in list(self.sessions):
                await self.send_to(session, chain)
            self.seen.add(pid)
            logger.info("[pixiv] 已推送新作 %s", pid)
        self._save_state()

    # -- 指令 ---------------------------------------------------------------
    @filter.command("pixiv", alias={"p站", "pixiv解析"})
    async def pixiv(self, event: AstrMessageEvent, args: GreedyStr):
        text = str(args or "").strip()
        pid = text if text.isdigit() else self._extract_pid(text)
        if not pid:
            await event.send(
                MessageChain([Plain("用法：/pixiv 12345678 或 pixiv.net/artworks/12345678")])
            )
            return

    @filter.command("pixiv_force")
    async def pixiv_force(self, event: AstrMessageEvent):
        """立即强制推送关注列表最新作品，用于验证推送链路。"""
        if self.client is None:
            await event.send(MessageChain([Plain("\n".join(lines))]))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_parse(self, event: AstrMessageEvent):
        text = getattr(event, "message_str", "") or ""
        match = PIXIV_URL_RE.search(text)
        if not match:
            return
        try:
            event.call_llm = True
        except Exception:
            pass
        if self.client is None:
            return
        illust = await self.client.illust(match.group(1))
        if illust is None:
            return
        await event.send(MessageChain(await self.build_chain(illust)))

    @staticmethod
    def _extract_pid(text: str) -> str:
        match = PIXIV_URL_RE.search(text)
        if match:
            return match.group(1)
        match = PID_RE.search(text)
        return match.group(1) if match else ""
