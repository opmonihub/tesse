"""Observa provas públicas do transporte SDK sem alterar seu protocolo ou envio."""

import json
import math
import time
from datetime import UTC, datetime, timedelta

from ebinex_sdk.protocols.sockjs import SockJSDecoder
from ebinex_sdk.protocols.stomp import StompDecoder, StompFrame


class BrokerTime:
    def __init__(self):
        self.reset()

    def reset(self):
        self.pending = None
        self.anchor = None
        self.sockjs = SockJSDecoder()
        self.stomp = StompDecoder()

    def sent(self, message):
        # Client-to-server SockJS is a JSON array, without the server 'a' prefix.
        try:
            for text in json.loads(message):
                for frame in StompDecoder().feed(text):
                    if (
                        isinstance(frame, StompFrame)
                        and frame.command == "SEND"
                        and frame.headers.get("destination") == "/user/topic/order-ready"
                    ):
                        self.pending = time.monotonic()
        except (ValueError, TypeError):
            self.anchor = None

    def received(self, message):
        try:
            for packet in self.sockjs.feed(message):
                for text in packet.messages:
                    for frame in self.stomp.feed(text):
                        if (
                            not isinstance(frame, StompFrame)
                            or frame.command != "MESSAGE"
                            or frame.headers.get("destination") != "/user/topic/order-ready"
                        ):
                            continue
                        value = json.loads(frame.body).get("serverTime")
                        received = time.monotonic()
                        if (
                            self.pending is None
                            or isinstance(value, bool)
                            or not isinstance(value, (int, float))
                        ):
                            continue
                        rtt = received - self.pending
                        self.pending = None
                        if not 0 <= rtt <= 1 or not math.isfinite(value):
                            self.anchor = None
                            continue
                        server = datetime.fromtimestamp(value / 1000, UTC)
                        self.anchor = (server, received, rtt)
        except (ValueError, TypeError, OverflowError):
            self.anchor = None

    def bounds(self):
        if self.anchor is None:
            raise ValueError("Relógio da corretora indisponível")
        server, received, rtt = self.anchor
        age = time.monotonic() - received
        if not 0 <= age < 10:
            raise ValueError("Prova de relógio expirada; aguarde nova sincronização SDK")
        return server + timedelta(seconds=age), server + timedelta(seconds=age + rtt)


class ObservedWebSocket:
    def __init__(self, native, clock):
        self.native, self.clock = native, clock

    async def connect(self, token):
        self.clock.reset()
        await self.native.connect(token)

    async def send(self, message):
        self.clock.sent(message)
        await self.native.send(message)

    async def send_checked(self, message, check):
        # Crucially retain SDK's deadline check under the native socket send lock.
        def observed_check():
            check()
            self.clock.sent(message)

        await self.native.send_checked(message, observed_check)

    async def receive(self):
        message = await self.native.receive()
        self.clock.received(message)
        return message

    async def close(self):
        self.clock.reset()
        await self.native.close()
