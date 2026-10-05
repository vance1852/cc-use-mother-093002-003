"""提供可推进的时钟，供离线验收与测试使用。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class MutableClock:
    """允许测试与验收按需推进的 UTC 时钟。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("初始时间必须包含时区")
        self._value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        """返回当前时钟时间。"""

        return self._value

    def advance(self, **kwargs: int) -> None:
        """按 timedelta 参数推进时钟。"""

        self._value = self._value + timedelta(**kwargs)
