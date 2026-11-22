"""Arquivos públicos Binance Spot. Intervalos sempre [início, fim), UTC."""

import calendar
import csv
import hashlib
import io
import json
import struct
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

from .config import SYMBOLS, atomic_write
from .core import Candle, utcnow
from .indicators.base import valid_volume

BASE = "https://data.binance.vision/data/spot"
DTYPE = np.dtype(
    [("t", "<i8"), ("o", "<f8"), ("h", "<f8"), ("l", "<f8"), ("c", "<f8"), ("v", "<f8")]
)


def verify_checksum(blob: bytes, text: str, filename: str):
    fields = text.strip().split()
    if len(fields) != 2 or fields[1].lstrip("*") != filename:
        raise ValueError("CHECKSUM malformado ou nome divergente")
    if hashlib.sha256(blob).hexdigest().lower() != fields[0].lower():
        raise ValueError("SHA256 divergente; arquivo não importado")


def normalize_timestamp(value: str):
    raw = int(value)
    # Arquivos Spot a partir de 2025 usam microssegundos; antigos usam ms.
    milliseconds = raw // 1000 if raw >= 100_000_000_000_000 else raw
    if milliseconds % 60000:
        raise ValueError("Timestamp M1 desalinhado")
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


def parse_archive(blob: bytes):
    candles = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()
        if len(names) != 1 or not names[0].endswith(".csv"):
            raise ValueError("ZIP deve conter um único CSV")
        if archive.getinfo(names[0]).file_size > 100_000_000:
            raise ValueError("CSV acima do tamanho máximo")
        with archive.open(names[0]) as source:
            for row in csv.reader(io.TextIOWrapper(source, encoding="utf-8")):
                if len(row) != 12:
                    raise ValueError("Schema Binance inesperado")
                ts = normalize_timestamp(row[0])
                c = Candle(ts, *(float(v) for v in row[1:6]))
                raw_open, raw_close = int(row[0]), int(row[6])
                expected = 59_999_999 if raw_open >= 100_000_000_000_000 else 59_999
                if raw_close - raw_open != expected:
                    raise ValueError("Fechamento de candle fora de M1")
                if ts in candles and candles[ts] != c:
                    raise ValueError("Duplicata conflitante")
                candles[ts] = c
    result = sorted(candles.values(), key=lambda c: c.timestamp)
    if not result:
        raise ValueError("Arquivo vazio")
    return result


def shift_month(ts, delta):
    month = ts.year * 12 + ts.month - 1 + delta
    year, month0 = divmod(month, 12)
    return ts.replace(
        year=year, month=month0 + 1, day=min(ts.day, calendar.monthrange(year, month0 + 1)[1])
    )


def default_range():
    end = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return shift_month(end, -12), end


def candle_digest(candles):
    """Hash dos dados selecionados, independente do arquivo que os contém."""
    digest = hashlib.sha256()
    for c in candles:
        if not valid_volume(c.volume):
            raise ValueError("candle_digest requires valid volume")
        digest.update(
            struct.pack(
                "<qddddd", int(c.timestamp.timestamp()), c.open, c.high, c.low, c.close, c.volume
            )
        )
    return digest.hexdigest()


def cached_range(root, symbols):
    """Último recorte anual comum disponível; publicações tardias não mudam um estudo."""
    common = None
    for symbol in symbols:
        ranges = set()
        for path in (root / "candles" / symbol).glob("*.json"):
            meta = json.loads(path.read_text(encoding="utf-8"))
            start, end = datetime.fromisoformat(meta["start"]), datetime.fromisoformat(meta["end"])
            if shift_month(end, -12) == start and meta["edge_complete"]:
                ranges.add((start, end))
        common = ranges if common is None else common & ranges
    return max(common, key=lambda item: item[1]) if common else default_range()


def archive_jobs(symbol, start, end):
    if symbol not in SYMBOLS or start >= end or start.hour or end.hour:
        raise ValueError("Ativo/intervalo inválido; use dias UTC completos")
    jobs = []
    current = start
    while current < end:
        next_month = shift_month(current.replace(day=1), 1)
        if current.day == 1 and next_month <= end:
            jobs.append(("monthly", current.strftime("%Y-%m"), symbol))
            current = next_month
        else:
            jobs.append(("daily", current.strftime("%Y-%m-%d"), symbol))
            current += timedelta(days=1)
    return jobs


def _request(client, url, allow_missing=False):
    for attempt in range(4):
        try:
            response = client.get(url)
            if response.status_code == 404 and allow_missing:
                return None
            response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == 3:
                raise
            time.sleep(min(2**attempt, 4))


