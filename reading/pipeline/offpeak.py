"""Off-peak guard for ingest.

DeepSeek bills peak rates during **01:00-04:00 and 06:00-10:00 UTC, Monday through
Friday, excluding Chinese public holidays**; every other hour — including all of
Saturday and Sunday — is off-peak at half the price. See
https://api-docs.deepseek.com/quick_start/pricing/

An ingest run spends money, so by default it refuses to start during peak hours and
says when the window opens. ``--allow-peak`` (or ``READING_ALLOW_PEAK=true``) is the
explicit approval.

Note on holidays: telling whether today is a Chinese public holiday needs a calendar
this module does not carry, so the default is the *conservative* reading — a holiday
is treated as peak and the run waits. ``READING_OFFPEAK_DATES`` (ISO dates) marks days
you know to be holidays, and those become off-peak all day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

# Half-open [start, end) in UTC hours.
DEFAULT_PEAK_WINDOWS: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))
# datetime.weekday(): Monday == 0.
DEFAULT_PEAK_WEEKDAYS: tuple[int, ...] = (0, 1, 2, 3, 4)

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def parse_windows(raw: str) -> tuple[tuple[int, int], ...]:
    """Parse ``"01:00-04:00,06:00-10:00"`` into ``((1, 4), (6, 10))``."""
    windows: list[tuple[int, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            start_s, end_s = chunk.split("-")
            start = int(start_s.split(":")[0])
            end = int(end_s.split(":")[0])
        except ValueError as exc:
            raise ValueError(f"bad peak window {chunk!r}; expected e.g. 01:00-04:00") from exc
        if not 0 <= start < end <= 24:
            raise ValueError(f"bad peak window {chunk!r}; hours must satisfy 0 <= start < end <= 24")
        windows.append((start, end))
    return tuple(windows)


def parse_weekdays(raw: str) -> tuple[int, ...]:
    """Parse ``"Mon,Tue,Wed,Thu,Fri"`` (or numbers) into weekday indices."""
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.isdigit():
            out.append(int(chunk) % 7)
            continue
        for index, name in enumerate(WEEKDAY_NAMES):
            if name.casefold() == chunk[:3].casefold():
                out.append(index)
                break
        else:
            raise ValueError(f"bad weekday {chunk!r}; use Mon..Sun")
    return tuple(sorted(set(out)))


@dataclass(frozen=True)
class PeakPolicy:
    """When DeepSeek charges peak rates."""

    windows: tuple[tuple[int, int], ...] = DEFAULT_PEAK_WINDOWS
    weekdays: tuple[int, ...] = DEFAULT_PEAK_WEEKDAYS
    # ISO dates (YYYY-MM-DD) that are holidays: off-peak for the whole day.
    offpeak_dates: frozenset[str] = frozenset()

    def is_peak(self, when: datetime) -> bool:
        if when.tzinfo is None:
            raise ValueError("is_peak needs a timezone-aware datetime")
        moment = when.astimezone(timezone.utc)
        if moment.date().isoformat() in self.offpeak_dates:
            return False
        if moment.weekday() not in self.weekdays:
            return False
        return any(start <= moment.hour < end for start, end in self.windows)

    def next_offpeak(self, when: datetime) -> datetime:
        """The first off-peak hour at or after ``when``.

        Windows are hour-aligned, so stepping by the hour is exact. Bounded so a
        pathological policy cannot spin.
        """
        moment = when.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for _ in range(24 * 8):
            if not self.is_peak(moment):
                return moment
            moment += timedelta(hours=1)
        return moment  # unreachable for any sane policy

    def describe(self) -> str:
        windows = ", ".join(f"{s:02d}:00-{e:02d}:00" for s, e in self.windows)
        days = ", ".join(WEEKDAY_NAMES[d] for d in self.weekdays) or "never"
        return f"peak = {windows} UTC on {days} (all other hours off-peak)"


def status_line(policy: PeakPolicy, now: datetime) -> str:
    """One-line summary for logs and refusal messages."""
    moment = now.astimezone(timezone.utc)
    if policy.is_peak(moment):
        opens = policy.next_offpeak(moment)
        delta = opens - moment.replace(minute=0, second=0, microsecond=0)
        hours = int(delta.total_seconds() // 3600)
        return (
            f"PEAK now ({moment:%Y-%m-%d %H:%M} UTC) — off-peak resumes "
            f"{opens:%Y-%m-%d %H:%M} UTC (in {hours}h)"
        )
    return f"off-peak now ({moment:%Y-%m-%d %H:%M} UTC)"


def check(policy: PeakPolicy, now: datetime, allow_peak: bool = False) -> tuple[bool, str]:
    """Decide whether a paid run may start.  Returns ``(allowed, message)``.

    ``now`` is a parameter rather than read from the clock so the decision is
    directly testable — this is the one place that authorises spending money.
    """
    if allow_peak or not policy.is_peak(now):
        return True, status_line(policy, now)

    opens = policy.next_offpeak(now)
    return False, (
        "Refusing to run during peak hours: DeepSeek bills double, and this run "
        "would both fetch and translate.\n"
        f"  {policy.describe()}\n"
        f"  Off-peak resumes {opens:%Y-%m-%d %H:%M} UTC.\n"
        "Re-run then, or pass --allow-peak (or set READING_ALLOW_PEAK=true) to "
        "approve this one explicitly."
    )
