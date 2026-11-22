"""Read-only TEST market history; SDK pagination follows the UTC grid, not the response."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from ebinex_sdk.exceptions import AuthenticationError, TransportError, UnsupportedMarketError
from ebinex_sdk.services.market import CandleGapError

from .market_history import DAY, SOURCE, Request

M1 = 60
PAGE_SLOTS = 498
PROBE_AGES = (1, 7, 30, 90)


def utc(second):
    return datetime.fromtimestamp(second, UTC)


@dataclass(frozen=True, slots=True)
class Page:
    symbol: str
    start: int
    end: int
    horizon: int | None = None

    @property
    def expected(self):
        return (self.end - self.start) // M1


@dataclass(frozen=True, slots=True)
class PageResult:
    rows: dict[int, tuple[str, ...]]
    evidence: dict


def page_at(request: Request, cursor: int) -> Page | None:
    if request.kind == "probe":
        symbol_idx, window_idx = divmod(cursor, 2 * len(PROBE_AGES))
        if symbol_idx >= len(request.symbols):
            return None
        age = PROBE_AGES[window_idx // 2]
        start = request.end - age * DAY + (window_idx % 2) * DAY // 2
        return Page(request.symbols[symbol_idx], start, start + 3600, age)
    span = M1 * PAGE_SLOTS
    n_pages = (request.end - request.start + span - 1) // span
    symbol_idx, window_idx = divmod(cursor, n_pages)
    if symbol_idx >= len(request.symbols):
        return None
    start = request.start + window_idx * span
    return Page(request.symbols[symbol_idx], start, min(start + span, request.end))


def _rows(candles, page):
    if len(candles) > page.expected + 2:
        raise ValueError("response exceeds page")
    result, conflicts = {}, set()
    identical = 0
    for bar in candles:
        if bar.symbol != page.symbol or bar.timeframe != "M1":
            raise ValueError("unexpected candle identity")
        t = bar.timestamp
        if (
            t.utcoffset() is None
            or t.microsecond
            or t.second
            or not (page.start - M1 <= t.timestamp() <= page.end and t.timestamp() % M1 == 0)
        ):
            raise ValueError("unexpected candle timestamp")
        values = bar.open, bar.high, bar.low, bar.close, bar.volume
        if any(not isinstance(v, Decimal) or not v.is_finite() for v in values):
            raise ValueError("invalid candle number")
        o, h, low, c, v = values
        if min(o, h, low, c) <= 0 or v < 0 or low > min(o, c) or h < max(o, c):
            raise ValueError("invalid candle OHLCV")
        minute = int(t.timestamp())
        if minute in result:
            if tuple(map(Decimal, result[minute])) != values:
                conflicts.add(minute)
            else:
                identical += 1
        else:
            result[minute] = tuple(map(str, values))
    return result, conflicts, identical


async def fetch_page(market, page: Page, *, timeout: float) -> PageResult:
    evidence = dict(
        source=SOURCE,
        start=page.start,
        end=page.end,
        horizon_days=page.horizon,
        expected_minutes=page.expected,
        received_minutes=0,
        quality="inconclusive",
    )
    try:
        async with asyncio.timeout(timeout):
            try:
                candles = await market.get_candles(
                    page.symbol,
                    "M1",
                    start=utc(page.start - M1),
                    end=utc(page.end),
                    limit=page.expected + 2,
                    mode="OPTION",
                )
            except CandleGapError as exc:
                candles = exc.candles
    except TimeoutError:
        return PageResult({}, dict(evidence, reason="timeout"))
    except AuthenticationError:
        return PageResult({}, dict(evidence, reason="authentication_unavailable"))
    except UnsupportedMarketError:
        return PageResult({}, dict(evidence, reason="unsupported_market"))
    except TransportError:
        return PageResult({}, dict(evidence, reason="transport_error"))
    try:
        rows, conflicts, identical = _rows(candles, page)
    except AttributeError, OverflowError, TypeError, ValueError:
        return PageResult({}, dict(evidence, reason="invalid_candles"))
    minutes = sum(page.start <= t < page.end for t in rows)
    quality = "complete" if minutes == page.expected else "partial" if minutes else "no_data"
    evidence.update(received_minutes=minutes, quality=quality, identical_in_response=identical)
    if conflicts:
        evidence.update(
            quality="inconclusive", reason="conflicting_candles", conflicts=sorted(conflicts)
        )
    return PageResult(rows, evidence)


def retry_at(evidence, *, attempts, now):
    if attempts < 2 and (
        evidence["quality"] in ("partial", "no_data")
        or evidence.get("reason") in ("timeout", "transport_error", "inventory_unavailable")
    ):
        return now + 2 * (2**attempts)
    return None


def record_page(queue, claim, page, result, *, now):
    evidence, rows = result.evidence, result.rows
    if evidence.get("source") != SOURCE:
        evidence, rows = (
            dict(source=SOURCE, quality="inconclusive", reason="wrong_source", received_minutes=0),
            {},
        )
    evidence = dict(
        evidence,
        start=page.start,
        end=page.end,
        horizon_days=page.horizon,
        expected_minutes=page.expected,
    )
    # Adjacent pages retain their guards for conflict detection, except outside the job.
    if claim.request.kind == "sync":
        lo, hi = claim.request.start, claim.request.end
    else:
        lo, hi = page.start, page.end
    rows = {t: bar for t, bar in rows.items() if lo <= t < hi}
    return queue.record(
        claim,
        page.symbol,
        rows,
        evidence,
        retry_at=retry_at(evidence, attempts=claim.attempts, now=now),
    )


def _intervals(minutes):
    result = []
    for t in minutes:
        if result and result[-1][1] == t:
            result[-1][1] = t + M1
        else:
            result.append([t, t + M1])
    return result


def summarize_symbol(queue, job_id, request, symbol):
    evidence = {(d["start"], d["end"]): d for d in queue.evidence(job_id) if d["symbol"] == symbol}
    seen = {t for t, _ in queue.read_rows(job_id, symbol)}
    index = request.symbols.index(symbol)
    span = PAGE_SLOTS * M1
    page_count = (
        2 * len(PROBE_AGES)
        if request.kind == "probe"
        else (request.end - request.start + span - 1) // span
    )
    pages = [page_at(request, n) for n in range(index * page_count, (index + 1) * page_count)]
    checked = []
    for page in pages:
        detail = evidence.get(
            (page.start, page.end),
            dict(
                start=page.start,
                end=page.end,
                horizon_days=page.horizon,
                expected_minutes=page.expected,
                received_minutes=0,
                quality="inconclusive",
                reason="not_verified",
            ),
        )
        grid = range(page.start, page.end, M1)
        if detail["quality"] == "inconclusive":
            checked.append(
                dict(
                    detail,
                    gaps=[],
                    unverified=[[page.start, page.end]],
                    missing_minutes=0,
                    unverified_minutes=page.expected,
                )
            )
        else:
            gaps = _intervals(t for t in grid if t not in seen)
            missing = sum((end - start) // M1 for start, end in gaps)
            checked.append(
                dict(
                    detail,
                    gaps=gaps,
                    unverified=[],
                    missing_minutes=missing,
                    unverified_minutes=0,
                    received_minutes=page.expected - missing,
                    quality=(
                        "complete"
                        if not missing
                        else "no_data"
                        if missing == page.expected
                        else "partial"
                    ),
                )
            )
    report = dict(symbol=symbol, source=SOURCE, research_ready=False)
    if request.kind == "probe":
        return dict(report, quality="sampled", samples=checked)
    gaps = _intervals(t for part in checked for a, b in part["gaps"] for t in range(a, b, M1))
    unverified = _intervals(
        t for part in checked for a, b in part["unverified"] for t in range(a, b, M1)
    )
    conflicts = sorted({t for part in checked for t in part.get("conflicts", ())})
    quality = (
        "inconclusive"
        if unverified or conflicts
        else "no_data"
        if not seen
        else "partial"
        if gaps
        else "complete"
    )
    return dict(
        report,
        quality=quality,
        start=request.start,
        end=request.end,
        expected_minutes=(request.end - request.start) // M1,
        received_minutes=len(seen),
        missing_minutes=sum(p["missing_minutes"] for p in checked),
        unverified_minutes=sum(p["unverified_minutes"] for p in checked),
        first=min(seen, default=None),
        last=max(seen, default=None),
        gaps=gaps,
        unverified=unverified,
        conflicts=conflicts,
        identical_overlaps=sum(p.get("identical_overlap", 0) for p in checked),
        identical_in_responses=sum(p.get("identical_in_response", 0) for p in checked),
        reasons=sorted({p["reason"] for p in checked if p.get("reason")}),
        pages=checked,
    )


def _publish_directory(staged, target):
    # Published destinations are nonempty, so a competing rename cannot overwrite them.
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    staged.rename(target)


def _sync_directory(path):
    import os

    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish_snapshot(root, job_id, report, rows):
    import hashlib
    import json
    import os
    import re
    from importlib.metadata import version
    from pathlib import Path
    from tempfile import TemporaryDirectory

    import numpy as np

    from . import __version__
    from .data import DTYPE
    from .forge.snapshots import load_snapshot

    report = dict(report, research_ready=False)
    if report["quality"] != "complete" or report["source"] != SOURCE:
        return report
    symbol, start, end = (report[k] for k in ("symbol", "start", "end"))
    if not re.fullmatch(r"[A-Z0-9]{1,32}", symbol) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("Identidade do snapshot Ebinex inválida")
    root = Path(root).resolve()
    base = Path("market-data") / "ebinex" / "snapshots" / symbol
    parent = root / base
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = parent / job_id
    relative = base / job_id / "candles.json"
    with TemporaryDirectory(prefix=".snapshot-", dir=parent) as temporary:
        staging = Path(temporary)
        with (staging / "candles.npy").open("wb") as stream:
            bars = np.array([(t, *map(float, values)) for t, values in rows], dtype=DTYPE)
            np.save(stream, bars, allow_pickle=False)
            stream.flush()
            os.fsync(stream.fileno())
        with (staging / "candles.npy").open("rb") as stream:
            data_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = dict(
            symbol=symbol,
            source=SOURCE,
            environment="TEST",
            mode="OPTION",
            timeframe="M1",
            start=utc(start).isoformat(),
            end=utc(end).isoformat(),
            rows=len(rows),
            expected=(end - start) // M1,
            missing=0,
            gaps=0,
            edge_complete=True,
            collector_version=__version__,
            sdk_version=version("ebinex-sdk"),
            sha256=data_hash,
        )
        raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        with (staging / "candles.json").open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        identity = dict(
            manifest=relative.as_posix(),
            manifest_sha256=hashlib.sha256(raw).hexdigest(),
            sha256=data_hash,
        )
        load_snapshot(staging, dict(identity, manifest="candles.json"))
        _sync_directory(staging)
        try:
            _publish_directory(staging, destination)
            _sync_directory(parent)
        except FileExistsError:
            if (
                destination.is_symlink()
                or (destination / "candles.json").is_symlink()
                or (destination / "candles.npy").is_symlink()
            ):
                raise ValueError("Snapshot Ebinex existente divergente") from None
            try:
                load_snapshot(root, identity)
            except OSError, ValueError:
                raise ValueError("Snapshot Ebinex existente divergente") from None
    return dict(report, snapshot=identity, research_ready=end - start >= 90 * DAY)
