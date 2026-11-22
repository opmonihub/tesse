"""Budgeted worker-side consumer for the isolated Ebinex history queue."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ebinex_sdk.exceptions import AuthenticationError, TransportError

from .ebinex_history import (
    PageResult,
    fetch_page,
    page_at,
    publish_snapshot,
    record_page,
    summarize_symbol,
)
from .market_history import SOURCE, HistoryQueue

LOG = logging.getLogger(__name__)


class _ReadBudgetLost(Exception):
    pass


class HistoryCollector:
    def __init__(
        self, root, *, timeout=4, min_interval=2, monotonic=time.monotonic, wall=time.time
    ):
        self.root = Path(root)
        self.queue = HistoryQueue(root)
        self.timeout = timeout
        self.min_interval = min_interval
        self.monotonic = monotonic
        self.wall = wall
        self.task = None
        self._next_call = 0.0
        self._inventory = None
        self._epoch = 0
        self._network = None
        self._pause_reason = "awaiting_session"
        self._fault = None

    @staticmethod
    def _has_session(client):
        status = client.readiness
        return all(
            getattr(status, key, False)
            for key in (
                "authenticated",
                "test_account_confirmed",
                "clock_ready",
            )
        )

    def offer(self, client, binding, can_start):
        """Offer one idle budget. The caller never awaits history work here."""
        if (
            not binding
            or (self.task is not None and not self.task.done())
            or self.monotonic() < self._next_call
            or not self._has_session(client)
            or not can_start()
        ):
            return
        self.task = asyncio.create_task(
            self._step(
                client,
                binding,
                client.readiness.generation,
                self._epoch,
                can_start,
            )
        )

    def _scope_matches(self, client, generation, epoch):
        return self._epoch == epoch and client.readiness.generation == generation

    async def pause(self, reason, *, drain_timeout=1):
        """Fence results before the owner closes or replaces its SDK client."""
        self._epoch += 1
        self._pause_reason = reason
        self._inventory = None
        if self._network is not None and not self._network.done():
            self._network.cancel()
        if self.task is not None and not self.task.done():
            # Do not cancel a thread holding a SQLite transaction. If a transport
            # stalls during cancellation, its obsolete result is still fenced.
            await asyncio.wait((self.task,), timeout=drain_timeout)

    async def _call(self, operation, admitted):
        async def run():
            if not admitted():
                raise _ReadBudgetLost
            self._next_call = self.monotonic() + self.min_interval
            return await operation()

        self._network = asyncio.create_task(run())
        try:
            return await self._network
        finally:
            self._network = None

    async def _native_symbols(self, market):
        async with asyncio.timeout(self.timeout):
            assets = await market.list_assets(mode=None)
        return frozenset(asset.symbol for asset in assets)

    def _report(self, claim):
        result = {}
        for symbol in claim.request.symbols:
            report = summarize_symbol(self.queue, claim.id, claim.request, symbol)
            if report["quality"] == "complete":
                rows = self.queue.read_rows(claim.id, symbol)
                report = publish_snapshot(self.root, claim.id, report, rows)
            result[symbol] = report
        return result

    def _note_failure(self, exc):
        # A research failure never pauses the order path. Do not log broker details.
        self._next_call = max(self._next_call, self.monotonic() + 10)
        category = type(exc).__name__
        if self._fault != category:
            LOG.warning("Falha na coleta Ebinex (%s); operação continua", category)
            self._fault = category

    async def _step(self, client, binding, generation, epoch, can_start):
        claim = None
        try:
            claim = await asyncio.to_thread(self.queue.claim, binding, now=self.wall())
            if claim is None:
                self._fault = None
                return

            async def defer(reason):
                try:
                    await asyncio.to_thread(self.queue.defer, claim, reason)
                except Exception as exc:
                    self._note_failure(exc)

            def same_session():
                return self._scope_matches(client, generation, epoch) and self._has_session(client)

            def admitted():
                return same_session() and can_start()

            if not admitted():
                await defer(
                    self._pause_reason
                    if self._epoch != epoch
                    else "worker_busy"
                    if same_session()
                    else "awaiting_session"
                )
                return
            page = page_at(claim.request, claim.cursor)
            if page is None:
                result = await asyncio.to_thread(self._report, claim)
                await asyncio.to_thread(self.queue.finish, claim, result)
                return
            key = (epoch, generation)
            if (
                self._inventory is None
                or self._inventory[0] != key
                or self.monotonic() >= self._inventory[1]
            ):
                try:
                    assets = await self._call(lambda: self._native_symbols(client.market), admitted)
                except AuthenticationError:
                    await defer("authentication_unavailable")
                except TimeoutError, TransportError:
                    failure = PageResult(
                        {},
                        dict(
                            source=SOURCE,
                            quality="inconclusive",
                            reason="inventory_unavailable",
                            received_minutes=0,
                        ),
                    )
                    if same_session():
                        await asyncio.to_thread(
                            record_page, self.queue, claim, page, failure, now=self.wall()
                        )
                    else:
                        await defer("awaiting_session")
                else:
                    if same_session():
                        self._inventory = key, self.monotonic() + 60, assets
                    await defer(
                        self._pause_reason
                        if self._epoch != epoch
                        else "awaiting_read_budget"
                        if same_session()
                        else "awaiting_session"
                    )
                return
            if page.symbol not in self._inventory[2]:
                result = PageResult(
                    {},
                    dict(
                        source=SOURCE,
                        quality="inconclusive",
                        reason="unknown_native_symbol",
                        received_minutes=0,
                    ),
                )
            else:
                result = await self._call(
                    lambda: fetch_page(client.market, page, timeout=self.timeout),
                    admitted,
                )
            if self._epoch != epoch:
                await defer(self._pause_reason)
            elif result.evidence.get("reason") == "authentication_unavailable":
                await defer("authentication_unavailable")
            elif not same_session():
                await defer("awaiting_session")
            else:
                await asyncio.to_thread(
                    record_page, self.queue, claim, page, result, now=self.wall()
                )
            self._fault = None
        except _ReadBudgetLost:
            reason = (
                self._pause_reason
                if self._epoch != epoch
                else "worker_busy"
                if same_session()
                else "awaiting_session"
            )
            await defer(reason)
        except asyncio.CancelledError:
            if self._epoch == epoch:
                raise
            if claim is not None:
                await defer(self._pause_reason)
        except Exception as exc:
            self._note_failure(exc)
            if claim is not None:
                try:
                    await asyncio.to_thread(
                        self.queue.defer, claim, "collector_error", retry_at=self.wall() + 10
                    )
                except Exception:
                    pass  # A broken/unavailable queue is not a trading failure.
