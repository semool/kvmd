# ========================================================================== #
#                                                                            #
#    KVMD - The main PiKVM daemon.                                           #
#                                                                            #
#    Copyright (C) 2020  Maxim Devaev <mdevaev@gmail.com>                    #
#                                                                            #
#    This program is free software: you can redistribute it and/or modify    #
#    it under the terms of the GNU General Public License as published by    #
#    the Free Software Foundation, either version 3 of the License, or       #
#    (at your option) any later version.                                     #
#                                                                            #
#    This program is distributed in the hope that it will be useful,         #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#    GNU General Public License for more details.                            #
#                                                                            #
#    You should have received a copy of the GNU General Public License       #
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.  #
#                                                                            #
# ========================================================================== #


import asyncio
import contextlib
import struct

from typing import Callable
from typing import AsyncGenerator

import aiohttp

from .. import aiotools
from .. import htclient
from .. import htserver

from . import BaseHttpClient
from . import BaseHttpClientSession


# =====
class _BaseApiPart:
    def __init__(self, ensure_http_session: Callable[[], aiohttp.ClientSession]) -> None:
        self._ensure_http_session = ensure_http_session

    async def _set_params(self, handle: str, **params: (int | str | None)) -> None:
        session = self._ensure_http_session()
        async with session.post(
            url=handle,
            params={
                key: value
                for (key, value) in params.items()
                if value is not None
            },
        ) as response:
            htclient.raise_not_200(response)


class _AuthApiPart(_BaseApiPart):
    async def check(self) -> bool:
        session = self._ensure_http_session()
        try:
            async with session.get("/auth/check") as response:
                htclient.raise_not_200(response)
                return True
        except aiohttp.ClientResponseError as ex:
            if ex.status in [400, 401, 403]:
                return False
            raise


class _StreamerApiPart(_BaseApiPart):
    async def get_state(self) -> dict:
        session = self._ensure_http_session()
        async with session.get("/streamer") as response:
            htclient.raise_not_200(response)
            return (await response.json())["result"]

    async def set_params(self, quality: (int | None)=None, desired_fps: (int | None)=None) -> None:
        await self._set_params(
            "/streamer/set_params",
            quality=quality,
            desired_fps=desired_fps,
        )


class _HidApiPart(_BaseApiPart):
    async def get_keymaps(self) -> tuple[str, set[str]]:
        session = self._ensure_http_session()
        async with session.get("/hid/keymaps") as response:
            htclient.raise_not_200(response)
            result = (await response.json())["result"]
            return (result["keymaps"]["default"], set(result["keymaps"]["available"]))

    async def print(self, text: str, limit: int, keymap_name: str) -> None:
        session = self._ensure_http_session()
        async with session.post(
            url="/hid/print",
            params={"limit": limit, "keymap": keymap_name},
            data=text,
        ) as response:
            htclient.raise_not_200(response)

    async def set_params(self, keyboard_output: (str | None)=None, mouse_output: (str | None)=None) -> None:
        await self._set_params(
            "/hid/set_params",
            keyboard_output=keyboard_output,
            mouse_output=mouse_output,
        )


class _AtxApiPart(_BaseApiPart):
    async def get_state(self) -> dict:
        session = self._ensure_http_session()
        async with session.get("/atx") as response:
            htclient.raise_not_200(response)
            return (await response.json())["result"]

    async def switch_power(self, action: str) -> bool:
        session = self._ensure_http_session()
        try:
            async with session.post(
                url="/atx/power",
                params={"action": action},
            ) as response:
                htclient.raise_not_200(response)
                return True
        except aiohttp.ClientResponseError as ex:
            if ex.status == 409:
                return False
            raise


# =====
class KvmdClientWs:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.__ws = ws
        self.__writer_queue: "asyncio.Queue[tuple[str, dict] | bytes]" = asyncio.Queue()
        self.__communicated = False

    async def communicate(self) -> AsyncGenerator[tuple[str, dict], None]:  # pylint: disable=too-many-branches
        assert not self.__communicated
        self.__communicated = True
        receive_task: (asyncio.Task | None) = None
        writer_task: (asyncio.Task | None) = None
        try:
            while True:
                if receive_task is None:
                    receive_task = asyncio.create_task(self.__ws.receive())
                if writer_task is None:
                    writer_task = asyncio.create_task(self.__writer_queue.get())

                done = (await aiotools.wait_first(receive_task, writer_task))[0]

                if receive_task in done:
                    msg = receive_task.result()
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        yield htserver.parse_ws_event(msg.data)
                    elif msg.type == aiohttp.WSMsgType.CLOSE:
                        await self.__ws.close()
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        break
                    else:
                        raise RuntimeError(f"Unhandled WS message type: {msg!r}")
                    receive_task = None

                if writer_task in done:
                    payload = writer_task.result()
                    if isinstance(payload, bytes):
                        await self.__ws.send_bytes(payload)
                    else:
                        await htserver.send_ws_event(self.__ws, *payload)
                    writer_task = None
        finally:
            if receive_task:
                receive_task.cancel()
            if writer_task:
                writer_task.cancel()
            try:
                await aiotools.shield_fg(self.__ws.close())
            except Exception:
                pass
            finally:
                self.__communicated = False

    async def send_key_event(self, key: str, state: bool) -> None:
        mask = (0b01 if state else 0)
        await self.__writer_queue.put(bytes([1, mask]) + key.encode("ascii"))

    async def send_mouse_button_event(self, button: str, state: bool) -> None:
        mask = (0b01 if state else 0)
        await self.__writer_queue.put(bytes([2, mask]) + button.encode("ascii"))

    async def send_mouse_move_event(self, to_x: int, to_y: int) -> None:
        await self.__writer_queue.put(struct.pack(">bhh", 3, to_x, to_y))

    async def send_mouse_wheel_event(self, delta_x: int, delta_y: int) -> None:
        await self.__writer_queue.put(struct.pack(">bbbb", 5, 0, delta_x, delta_y))


class KvmdClientSession(BaseHttpClientSession):
    def __init__(self, make_http_session: Callable[[], aiohttp.ClientSession]) -> None:
        super().__init__(make_http_session)
        self.auth = _AuthApiPart(self._ensure_http_session)
        self.streamer = _StreamerApiPart(self._ensure_http_session)
        self.hid = _HidApiPart(self._ensure_http_session)
        self.atx = _AtxApiPart(self._ensure_http_session)

    @contextlib.asynccontextmanager
    async def ws(self) -> AsyncGenerator[KvmdClientWs, None]:
        session = self._ensure_http_session()
        async with session.ws_connect("/ws", params={"legacy": "0"}) as ws:
            yield KvmdClientWs(ws)


class KvmdClient(BaseHttpClient):
    def make_session(self, user: str="", passwd: str="") -> KvmdClientSession:
        headers = {
            "X-KVMD-User": user,
            "X-KVMD-Passwd": passwd,
        }
        return KvmdClientSession(lambda: self._make_http_session(headers))
