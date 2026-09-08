"""Narrow workaround for a reset during Windows asyncio socket teardown."""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import contextmanager

LOG = logging.getLogger("responses_proxy")


@contextmanager
def windows_connection_reset_guard():
    if sys.platform != "win32":
        yield
        return

    # Python 3.11's Proactor callback can fail at socket.shutdown(), after
    # notifying the protocol but before closing the socket/detaching the server.
    # Keep this private-API workaround isolated; newer implementations that
    # handle shutdown themselves never reach this exception handler.
    from asyncio.proactor_events import _ProactorBasePipeTransport

    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handle_exception(loop, context):
        error = context.get("exception")
        callback = getattr(context.get("handle"), "_callback", None)
        transport = getattr(callback, "__self__", None)
        if (isinstance(error, ConnectionResetError) and getattr(error, "winerror", None) == 10054
                and error.__context__ is None
                and getattr(callback, "__func__", None) is _ProactorBasePipeTransport._call_connection_lost
                and transport.is_closing() and not transport._called_connection_lost
                and transport._sock is not None):
            # Finish the cleanup skipped by the failed shutdown. Do not call
            # connection_lost twice, retry requests, or change provider health.
            transport._sock.close()
            transport._sock = None
            if transport._server is not None:
                transport._server._detach()
                transport._server = None
            transport._called_connection_lost = True
            LOG.info("event=connection_closed reason=windows_connection_reset")
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handle_exception)
    try:
        yield
    finally:
        if loop.get_exception_handler() is handle_exception:
            loop.set_exception_handler(previous)
