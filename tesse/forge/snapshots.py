"""Load one pinned M1 snapshot, never the latest available cache entry."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from re import fullmatch

import numpy as np

from ..data import DTYPE
from .contracts import Invalid, Lineage, Window
from .validation import MAX_YAML, parse_utc, read_limited


@dataclass(frozen=True)
class Snapshot:
    bars: np.ndarray
    asset: str
    source: str
    span: Window


@dataclass(frozen=True)
class SnapshotManifest:
    lineage: Lineage
    span: Window
    rows: int


def _inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise Invalid("snapshot", f"arquivo local ausente ou fora da raiz: {relative}")
    return path


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Invalid("snapshot.manifest", f"chave duplicada: {key}")
        result[key] = value
    return result


def read_snapshot_manifest(root, identity):
    """Verify pinned metadata without resolving or opening the candle payload."""
    root = Path(root).resolve()
    relative = Path(identity["manifest"])
    manifest = _inside(root, relative)
    raw = read_limited(manifest)
    if len(raw) > MAX_YAML:
        raise Invalid("snapshot.manifest", "manifesto excede 256 KiB")
    if hashlib.sha256(raw).hexdigest() != identity["manifest_sha256"]:
        raise Invalid("snapshot.manifest_sha256", "hash do manifesto não confere")
    try:
        meta = json.loads(raw, object_pairs_hook=_unique)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise Invalid("snapshot.manifest", "JSON inválido") from exc
    if not isinstance(meta, dict) or meta.get("sha256") != identity["sha256"]:
        raise Invalid("snapshot.sha256", "o manifesto não declara o hash dos candles configurado")
    asset, source = meta.get("symbol"), meta.get("source")
    if (not isinstance(asset, str) or not fullmatch(r"[A-Z0-9]{1,32}", asset)
            or not isinstance(source, str) or not source.strip() or meta.get("timeframe", "M1") != "M1"):
        raise Invalid("snapshot.manifest", "esperados símbolo, fonte e série M1")
    begin, end = (parse_utc(meta.get(key), f"snapshot.manifest.{key}") for key in ("start", "end"))
    count = int((end - begin).total_seconds() // 60)
    rows = meta.get("rows")
    if count <= 0 or type(rows) is not int or rows != count:
        raise Invalid("snapshot.manifest.rows", "snapshot M1 incompleto para o intervalo declarado")

    return SnapshotManifest(Lineage(source, asset), Window(begin, end), rows)


def load_snapshot(root, identity, *, manifest=None):
    root = Path(root).resolve()
    if manifest is None:
        manifest = read_snapshot_manifest(root, identity)
    begin, end = manifest.span.start, manifest.span.end
    rows = manifest.rows
    payload = _inside(root, Path(identity["manifest"]).with_suffix(".npy"))
    with payload.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != identity["sha256"]:
            raise Invalid("snapshot.sha256", "hash dos candles não confere")
        stream.seek(0)
        bars = np.load(stream, allow_pickle=False)
    if not isinstance(bars, np.ndarray) or bars.shape != (rows,) or bars.dtype != DTYPE:
        if isinstance(bars, np.lib.npyio.NpzFile):
            bars.close()
        raise Invalid("snapshot", "esperada matriz NPY M1 com schema nativo do Tesse")
    times = bars["t"]
    if (times[0] != int(begin.timestamp()) or times[-1] + 60 != int(end.timestamp())
            or np.any(np.diff(times) != 60)):
        raise Invalid("snapshot", "timestamps M1 incompletos, duplicados ou fora de ordem")
    if (any(not np.isfinite(bars[key]).all() for key in ("o", "h", "l", "c", "v"))
            or np.any(bars["l"] <= 0) or np.any(bars["v"] < 0)
            or np.any(bars["l"] > np.minimum(bars["o"], bars["c"]))
            or np.any(bars["h"] < np.maximum(bars["o"], bars["c"]))):
        raise Invalid("snapshot", "candles OHLCV inválidos")
    bars.flags.writeable = False
    return Snapshot(bars, manifest.lineage.symbol, manifest.lineage.source, manifest.span)
