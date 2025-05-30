# ========================================================================== #
#                                                                            #
#    KVMD - The main PiKVM daemon.                                           #
#                                                                            #
#    Copyright (C) 2018-2024  Maxim Devaev <mdevaev@gmail.com>               #
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


import os
import socket
import struct
import asyncio
import contextlib
import dataclasses
import inspect
import urllib.parse
import json

from typing import Callable
from typing import AsyncGenerator
from typing import Any

from aiohttp import ClientWebSocketResponse
from aiohttp.web import BaseRequest
from aiohttp.web import Request
from aiohttp.web import Response
from aiohttp.web import StreamResponse
from aiohttp.web import WebSocketResponse
from aiohttp.web import WSMsgType
from aiohttp.web import Application
from aiohttp.web import AccessLogger
from aiohttp.web import run_app
from aiohttp.web import normalize_path_middleware

from .logging import get_logger

from .errors import OperationError
from .errors import IsBusyError

from .validators import ValidatorError

from . import aiotools


# =====
class HttpError(Exception):
    def __init__(self, msg: str, status: int) -> None:
        super().__init__(msg)
        self.status = status


class UnauthorizedError(HttpError):
    def __init__(self) -> None:
        super().__init__("Unauthorized", 401)


class ForbiddenError(HttpError):
    def __init__(self) -> None:
        super().__init__("Forbidden", 403)


class UnavailableError(HttpError):
    def __init__(self) -> None:
        super().__init__("Service Unavailable", 503)


# =====
@dataclasses.dataclass(frozen=True)
class HttpExposed:
    method: str
    path: str
    auth_required: bool
    allow_usc: bool
    handler: Callable


_HTTP_EXPOSED = "_http_exposed"
_HTTP_METHOD = "_http_method"
_HTTP_PATH = "_http_path"
_HTTP_AUTH_REQUIRED = "_http_auth_required"
_HTTP_ALLOW_USC = "_http_allow_usc"


def exposed_http(
    http_method: str,
    path: str,
    auth_required: bool=True,
    allow_usc: bool=True,
) -> Callable:

    def set_attrs(handler: Callable) -> Callable:
        setattr(handler, _HTTP_EXPOSED, True)
        setattr(handler, _HTTP_METHOD, http_method)
        setattr(handler, _HTTP_PATH, path)
        setattr(handler, _HTTP_AUTH_REQUIRED, auth_required)
        setattr(handler, _HTTP_ALLOW_USC, allow_usc)
        return handler
    return set_attrs


def _get_exposed_http(obj: object) -> list[HttpExposed]:
    return [
        HttpExposed(
            method=getattr(handler, _HTTP_METHOD),
            path=getattr(handler, _HTTP_PATH),
            auth_required=getattr(handler, _HTTP_AUTH_REQUIRED),
            allow_usc=getattr(handler, _HTTP_ALLOW_USC),
            handler=handler,
        )
        for handler in [getattr(obj, name) for name in dir(obj)]
        if inspect.ismethod(handler) and getattr(handler, _HTTP_EXPOSED, False)
    ]


# =====
@dataclasses.dataclass(frozen=True)
class WsExposed:
    event_type: str
    binary: bool
    handler: Callable


_WS_EXPOSED = "_ws_exposed"
_WS_BINARY = "_ws_binary"
_WS_EVENT_TYPE = "_ws_event_type"


def exposed_ws(event_type: (str | int)) -> Callable:
    def set_attrs(handler: Callable) -> Callable:
        setattr(handler, _WS_EXPOSED, True)
        setattr(handler, _WS_BINARY, isinstance(event_type, int))
        setattr(handler, _WS_EVENT_TYPE, str(event_type))
        return handler
    return set_attrs


def _get_exposed_ws(obj: object) -> list[WsExposed]:
    return [
        WsExposed(
            event_type=getattr(handler, _WS_EVENT_TYPE),
            binary=getattr(handler, _WS_BINARY),
            handler=handler,
        )
        for handler in [getattr(obj, name) for name in dir(obj)]
        if inspect.ismethod(handler) and getattr(handler, _WS_EXPOSED, False)
    ]


# =====
def make_json_response(
    result: (dict | None)=None,
    status: int=200,
    set_cookies: (dict[str, str] | None)=None,
    wrap_result: bool=True,
) -> Response:

    resp = Response(
        text=json.dumps(({
            "ok": (status == 200),
            "result": (result or {}),
        } if wrap_result else result), sort_keys=True, indent=4),
        status=status,
        content_type="application/json",
    )
    if set_cookies:
        for (key, value) in set_cookies.items():
            resp.set_cookie(key, value, httponly=True, samesite="Strict")
    return resp


def make_json_exception(ex: Exception, status: (int | None)=None) -> Response:
    name = type(ex).__name__
    msg = str(ex)
    if isinstance(ex, HttpError):
        status = ex.status
    else:
        get_logger().error("API error: %s: %s", name, msg)
    assert status is not None, ex
    return make_json_response({
        "error": name,
        "error_msg": msg,
    }, status=status)


