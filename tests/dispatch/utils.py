import ssl
import typing

import h2.config
import h2.connection
import h2.events

from httpx import Request, Timeout
from httpx._backends.base import BaseSocketStream, lookup_backend


class MockHTTP2Backend:
    def __init__(self, app):
        self.app = app
        self.backend = lookup_backend()
        self.server = None

    async def open_tcp_stream(
        self,
        hostname: str,
        port: int,
        ssl_context: typing.Optional[ssl.SSLContext],
        timeout: Timeout,
    ) -> BaseSocketStream:
        self.server = MockHTTP2Server(self.app, backend=self.backend)
        return self.server

    # Defer all other attributes and methods to the underlying backend.
    def __getattr__(self, name: str) -> typing.Any:
        return getattr(self.backend, name)


class MockHTTP2Server(BaseSocketStream):
    def __init__(self, app, backend: MockHTTP2Backend):
        config = h2.config.H2Configuration(client_side=False)
        self.conn = h2.connection.H2Connection(config=config)
        self.app = app
        self.backend = backend
        self.buffer = b""
        self.requests = {}
        self.close_connection = False
        self.return_data = {}
        self.settings_changed = []

    # Socket stream interface

    def get_http_version(self) -> str:
        return "HTTP/2"

    async def read(self, n, timeout, flag=None) -> bytes:
        send, self.buffer = self.buffer[:n], self.buffer[n:]
        return send

    async def write(self, data: bytes, timeout) -> None:
        if not data:
            return
        events = self.conn.receive_data(data)
        self.buffer += self.conn.data_to_send()
        for event in events:
            if isinstance(event, h2.events.RequestReceived):
                self.request_received(event.headers, event.stream_id)
            elif isinstance(event, h2.events.DataReceived):
                self.receive_data(event.data, event.stream_id)
                # This should send an UPDATE_WINDOW for both the stream and the
                # connection increasing it by the amount
                # consumed keeping the flow control window constant
                flow_control_consumed = event.flow_controlled_length
                if flow_control_consumed > 0:
                    self.conn.increment_flow_control_window(flow_control_consumed)
                    self.buffer += self.conn.data_to_send()
                    self.conn.increment_flow_control_window(
                        flow_control_consumed, event.stream_id
                    )
                    self.buffer += self.conn.data_to_send()
            elif isinstance(event, h2.events.StreamEnded):
                await self.stream_complete(event.stream_id)
            elif isinstance(event, h2.events.RemoteSettingsChanged):
                self.settings_changed.append(event)

    async def close(self) -> None:
        pass

    def is_connection_dropped(self) -> bool:
        return self.close_connection

    # Server implementation

    def request_received(self, headers, stream_id):
        """
        Handler for when the initial part of the HTTP request is received.
        """
        if stream_id not in self.requests:
            self.requests[stream_id] = []
        self.requests[stream_id].append({"headers": headers, "data": b""})

    def receive_data(self, data, stream_id):
        """
        Handler for when a data part of the HTTP request is received.
        """
        self.requests[stream_id][-1]["data"] += data

    async def stream_complete(self, stream_id):
        """
        Handler for when the HTTP request is completed.
        """
        request = self.requests[stream_id].pop(0)
        if not self.requests[stream_id]:
            del self.requests[stream_id]

        headers_dict = dict(request["headers"])

        method = headers_dict[b":method"].decode("ascii")
        url = "%s://%s%s" % (
            headers_dict[b":scheme"].decode("ascii"),
            headers_dict[b":authority"].decode("ascii"),
            headers_dict[b":path"].decode("ascii"),
        )
        headers = [(k, v) for k, v in request["headers"] if not k.startswith(b":")]
        data = request["data"]

        # Call out to the app.
        request = Request(method, url, headers=headers, data=data)
        response = await self.app(request)

        # Write the response to the buffer.
        status_code_bytes = str(response.status_code).encode("ascii")
        response_headers = [(b":status", status_code_bytes)] + response.headers.raw

        self.conn.send_headers(stream_id, response_headers)
        self.buffer += self.conn.data_to_send()
        self.return_data[stream_id] = response.content
        self.send_return_data(stream_id)

    def send_return_data(self, stream_id):
        while self.return_data[stream_id]:
            flow_control = self.conn.local_flow_control_window(stream_id)
            chunk_size = min(
                len(self.return_data[stream_id]),
                flow_control,
                self.conn.max_outbound_frame_size,
            )
            if chunk_size > 0:
                chunk, self.return_data[stream_id] = (
                    self.return_data[stream_id][:chunk_size],
                    self.return_data[stream_id][chunk_size:],
                )
                self.conn.send_data(stream_id, chunk)
                self.buffer += self.conn.data_to_send()
        self.conn.end_stream(stream_id)
        self.buffer += self.conn.data_to_send()


class MockRawSocketBackend:
    def __init__(self, data_to_send=b""):
        self.backend = lookup_backend()
        self.data_to_send = data_to_send
        self.received_data = []
        self.stream = MockRawSocketStream(self)

    async def open_tcp_stream(
        self,
        hostname: str,
        port: int,
        ssl_context: typing.Optional[ssl.SSLContext],
        timeout: Timeout,
    ) -> BaseSocketStream:
        self.received_data.append(
            b"--- CONNECT(%s, %d) ---" % (hostname.encode(), port)
        )
        return self.stream

    # Defer all other attributes and methods to the underlying backend.
    def __getattr__(self, name: str) -> typing.Any:
        return getattr(self.backend, name)


class MockRawSocketStream(BaseSocketStream):
    def __init__(self, backend: MockRawSocketBackend):
        self.backend = backend

    async def start_tls(
        self, hostname: str, ssl_context: ssl.SSLContext, timeout: Timeout
    ) -> BaseSocketStream:
        self.backend.received_data.append(b"--- START_TLS(%s) ---" % hostname.encode())
        return MockRawSocketStream(self.backend)

    def get_http_version(self) -> str:
        return "HTTP/1.1"

    async def write(self, data: bytes, timeout) -> None:
        if not data:
            return
        self.backend.received_data.append(data)

    async def read(self, n, timeout, flag=None) -> bytes:
        if not self.backend.data_to_send:
            return b""
        return self.backend.data_to_send.pop(0)

    def is_connection_dropped(self) -> bool:
        return False

    async def close(self) -> None:
        pass
