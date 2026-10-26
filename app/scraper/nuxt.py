"""Decoder for Nuxt 3 `__NUXT_DATA__` payloads.

Nuxt serialises its SSR state with `devalue`: a flat JSON array where index 0 is
the root and every container stores *indices* into the array instead of values.
Wrapper types are encoded as `["TypeName", idx, ...]`.
"""

import json
import re
from typing import Any

_PAYLOAD_RE = re.compile(
    r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)

# devalue's special negative indices
_UNDEFINED, _HOLE, _NAN, _POS_INF, _NEG_INF, _NEG_ZERO = -1, -2, -3, -4, -5, -6
_SPECIALS: dict[int, Any] = {
    _UNDEFINED: None,
    _HOLE: None,
    _NAN: float("nan"),
    _POS_INF: float("inf"),
    _NEG_INF: float("-inf"),
    _NEG_ZERO: -0.0,
}
# Vue/Nuxt reactive wrappers are transparent: the value is at index 1.
_PASSTHROUGH = {"Reactive", "ShallowReactive", "Ref", "ShallowRef", "NuxtError", "Island"}


class NuxtPayloadError(ValueError):
    pass


def extract_payload(html: str) -> list[Any]:
    match = _PAYLOAD_RE.search(html)
    if not match:
        raise NuxtPayloadError("__NUXT_DATA__ script not found")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise NuxtPayloadError(f"invalid __NUXT_DATA__ JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise NuxtPayloadError("unexpected __NUXT_DATA__ shape")
    return data


def decode(data: list[Any]) -> Any:
    memo: dict[int, Any] = {}

    def ref(idx: Any) -> Any:
        if not isinstance(idx, int) or isinstance(idx, bool):
            return idx
        if idx < 0:
            return _SPECIALS.get(idx)
        return resolve(idx)

    def resolve(idx: int) -> Any:
        if idx in memo:
            return memo[idx]
        value = data[idx]
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                tag = value[0]
                if tag in _PASSTHROUGH:
                    out = ref(value[1]) if len(value) > 1 else None
                    memo[idx] = out
                    return out
                if tag in ("EmptyRef", "EmptyShallowRef"):
                    memo[idx] = None
                    return None
                if tag == "Date":
                    memo[idx] = value[1]
                    return value[1]
                if tag == "Set":
                    out_list: list[Any] = []
                    memo[idx] = out_list
                    out_list.extend(ref(i) for i in value[1:])
                    return out_list
                if tag in ("Map", "null"):
                    out_map: dict[Any, Any] = {}
                    memo[idx] = out_map
                    items = value[1:]
                    for k, v in zip(items[::2], items[1::2]):
                        key = ref(k) if tag == "Map" else k
                        out_map[key if isinstance(key, (str, int, float)) else str(key)] = ref(v)
                    return out_map
                if tag in ("BigInt", "RegExp"):
                    memo[idx] = value[1]
                    return value[1]
            out: list[Any] = []
            memo[idx] = out
            out.extend(ref(i) for i in value)
            return out
        if isinstance(value, dict):
            out_dict: dict[str, Any] = {}
            memo[idx] = out_dict
            for key, i in value.items():
                out_dict[key] = ref(i)
            return out_dict
        memo[idx] = value
        return value

    return resolve(0)


def page_data(html: str) -> dict[str, Any]:
    """Return the `data` map of the Nuxt payload (async-data keyed by fetch key)."""
    root = decode(extract_payload(html))
    data = root.get("data") if isinstance(root, dict) else None
    if not isinstance(data, dict):
        raise NuxtPayloadError("payload has no data section")
    return data
