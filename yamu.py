__version__ = (3, 2, 0)

import asyncio
import aiohttp
import json
import random
import string
import logging
from asyncio import sleep
from yandex_music import ClientAsync
from telethon import TelegramClient
from telethon.tl.types import Message
from telethon.errors.rpcerrorlist import FloodWaitError, MessageNotModifiedError
from telethon.tl.functions.account import UpdateProfileRequest
from .. import loader, utils  # type: ignore

logger = logging.getLogger(__name__)
logging.getLogger("yandex_music").propagate = False

async def get_current_track(client, token):
    device_info = {"app_name": "Chrome", "type": 1}
    ws_proto = {
        "Ynison-Device-Id": "".join(random.choice(string.ascii_lowercase) for _ in range(16)),
        "Ynison-Device-Info": json.dumps(device_info),
    }
    timeout = aiohttp.ClientTimeout(total=15, connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                url="wss://ynison.music.yandex.ru/redirector.YnisonRedirectService/GetRedirectToYnison",
                headers={
                    "Sec-WebSocket-Protocol": f"Bearer, v2, {json.dumps(ws_proto)}",
                    "Origin": "http://music.yandex.ru",
                    "Authorization": f"OAuth {token}",
                },
                timeout=10,
            ) as ws:
                recv = await ws.receive()
                data = json.loads(recv.data)

            if "redirect_ticket" not in data or "host" not in data:
                return {"success": False}

            new_ws_proto = ws_proto.copy()
            new_ws_proto["Ynison-Redirect-Ticket"] = data["redirect_ticket"]

            to_send = {
                "update_full_state": {
                    "player_state": {
                        "player_queue": {
                            "current_playable_index": -1,
                            "entity_id": "",
                            "entity_type": "VARIOUS",
                            "playable_list": [],
                            "options": {"repeat_mode": "NONE"},
                            "entity_context": "BASED_ON_ENTITY_BY_DEFAULT",
                            "version": {"device_id": ws_proto["Ynison-Device-Id"], "version": 9021243204784341000, "timestamp_ms": 0},
                            "from_optional": "",
                        },
                        "status": {"duration_ms": 0, "paused": True, "playback_speed": 1, "progress_ms": 0, "version": {"device_id": ws_proto["Ynison-Device-Id"], "version": 8321822175199937000, "timestamp_ms": 0}},
                    },
                    "device": {"capabilities": {"can_be_player": True, "can_be_remote_controller": False, "volume_granularity": 16}, "info": {"device_id": ws_proto["Ynison-Device-Id"], "type": "WEB", "title": "Chrome Browser", "app_name": "Chrome"}, "volume_info": {"volume": 0}, "is_shadow": True},
                    "is_currently_active": False,
                },
                "rid": "ac281c26-a047-4419-ad00-e4fbfda1cba3",
                "player_action_timestamp_ms": 0,
                "activity_interception_type": "DO_NOT_INTERCEPT_BY_DEFAULT",
            }

            async with session.ws_connect(
                url=f"wss://{data['host']}/ynison_state.YnisonStateService/PutYnisonState",
                headers={
                    "Sec-WebSocket-Protocol": f"Bearer, v2, {json.dumps(new_ws_proto)}",
                    "Origin": "http://music.yandex.ru",
                    "Authorization": f"OAuth {token}",
                },
                timeout=10,
                method="GET",
            ) as ws:
                await ws.send_str(json.dumps(to_send))
                recv = await asyncio.wait_for(ws.receive(), timeout=10)
                ynison = json.loads(recv.data)
                track_index = ynison["player_state"]["player_queue"]["current_playable_index"]
                if track_index == -1:
                    return {"success": False}
                track = ynison["player_state"]["player_queue"]["playable_list"][track_index]

            info = await client.tracks_download_info(track["playable_id"], True)
            track = await client.tracks(track["playable_id"])
            return {
                "paused": ynison["player_state"]["status"]["paused"],
                "duration_ms": ynison["player_state"]["status"]["duration_ms"],
                "progress_ms": ynison["player_state"]["status"]["progress_ms"],
                "entity_id": ynison["player_state"]["player_queue"]["entity_id"],
                "repeat_mode": ynison["player_state"]["player_queue"]["options"]["repeat_mode"],
                "entity_type": ynison["player_state"]["player_queue"]["entity_type"],
                "track": track,
                "info": info,
                "success": True,
            }

    except Exception as e:
        return {"success": False, "error": str(e), "track": None}


