"""Reusable directional feature transformations."""

from collections.abc import Iterable, Sequence

import numpy as np

_VALID_NEUTRAL = {"zero", "preserve"}


def trailing_population_std(values, window: int) -> np.ndarray:
    """Return a causal, right-aligned float64 column; incomplete windows are NaN."""
    if type(window) is not int or window < 1:
        raise ValueError("window must be a positive integer")
    source = np.asarray(values, dtype=np.float64)
    if source.ndim != 1:
        raise ValueError("values must be one-dimensional")
    result = np.full(source.shape, np.nan)
    if len(source) >= window:
        trailing = np.lib.stride_tricks.sliding_window_view(source, window)
        with np.errstate(invalid="ignore", over="ignore"):
            result[window - 1 :] = trailing.std(axis=1, ddof=0)
    return result


def _matrix(values, name="values") -> np.ndarray:
    result = np.asarray(values, dtype=float)
    if result.ndim != 2:
        raise ValueError(f"{name} must be a two-dimensional array")
    if not np.isfinite(result).all():
        raise ValueError(f"{name} must contain only finite values")
    return result


def _side(side, rows: int) -> np.ndarray:
    result = np.asarray(side, dtype=int)
    if result.ndim != 1 or len(result) != rows:
        raise ValueError("side must be a one-dimensional array aligned with values")
    if not np.isin(result, (-1, 0, 1)).all():
        raise ValueError("side must contain only -1, 0 or 1")
    return result


def _indices(indices, width: int, name="columns") -> tuple[int, ...]:
    if isinstance(indices, np.ndarray) and indices.ndim == 0:
        indices = [indices.item()]
    if isinstance(indices, (str, bytes)) or not isinstance(indices, Iterable):
        raise ValueError(f"{name} must be an iterable of column indices")
    result = tuple(indices)
    if not result or any(type(index) is not int or not 0 <= index < width for index in result):
        raise ValueError(f"{name} contains an invalid column index")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} contains duplicate column indices")
    return result


def _neutralize(result: np.ndarray, side: np.ndarray, neutral: str) -> np.ndarray:
    if neutral not in _VALID_NEUTRAL:
        raise ValueError("neutral must be 'zero' or 'preserve'")
    if neutral == "zero":
        result[side == 0] = 0.0
    return result


def orient_columns(
    values,
    side,
    columns: Sequence[int] | None = None,
    *,
    indices: Sequence[int] | None = None,
    neutral="zero",
) -> np.ndarray:
    """Orient selected columns by side, preserving all other columns.

    The historical panel uses ``neutral='zero'``: selected values on a neutral
    row become zero, while unselected columns remain unchanged.
    """

    result = _matrix(values).copy()
    sides = _side(side, len(result))
    if columns is not None and indices is not None:
        raise ValueError("use either columns or indices, not both")
    selected_input = indices if indices is not None else columns
    selected = (tuple(range(result.shape[1])) if selected_input is None
                else _indices(selected_input, result.shape[1]))
    if neutral not in _VALID_NEUTRAL:
        raise ValueError("neutral must be 'zero' or 'preserve'")
    multiplier = sides if neutral == "zero" else np.where(sides == 0, 1, sides)
    result[:, selected] *= multiplier[:, None]
    return _neutralize(result, sides, neutral)


orient_directional_columns = orient_columns


def contrast_group(values, side, positive: Sequence[int], negative: Sequence[int], *, neutral="zero") -> np.ndarray:
    """Return an oriented sum(positive) - sum(negative) contrast."""

    matrix = _matrix(values)
    sides = _side(side, len(matrix))
    positive = _indices(positive, matrix.shape[1], "positive")
    negative = _indices(negative, matrix.shape[1], "negative")
    if set(positive) & set(negative):
        raise ValueError("positive and negative groups must be disjoint")
    result = matrix[:, positive].sum(axis=1) - matrix[:, negative].sum(axis=1)
    if neutral not in _VALID_NEUTRAL:
        raise ValueError("neutral must be 'zero' or 'preserve'")
    result = np.where(sides == 1, result, -result)
    if neutral == "preserve":
        raw = matrix[:, positive].sum(axis=1) - matrix[:, negative].sum(axis=1)
        result = np.where(sides == 0, raw, result)
    else:
        result = result.copy()
        result[sides == 0] = 0.0
    return result


def contrast_groups(
    values,
    side,
    groups=None,
    negative: Sequence[int] | None = None,
    *,
    positive: Sequence[int] | None = None,
    neutral="zero",
) -> np.ndarray:
    """Apply one or more oriented group contrasts.

    ``groups`` is an iterable of ``(positive_columns, negative_columns)``
    pairs.  The result is always shaped ``(rows, number_of_pairs)``.
    """

    if positive is not None:
        if groups is not None or negative is None:
            raise ValueError("positive and negative groups must be supplied together")
        groups = (positive, negative)
    elif negative is not None:
        if groups is None:
            raise ValueError("positive and negative groups must be supplied together")
        groups = (groups, negative)
    if isinstance(groups, (str, bytes)) or not isinstance(groups, Iterable):
        raise ValueError("groups must contain positive/negative column pairs")
    # A single (positive, negative) pair is accepted directly as a convenience.
    candidate = tuple(groups)
    if (len(candidate) == 2 and all(isinstance(item, (tuple, list)) for item in candidate)
            and all(all(type(index) is int for index in item) for item in candidate)):
        pairs = (candidate,)
    else:
        pairs = candidate
    if not pairs:
        raise ValueError("groups must not be empty")
    result = []
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError("each group must contain positive and negative columns")
        result.append(contrast_group(values, side, pair[0], pair[1], neutral=neutral))
    return np.column_stack(result)