async def start_streaming(
    req: Request,
    content_type: str,
    content_length: int=-1,
    file_name: str="",
) -> StreamResponse:

    resp = StreamResponse(status=200, reason="OK")
    resp.content_type = content_type
    if content_length >= 0:  # pylint: disable=consider-using-min-builtin
        resp.content_length = content_length
    if file_name:
        file_name = urllib.parse.quote(file_name, safe="")
        resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{file_name}"
    await resp.prepare(req)
    return resp


async def stream_json(resp: StreamResponse, result: dict, ok: bool=True) -> None:
    await resp.write(json.dumps({
        "ok": ok,
        "result": result,
    }).encode("utf-8") + b"\r\n")


async def stream_json_exception(resp: StreamResponse, ex: Exception) -> None:
    name = type(ex).__name__
    msg = str(ex)
    get_logger().error("API error: %s: %s", name, msg)
    await stream_json(resp, {
        "error": name,
        "error_msg": msg,
    }, False)


async def send_ws_event(
    wsr: (ClientWebSocketResponse | WebSocketResponse),
    event_type: str,
    event: (dict | None),
) -> None:

    await wsr.send_str(json.dumps({
        "event_type": event_type,
        "event": event,
    }))


async def send_ws_bin(
    wsr: (ClientWebSocketResponse | WebSocketResponse),
    op: int,
    data: bytes,
) -> None:

    assert 0 <= op <= 255
    await wsr.send_bytes(op.to_bytes() + data)


def parse_ws_event(msg: str) -> tuple[str, dict]:
    data = json.loads(msg)
    if not isinstance(data, dict):
        raise RuntimeError("Top-level event structure is not a dict")
    event_type = data.get("event_type")
    if not isinstance(event_type, str):
        raise RuntimeError("event_type must be a string")
    event = data["event"]
    if not isinstance(event, dict):
        raise RuntimeError("event must be a dict")
    return (event_type, event)


# =====
_REQUEST_AUTH_INFO = "_kvmd_auth_info"


def _format_P(req: BaseRequest, *_, **__) -> str:  # type: ignore  # pylint: disable=invalid-name
    return (getattr(req, _REQUEST_AUTH_INFO, None) or "-")


AccessLogger._format_P = staticmethod(_format_P)  # type: ignore  # pylint: disable=protected-access


def set_request_auth_info(req: BaseRequest, info: str) -> None:
    setattr(req, _REQUEST_AUTH_INFO, info)


@dataclasses.dataclass(frozen=True)
class RequestUnixCredentials:
    pid: int
    uid: int
    gid: int

    def __post_init__(self) -> None:
        assert self.pid >= 0
        assert self.uid >= 0
        assert self.gid >= 0


def get_request_unix_credentials(req: BaseRequest) -> (RequestUnixCredentials | None):
    if req.transport is None:
        return None
    sock = req.transport.get_extra_info("socket")
    if sock is None:
        return None
    try:
        data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iii"))
    except Exception:
        return None
    (pid, uid, gid) = struct.unpack("iii", data)
    if pid < 0 or uid < 0 or gid < 0:
        # PID == 0 when the client is outside of server's PID namespace, e.g. when kvmd runs in a container
        return None
    return RequestUnixCredentials(pid=pid, uid=uid, gid=gid)


# =====
@dataclasses.dataclass(frozen=True)
class WsSession:
    wsr: WebSocketResponse
    kwargs: dict[str, Any] = dataclasses.field(hash=False)

    def __str__(self) -> str:
        return f"WsSession(id={id(self)}, {self.kwargs})"

    def is_alive(self) -> bool:
        return (
            not self.wsr.closed
            and self.wsr._req is not None  # pylint: disable=protected-access
            and self.wsr._req.transport is not None  # pylint: disable=protected-access
        )

    async def send_event(self, event_type: str, event: (dict | None)) -> None:
        await send_ws_event(self.wsr, event_type, event)

    async def send_bin(self, op: int, data: bytes) -> None:
        await send_ws_bin(self.wsr, op, data)


