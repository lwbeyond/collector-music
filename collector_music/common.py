from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MusicError(Exception):
    """An actionable data/configuration error, without credentials."""


class LoginRequired(MusicError):
    """The platform channel must wait for a new local login session."""


class WorkspaceBusy(MusicError):
    """Another local job owns the requested workspace lock; no request failed."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       indent=2) + "\n").encode("utf-8")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MusicError(f"JSON 含重复键：{key}")
        result[key] = value
    return result


def parse_json(data: bytes | str) -> Any:
    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8-sig")
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (UnicodeError, ValueError) as exc:
        raise MusicError("不是有效的 UTF-8 JSON，原始数据应保留供检查。") from exc


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def identifier(value: str, label: str = "标识") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise MusicError(f"{label}需使用 1–96 位字母、数字、点、横线或下划线。")
    return value


def utc_ms(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MusicError("时间戳必须是非负整数 UTC 毫秒。")
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError) as exc:
        raise MusicError("时间戳超出支持范围。") from exc


def utc_time(value: Any) -> str:
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.utcoffset() is None:
            raise ValueError()
        return date.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    except (AttributeError, TypeError, ValueError) as exc:
        raise MusicError("时间必须是带时区的 ISO 8601。") from exc


def required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MusicError(f"{field} 必须是非空文本。")
    return value


def stable_id(*parts: Any) -> str:
    return digest(json_text(parts).encode("utf-8"))


def safe_child(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise MusicError("清单路径超出归档/快照目录。")
    return path
