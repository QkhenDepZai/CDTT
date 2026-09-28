"""Danh sách adapter theo tên kênh (khởi tạo lười, dùng chung trong tiến trình)."""
from __future__ import annotations

import threading

from channels.base import ChannelAdapter

_adapters: dict[str, ChannelAdapter] = {}
_lock = threading.Lock()


def _build(name: str) -> ChannelAdapter | None:
    if name == "messenger":
        from channels.messenger import MessengerAdapter
        return MessengerAdapter()
    if name == "zalo":
        from channels.zalo import ZaloAdapter
        return ZaloAdapter()
    return None


def get_adapter(name: str) -> ChannelAdapter | None:
    with _lock:
        if name not in _adapters:
            adapter = _build(name)
            if adapter is None:
                return None
            _adapters[name] = adapter
        return _adapters[name]


def set_adapter(name: str, adapter: ChannelAdapter) -> None:
    """Thay adapter (dùng trong test hoặc khi cần cấu hình đặc biệt)."""
    with _lock:
        _adapters[name] = adapter


def channel_status() -> dict[str, bool]:
    return {name: bool(get_adapter(name) and get_adapter(name).is_configured)
            for name in ("messenger", "zalo")}
