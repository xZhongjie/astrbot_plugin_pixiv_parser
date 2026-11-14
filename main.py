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


def normalize_cookie(value: str) -> str:
    value = str(value or "").strip().strip('"').strip("'")
    if not value:
        return ""
    if "=" not in value:
        return "PHPSESSID=" + value
    return value


APP_HEADERS = {
    "App-OS": "ios",
    "App-OS-Version": "16.6",
    "App-Version": "7.16.0",
    "User-Agent": "PixivIOS/7.16.0 (iOS 16.6; iPhone14,2)",
    "Accept-Language": "zh-cn",
}
APP_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
APP_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"


class PixivClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        cookie: str,
        proxy: str,
        refresh_token: str = "",
    ) -> None:
        self.session = session
        self.cookie = normalize_cookie(cookie)
        self.proxy = proxy or None
        self.refresh_token = str(refresh_token or "").strip()
        self.access_token = ""
        self.token_expiry = 0.0
        self.last_error = ""

    def headers(self) -> dict:
        return {
            "User-Agent": UA,
            "Referer": "https://www.pixiv.net/",
            "Accept": "application/json, text/plain, */*",
            "Cookie": self.cookie,
        }

    async def _app_token(self) -> str:
        if self.access_token and time.time() < self.token_expiry:
            return self.access_token
        if not self.refresh_token:
            return ""
        data = {
            "client_id": APP_CLIENT_ID,
            "client_secret": APP_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "include_policy": "true",
        }
        try:
            async with self.session.post(
                "https://oauth.secure.pixiv.net/auth/token",
                data=data,
                headers={**APP_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                proxy=self.proxy,
            ) as resp:
                payload = await resp.json(content_type=None)
        except Exception as exc:
            self.last_error = f"oauth 请求失败：{exc}"
            logger.warning("[pixiv] 刷新 access_token 失败：%s", exc)
            return ""
        token = (payload or {}).get("access_token")
        if not token:
            self.last_error = f"refresh_token 无效：{str(payload)[:200]}"
            logger.warning("[pixiv] refresh_token 无效：%s", str(payload)[:200])
            return ""
        self.access_token = token
        self.token_expiry = time.time() + int((payload or {}).get("expires_in") or 3600) - 60
        logger.info("[pixiv] access_token 刷新成功")
        return token

    async def app_get(self, path: str, params: dict | None = None) -> Any:
        token = await self._app_token()
        if not token:
            return None
        try:
            async with self.session.get(
                "https://app-api.pixiv.net" + path,
                params=params,
                headers={**APP_HEADERS, "Authorization": f"Bearer {token}"},
                proxy=self.proxy,
            ) as resp:
                if resp.status == 401:
                    self.access_token = ""
                    token = await self._app_token()
                    if not token:
                        return None
                    async with self.session.get(
                        "https://app-api.pixiv.net" + path,
                        params=params,
                        headers={**APP_HEADERS, "Authorization": f"Bearer {token}"},
                        proxy=self.proxy,
                    ) as retry:
                        return await retry.json(content_type=None)
                return await resp.json(content_type=None)
        except Exception as exc:
            logger.warning("[pixiv] app API 失败：%s", exc)
            return None

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
        if self.refresh_token:
            return await self.app_illust(pid)
        data = await self.ajax(f"/ajax/illust/{pid}")
        body = (data or {}).get("body")
        if not isinstance(body, dict):
            return None
        pages_data = await self.ajax(f"/ajax/illust/{pid}/pages")
        pages = []
        for item in (pages_data or {}).get("body") or []:
            url = ((item or {}).get("urls") or {}).get("original")
            if url:
                pages.append(url)
        if not pages:
            urls = body.get("urls") or {}
            original = urls.get("original") or urls.get("large")
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

    async def app_illust(self, pid: str) -> Illust | None:
        data = await self.app_get("/v1/illust/detail", {"illust_id": pid})
        body = (data or {}).get("illust") or {}
        if not body:
            return None
        pages = []
        for page in body.get("meta_pages") or []:
            url = (page.get("image_urls") or {}).get("original")
            if url:
                pages.append(url)
        if not pages:
            single = (body.get("meta_single_page") or {}).get("original_image_url")
            if single:
                pages.append(str(single))
        if not pages:
            urls = body.get("image_urls") or {}
            url = urls.get("original")
            if not url:
                logger.warning("[pixiv] 没有 original_image_url，回退压缩图")
                url = urls.get("large")
            if url:
                pages.append(url)
        tags = []
        for tag in body.get("tags") or []:
            if isinstance(tag, str):
                tags.append(tag)
            elif isinstance(tag, dict) and tag.get("name"):
                tags.append(str(tag["name"]))
        user = body.get("userName") or (body.get("user") or {}).get("name") or ""
        uid = body.get("userId") or (body.get("user") or {}).get("id") or ""
        logger.info("[pixiv] illust detail keys=%s user=%s", list(body.keys())[:12], bool(user))
        return Illust(
            pid=str(body.get("id") or pid),
            title=str(body.get("title") or ""),
            user=str(user),
            uid=str(uid),
            tags=tags,
            pages=pages,
        )

    async def follow_latest(self, page: int = 1) -> list[dict]:
        if self.refresh_token:
            data = await self.app_get("/v2/illust/follow", {"restrict": "all"})
            body = (data or {}).get("body") if isinstance(data, dict) and "body" in data else (data or {})
            works = (body or {}).get("illusts") or []
            logger.info("[pixiv] app follow: count=%d", len(works))
            return [w for w in works if isinstance(w, dict) and (w.get("id") or w.get("illustId"))]

        data = await self.ajax(
            "/ajax/follow_latest/illust", {"p": page, "mode": "all"}
        )
        body = (data or {}).get("body") or {}
        works = list(body.get("illusts") or [])
        if not works:
            for thumb in body.get("thumbnails") or []:
                if isinstance(thumb, dict) and isinstance(thumb.get("illust"), dict):
                    works.append(thumb["illust"])
                elif isinstance(thumb, dict) and thumb.get("illustId"):
                    works.append(thumb)
        thumbs = body.get("thumbnails") or []
        first_keys = list(thumbs[0].keys())[:8] if thumbs and isinstance(thumbs[0], dict) else []
        logger.info(
            "[pixiv] follow_latest: error=%s keys=%s count=%d thumbs=%d first=%s",
            (data or {}).get("error"),
            list(body.keys())[:6] if isinstance(body, dict) else type(body).__name__,
            len(works),
            len(thumbs),
            first_keys,
        )
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
            str(self.config.get("refresh_token") or ""),
        )
        self._load_state()
        logger.info(
            "[pixiv] 插件已加载：cookie=%s，推送会话=%d",
            "已配置" if self.config.get("cookie") else "未配置",
            len(self.sessions),
        )
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
        if illust.pages:
            logger.info("[pixiv] 发送图源：%s", illust.pages[0][:120])
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

    @staticmethod
    def _work_id(work: dict) -> str:
        return str(work.get("id") or work.get("illustId") or "")

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
                self.seen.add(self._work_id(work))
            self._baseline_done = True
            self._save_state()
            logger.info("[pixiv] 已建立基线，记录 %d 个作品", len(works))
            return
        new_works = [w for w in works if self._work_id(w) not in self.seen]
        if not new_works:
            return
        limit = max(1, int(self.config.get("push_limit") or 3))
        for work in reversed(new_works[:limit]):
            pid = self._work_id(work)
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
            await event.send(MessageChain([Plain("用法：/pixiv 12345678 或 pixiv.net/artworks/12345678")]))
            return
        if self.client is None:
            await event.send(MessageChain([Plain("插件尚未初始化")]))
            return
        illust = await self.client.illust(pid)
        if illust is None:
            await event.send(MessageChain([Plain(f"解析失败：{pid} 不存在或 Cookie 失效")]))
            return
        await event.send(MessageChain(await self.build_chain(illust)))

    @filter.command("pixiv_test")
    async def pixiv_test(self, event: AstrMessageEvent):
        if self.client is None:
            await event.send(MessageChain([Plain("插件尚未初始化")]))
            return
        works = await self.client.follow_latest(1)
        if not works:
            if self.client.refresh_token:
                reason = self.client.last_error or "关注列表为空，或 token 无法换取 access_token"
                await event.send(MessageChain([Plain(f"检查结果：{reason}")]))
            else:
                await event.send(MessageChain([Plain("检查结果：Cookie 无效或关注列表为空。请检查 PHPSESSID 和代理。")]))
            return
        lines = [
            "检查结果：",
            f"Cookie：有效（follow_latest 返回 {len(works)} 条）",
            f"推送会话：{len(self.sessions)} 个",
            f"已记录作品：{len(self.seen)} 个",
            f"轮询间隔：{self.config.get('poll_interval_minutes') or 30} 分钟",
            "示例：",
        ]
        for work in works[:3]:
            lines.append(f"- {work.get('illustId')} {work.get('title') or ''}")
        if not self.sessions:
            lines.append("还没有绑定会话，发送 /pixiv_push")
        await event.send(MessageChain([Plain("\n".join(lines))]))

    @filter.command("pixiv_force")
    async def pixiv_force(self, event: AstrMessageEvent):
        logger.info("[pixiv] force 指令触发")
        if self.client is None:
            await event.send(MessageChain([Plain("插件尚未初始化")]))
            return
        try:
            works = await self.client.follow_latest(1)
        except Exception as exc:
            logger.warning("[pixiv] force 拉取异常：%s", exc)
            works = []
        logger.info("[pixiv] force 拉取到 %d 条，会话 %d 个", len(works or []), len(self.sessions))
        if not works:
            await event.send(MessageChain([Plain("拉取失败：Cookie 无效或网络异常")]))
            return
        if not self.sessions:
            await event.send(MessageChain([Plain("还没有绑定会话，先发 /pixiv_push")]))
            return
        limit = max(1, int(self.config.get("push_limit") or 3))
        sent = 0
        for work in works[:limit]:
            pid = self._work_id(work)
            illust = await self.client.illust(pid)
            if illust is None:
                continue
            chain = await self.build_chain(illust)
            for session in list(self.sessions):
                await self.send_to(session, chain)
            self.seen.add(pid)
            sent += 1
        self._save_state()
        await event.send(MessageChain([Plain(f"已强制推送 {sent} 个作品到 {len(self.sessions)} 个会话")]))

    @filter.command("pixiv_push")
    async def pixiv_push(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        if umo in self.sessions:
            self.sessions.remove(umo)
            state = "已关闭"
        else:
            self.sessions.append(umo)
            state = "已开启"
        self._save_state()
        await event.send(MessageChain([Plain(f"Pixiv 新作推送{state}（当前 {len(self.sessions)} 个会话）")]))

    @filter.command("pixiv_status")
    async def pixiv_status(self, event: AstrMessageEvent):
        lines = [
            f"Cookie：{'已配置' if self.config.get('cookie') else '未配置'}",
            f"推送会话：{len(self.sessions)} 个",
            f"已记录作品：{len(self.seen)} 个",
            f"轮询间隔：{self.config.get('poll_interval_minutes') or 30} 分钟",
        ]
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