class HttpServer:
    def __init__(self) -> None:
        self.__ws_heartbeat: (float | None) = None
        self.__ws_handlers: dict[str, Callable] = {}
        self.__ws_bin_handlers: dict[int, Callable] = {}
        self.__ws_sessions: list[WsSession] = []
        self.__ws_sessions_lock = asyncio.Lock()

    def run(
        self,
        unix_path: str,
        unix_rm: bool,
        unix_mode: int,
        heartbeat: float,
        access_log_format: str,
    ) -> None:

        self.__ws_heartbeat = heartbeat

        if unix_rm and os.path.exists(unix_path):
            os.remove(unix_path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        sock.bind(unix_path)
        if unix_mode:
            os.chmod(unix_path, unix_mode)

        run_app(
            sock=sock,
            app=self.__make_app(),
            shutdown_timeout=1,
            access_log_format=access_log_format,
            print=self.__run_app_print,
            loop=asyncio.get_event_loop(),
        )

    # =====

    def _add_exposed(self, *objs: object) -> None:
        for obj in objs:
            for http_exposed in _get_exposed_http(obj):
                self.__add_exposed_http(http_exposed)
            for ws_exposed in _get_exposed_ws(obj):
                self.__add_exposed_ws(ws_exposed)

    def __add_exposed_http(self, exposed: HttpExposed) -> None:
        async def wrapper(req: Request) -> Response:
            try:
                await self._check_request_auth(exposed, req)
                return (await exposed.handler(req))
            except IsBusyError as ex:
                return make_json_exception(ex, 409)
            except (ValidatorError, OperationError) as ex:
                return make_json_exception(ex, 400)
            except HttpError as ex:
                return make_json_exception(ex)
        self.__app.router.add_route(exposed.method, exposed.path, wrapper)

    def __add_exposed_ws(self, exposed: WsExposed) -> None:
        if exposed.binary:
            event_type = int(exposed.event_type)
            assert event_type not in self.__ws_bin_handlers
            self.__ws_bin_handlers[event_type] = exposed.handler
        else:
            assert exposed.event_type not in self.__ws_handlers
            self.__ws_handlers[exposed.event_type] = exposed.handler

    # =====

    @contextlib.asynccontextmanager
    async def _ws_session(self, req: Request, **kwargs: Any) -> AsyncGenerator[WsSession, None]:
        assert self.__ws_heartbeat is not None
        wsr = WebSocketResponse(heartbeat=self.__ws_heartbeat)
        await wsr.prepare(req)
        ws = WsSession(wsr, kwargs)

        async with self.__ws_sessions_lock:
            self.__ws_sessions.append(ws)
            get_logger(2).info("Registered new client session: %s; clients now: %d", ws, len(self.__ws_sessions))

        try:
            await self._on_ws_opened(ws)
            yield ws
        finally:
            await aiotools.shield_fg(self.__close_ws(ws))

    async def _ws_loop(self, ws: WsSession) -> WebSocketResponse:
        logger = get_logger()
        async for msg in ws.wsr:
            if msg.type == WSMsgType.TEXT:
                try:
                    (event_type, event) = parse_ws_event(msg.data)
                except Exception as ex:
                    logger.error("Can't parse JSON event from websocket: %r", ex)
                else:
                    handler = self.__ws_handlers.get(event_type)
                    if handler:
                        await handler(ws, event)
                    else:
                        logger.error("Unknown websocket event: %r", msg.data)

            elif msg.type == WSMsgType.BINARY and len(msg.data) >= 1:
                handler = self.__ws_bin_handlers.get(msg.data[0])
                if handler:
                    await handler(ws, msg.data[1:])
                else:
                    logger.error("Unknown websocket binary event: %r", msg.data)

            else:
                break
        return ws.wsr

    async def _broadcast_ws_event(self, event_type: str, event: (dict | None)) -> None:
        if self.__ws_sessions:
            await asyncio.gather(*[
                ws.send_event(event_type, event)
                for ws in self.__ws_sessions
                if ws.is_alive()
            ], return_exceptions=True)

    async def _close_all_wss(self) -> bool:
        wss = self._get_wss()
        for ws in wss:
            await self.__close_ws(ws)
        return bool(wss)

    def _get_wss(self) -> list[WsSession]:
        return list(self.__ws_sessions)

    async def __close_ws(self, ws: WsSession) -> None:
        async with self.__ws_sessions_lock:
            try:
                self.__ws_sessions.remove(ws)
                get_logger(3).info("Removed client socket: %s; clients now: %d", ws, len(self.__ws_sessions))
                await ws.wsr.close()
            except Exception:
                pass
        await self._on_ws_closed(ws)

    # =====

    async def _check_request_auth(self, exposed: HttpExposed, req: Request) -> None:
        pass

    async def _init_app(self) -> None:
        raise NotImplementedError

    async def _on_shutdown(self) -> None:
        pass

    async def _on_cleanup(self) -> None:
        pass

    async def _on_ws_opened(self, ws: WsSession) -> None:
        pass

    async def _on_ws_closed(self, ws: WsSession) -> None:
        pass

    # =====

    async def __make_app(self) -> Application:
        self.__app = Application(middlewares=[normalize_path_middleware(  # pylint: disable=attribute-defined-outside-init
            append_slash=False,
            remove_slash=True,
            merge_slashes=True,
        )])

        async def on_shutdown(_: Application) -> None:
            await self._on_shutdown()
        self.__app.on_shutdown.append(on_shutdown)

        async def on_cleanup(_: Application) -> None:
            await self._on_cleanup()
        self.__app.on_cleanup.append(on_cleanup)

        await self._init_app()
        return self.__app

    def __run_app_print(self, text: str) -> None:
        logger = get_logger(0)
        for line in text.strip().splitlines():
            logger.info(line.strip())
