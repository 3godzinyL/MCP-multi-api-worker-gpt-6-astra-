import asyncio
import logging
import socket
import sys
from unittest.mock import Mock

import pytest

from proxy.server import create_app
from proxy.windows_transport import windows_connection_reset_guard
from tests.test_proxy import KEYS, TOKEN, settings

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Proactor workaround")


@pytest.mark.parametrize("winerror", [10054, 10053])
async def test_proactor_shutdown_reset_finishes_cleanup_and_preserves_other_errors(winerror, caplog):
    from asyncio.proactor_events import _ProactorBasePipeTransport

    loop = asyncio.get_running_loop()
    original = loop.get_exception_handler()
    previous = Mock()
    loop.set_exception_handler(previous)
    sock = Mock(spec=socket.socket)
    sock.fileno.return_value = 123
    sock.shutdown.side_effect = ConnectionResetError(0, "PRIVATE_SOCKET_DETAIL", None, winerror)
    protocol, server = Mock(spec=asyncio.Protocol), Mock()
    transport = _ProactorBasePipeTransport(loop, sock, protocol, server=server)
    try:
        with caplog.at_level(logging.INFO, logger="responses_proxy"), windows_connection_reset_guard():
            transport.close()
            await asyncio.sleep(0)
        assert loop.get_exception_handler() is previous
        protocol.connection_lost.assert_called_once_with(None)
        sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        if winerror == 10054:
            previous.assert_not_called()
            sock.close.assert_called_once()
            server._detach.assert_called_once()
            assert transport._sock is None
            assert transport._called_connection_lost
            assert "event=connection_closed reason=windows_connection_reset" in caplog.text
        else:
            previous.assert_called_once()
            assert previous.call_args.args[1]["exception"].winerror == winerror
            sock.close.assert_not_called()
            server._detach.assert_not_called()
            assert "event=connection_closed" not in caplog.text
        assert "PRIVATE_SOCKET_DETAIL" not in caplog.text
    finally:
        loop.set_exception_handler(original)
        # The nonmatching error intentionally uses the original handler, which
        # only records it. Clean up this fake socket without rerunning callbacks.
        if transport._sock is not None:
            sock.close()
            transport._sock = None
            server._detach()
            transport._server = None


@pytest.mark.parametrize("use_previous", [False, True])
async def test_unrelated_callback_reset_is_not_hidden(use_previous, monkeypatch):
    loop = asyncio.get_running_loop()
    original = loop.get_exception_handler()
    previous, default = Mock(), Mock()
    monkeypatch.setattr(loop, "default_exception_handler", default)
    loop.set_exception_handler(previous if use_previous else None)
    error = ConnectionResetError(0, "unrelated callback reset", None, 10054)

    def unrelated_callback():
        raise error

    try:
        with windows_connection_reset_guard():
            loop.call_soon(unrelated_callback)
            await asyncio.sleep(0)
        if use_previous:
            previous.assert_called_once()
            assert previous.call_args.args[0] is loop
            assert previous.call_args.args[1]["exception"] is error
            default.assert_not_called()
        else:
            default.assert_called_once()
            assert default.call_args.args[0]["exception"] is error
            previous.assert_not_called()
        assert loop.get_exception_handler() is (previous if use_previous else None)
    finally:
        loop.set_exception_handler(original)


@pytest.mark.parametrize("shutdown_also_fails", [False, True])
async def test_protocol_errors_in_same_callback_are_not_hidden(shutdown_also_fails):
    from asyncio.proactor_events import _ProactorBasePipeTransport

    loop = asyncio.get_running_loop()
    original = loop.get_exception_handler()
    previous = Mock()
    loop.set_exception_handler(previous)
    sock = Mock(spec=socket.socket)
    sock.fileno.return_value = 123
    if shutdown_also_fails:
        sock.shutdown.side_effect = ConnectionResetError(0, "shutdown reset", None, 10054)
    protocol = Mock(spec=asyncio.Protocol)
    protocol_error = ConnectionResetError(0, "protocol error", None, 10054)
    protocol.connection_lost.side_effect = protocol_error
    transport = _ProactorBasePipeTransport(loop, sock, protocol)
    try:
        with windows_connection_reset_guard():
            transport.close()
            await asyncio.sleep(0)
        previous.assert_called_once()
        error = previous.call_args.args[1]["exception"]
        assert (error.__context__ if shutdown_also_fails else error) is protocol_error
    finally:
        loop.set_exception_handler(original)
        if transport._sock is not None:
            sock.close()
            transport._sock = None


async def test_app_installs_and_restores_exception_handler():
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    app = create_app(settings(), secrets=KEYS, token=TOKEN)
    async with app.router.lifespan_context(app):
        assert loop.get_exception_handler() is not previous
    assert loop.get_exception_handler() is previous
