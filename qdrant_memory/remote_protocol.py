"""Bound HTTP header assembly before the authenticated ASGI gateway runs."""

from uvicorn.protocols.http.h11_impl import H11Protocol


class HeaderTimeoutH11Protocol(H11Protocol):
    """Expire idle or partial headers without timing out an active MCP request."""

    header_timeout_seconds = 10
    _header_timer = None

    def _cancel_header_timer(self):
        if self._header_timer is not None:
            self._header_timer.cancel()
            self._header_timer = None

    def _arm_header_timer(self):
        self._cancel_header_timer()
        if not self.transport.is_closing():
            self._header_timer = self.loop.call_later(
                self.header_timeout_seconds, self.transport.close
            )

    def connection_made(self, transport):
        """Start one absolute deadline as soon as the connection is accepted."""
        super().connection_made(transport)
        self._arm_header_timer()

    def handle_events(self):
        """Cancel the deadline only when complete headers create a request cycle."""
        previous = self.cycle
        super().handle_events()
        if self.cycle is not previous or self.transport.is_closing():
            self._cancel_header_timer()

    def on_response_complete(self):
        """Bound the next request, including partial headers on keep-alive sockets."""
        # Arm before the base callback processes any pipelined request. That
        # request's handle_events call must be able to cancel the new timer.
        self._arm_header_timer()
        super().on_response_complete()

    def connection_lost(self, exc):
        """Release the timer when the connection closes or changes protocol."""
        self._cancel_header_timer()
        super().connection_lost(exc)