def _fetch_job(root, client, job):
    kind, date, symbol = job
    name = f"{symbol}-1m-{date}.zip"
    directory = root / "archives" / symbol
    directory.mkdir(parents=True, exist_ok=True)
    cached = directory / name
    checksum_path = directory / (name + ".CHECKSUM")
    url = f"{BASE}/{kind}/klines/{symbol}/1m/{name}"
    # Reconsultar checksum permite detectar revisões públicas dos arquivos.
    response = _request(client, url + ".CHECKSUM", allow_missing=True)
    if response is None:
        if kind == "monthly":
            start = datetime.strptime(date, "%Y-%m").replace(tzinfo=UTC)
            return sum(
                (
                    _fetch_job(root, client, ("daily", d.strftime("%Y-%m-%d"), symbol))
                    for d in (
                        start + timedelta(days=i)
                        for i in range(calendar.monthrange(start.year, start.month)[1])
                    )
                ),
                [],
            )
        raise ValueError(f"Arquivo ainda indisponível: {symbol} {date}; tente novamente mais tarde")
    checksum = response.text
    blob = cached.read_bytes() if cached.exists() else None
    try:
        verify_checksum(blob or b"", checksum, name)
    except ValueError:
        blob = _request(client, url).content
        verify_checksum(blob, checksum, name)
        tmp = cached.with_suffix(".download")
        tmp.write_bytes(blob)
        tmp.replace(cached)
    checksum_path.write_text(checksum, encoding="utf-8")
    rows = parse_archive(blob)
    start = datetime.strptime(date, "%Y-%m" if kind == "monthly" else "%Y-%m-%d").replace(
        tzinfo=UTC
    )
    end = shift_month(start, 1) if kind == "monthly" else start + timedelta(days=1)
    if rows[0].timestamp < start or rows[-1].timestamp >= end:
        raise ValueError("Arquivo contém candles fora do intervalo anunciado")
    return rows


def sync_symbol(root: Path, symbol: str, start: datetime, end: datetime, progress=print):
    jobs = archive_jobs(symbol, start, end)
    unique = {}
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for index, job in enumerate(jobs, 1):
            progress(f"{symbol}: arquivo {index}/{len(jobs)} {job[1]}")
            for candle in _fetch_job(root, client, job):
                if candle.timestamp in unique and unique[candle.timestamp] != candle:
                    raise ValueError("Duplicata conflitante entre arquivos")
                unique[candle.timestamp] = candle
    rows = sorted(unique.values(), key=lambda c: c.timestamp)
    array = np.array(
        [(int(c.timestamp.timestamp()), c.open, c.high, c.low, c.close, c.volume) for c in rows],
        dtype=DTYPE,
    )
    dest = root / "candles" / symbol
    dest.mkdir(parents=True, exist_ok=True)
    label = f"{start.date()}_{end.date()}"
    output = dest / (label + ".npy")
    tmp = output.with_suffix(".tmp")
    with tmp.open("wb") as stream:
        np.save(stream, array, allow_pickle=False)
        stream.flush()
        import os

        os.fsync(stream.fileno())
    tmp.replace(output)
    gaps = int(np.count_nonzero(np.diff(array["t"]) != 60))
    expected = int((end - start).total_seconds() // 60)
    manifest = dict(
        symbol=symbol,
        start=start.isoformat(),
        end=end.isoformat(),
        rows=len(array),
        expected=expected,
        missing=expected - len(array),
        gaps=gaps,
        edge_complete=bool(
            len(array)
            and array["t"][0] == start.timestamp()
            and array["t"][-1] == end.timestamp() - 60
        ),
        sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        source=BASE,
        fetched=utcnow().isoformat(),
    )
    atomic_write(output.with_suffix(".json"), json.dumps(manifest, indent=2))
    return manifest


def load_candles(root: Path, symbol, start=None, end=None):
    directory = root / "candles" / symbol
    candidates = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    chosen = None
    for path in candidates:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if (start is None or datetime.fromisoformat(meta["start"]) <= start) and (
            end is None or datetime.fromisoformat(meta["end"]) >= end
        ):
            chosen = path
            break
    if chosen is None:
        raise ValueError(f"Sem dados cobrindo o intervalo de {symbol}; execute data sync")
    source = chosen.with_suffix(".npy")
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != meta["sha256"]:
        raise ValueError("Cache alterado/corrompido; execute data sync para reconstruir")
    data = np.load(source, mmap_mode="r", allow_pickle=False)
    mask = np.ones(len(data), dtype=bool)
    if start:
        mask &= data["t"] >= start.timestamp()
    if end:
        mask &= data["t"] < end.timestamp()
    rows = [
        Candle(
            datetime.fromtimestamp(int(r["t"]), UTC),
            *(float(r[k]) for k in ("o", "h", "l", "c", "v")),
        )
        for r in data[mask]
    ]
    return rows, meta