@loader.tds
class YaMusicFastMod(loader.Module):
    """Fast Yandex.Music module with AutoBio and widgets"""

    strings = {
        "name": "YaMusicFast",
        "no_token": "<b>🚫 Specify a token in config!</b>",
        "playing": "<b>🎶 Now playing: </b><code>{}</code><b> - </b><code>{}</code>\n<b>🕐 {}</b>",
        "autobioe": "<b>🔁 Autobio enabled</b>",
        "autobiod": "<b>🔁 Autobio disabled</b>",
        "_cfg_yandexmusictoken": "Yandex.Music account token",
        "_cfg_autobiotemplate": "Template for AutoBio",
        "_cfg_automesgtemplate": "Template for AutoMessage",
        "_cfg_update_interval": "Update interval",
        "guide": '<a href="https://github.com/MarshalX/yandex-music-api/discussions/513#discussioncomment-2729781">How to get token</a>',
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue("YandexMusicToken", None, lambda: self.strings["_cfg_yandexmusictoken"], validator=loader.validators.Hidden()),
            loader.ConfigValue("AutoBioTemplate", "🎧 {artists} - {track} / {time}", lambda: self.strings["_cfg_autobiotemplate"], validator=loader.validators.String()),
            loader.ConfigValue("AutoMessageTemplate", "🎧 {artists} - {track} / {time} {link}", lambda: self.strings["_cfg_automesgtemplate"], validator=loader.validators.String()),
            loader.ConfigValue("update_interval", 300, lambda: self.strings["_cfg_update_interval"], validator=loader.validators.Integer(minimum=100)),
        )

    async def client_ready(self, client: TelegramClient, db):
        self.client = client
        self.db = db
        self._premium = getattr(await self.client.get_me(), "premium", False)
        self.set("widgets", list(map(tuple, self.get("widgets", []))))
        self._task = asyncio.ensure_future(self._parse())
        if self.get("autobio", False):
            self.autobio.start()

    @loader.command()
    async def yncmd(self, message: Message):
        """Get now playing track"""
        if not self.config["YandexMusicToken"]:
            await utils.answer(message, self.strings["no_token"])
            return
        client = ClientAsync(self.config["YandexMusicToken"])
        await client.init()
        res = await get_current_track(client, self.config["YandexMusicToken"])
        if not res["success"]:
            await utils.answer(message, "No track playing")
            return
        track = res["track"][0]
        artists = ", ".join(a["name"] for a in track["artists"])
        title = track["title"]
        duration_ms = int(track["duration_ms"])
        await utils.answer(message, self.strings["playing"].format(artists, title, f"{duration_ms//1000//60:02}:{duration_ms//1000%60:02}"))

    @loader.command()
    async def ybio(self, message: Message):
        """Toggle AutoBio"""
        if not self.config["YandexMusicToken"]:
            await utils.answer(message, self.strings["no_token"])
            return
        current = self.get("autobio", False)
        new = not current
        self.set("autobio", new)
        if new:
            await utils.answer(message, self.strings["autobioe"])
            self.autobio.start()
        else:
            await utils.answer(message, self.strings["autobiod"])
            self.autobio.stop()

    @loader.loop(interval=60)
    async def autobio(self):
        client = ClientAsync(self.config["YandexMusicToken"])
        await client.init()
        res = await get_current_track(client, self.config["YandexMusicToken"])
        if not res["success"]:
            return
        track = res["track"][0]
        artists = ", ".join(a["name"] for a in track["artists"])
        title = track["title"]
        duration_ms = int(track["duration_ms"])
        text = self.config["AutoBioTemplate"].format(artists=artists, track=title, time=f"{duration_ms//1000//60:02}:{duration_ms//1000%60:02}")
        try:
            await self.client(UpdateProfileRequest(about=text[:140 if self._premium else 70]))
        except FloodWaitError as e:
            logger.info(f"Sleeping {e.seconds}")
            await sleep(e.seconds)

    async def _parse(self, do_not_loop=False):
        while True:
            for widget in self.get("widgets", []):
                client = ClientAsync(self.config["YandexMusicToken"])
                await client.init()
                res = await get_current_track(client, self.config["YandexMusicToken"])
                if not res["success"]:
                    continue
                track = res["track"][0]
                artists = ", ".join(a["name"] for a in track["artists"])
                title = track["title"]
                duration_ms = int(track["duration_ms"])
                try:
                    await self.client.edit_message(
                        *widget[:2],
                        self.config["AutoMessageTemplate"].format(
                            artists=artists,
                            track=title,
                            time=f"{duration_ms//1000//60:02}:{duration_ms//1000%60:02}",
                            link=f"https://song.link/ya/{track['id']}",
                        ),
                    )
                except (MessageNotModifiedError, FloodWaitError):
                    pass
            if do_not_loop:
                break
            await asyncio.sleep(int(self.config["update_interval"]))

    async def on_unload(self):
        self._task.cancel()
