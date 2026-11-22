"""Plan when to open each account's 5-hour window.

A window starts with the first message and lasts 5 hours. If you start working at 09:00
the windows are 09-14 and 14-19: two windows of quota in a 10-hour day. A tiny message at
06:00 makes them 06-11, 11-16 and 16-21: three windows, and the first reset lands mid-morning
instead of mid-afternoon.

Each profile gets a chain of "anchors" (ping times). The first is timed so its window resets
`first_reset_after` minutes after work starts; then one every 5 hours (+ a small delay, so the
previous window has surely reset) while the window still overlaps work hours by at least
`min_useful` minutes.

Strategies for several accounts:
  stagger  offset each account's chain by window/N, so some account resets every 5h/N hours.
           Best for continuous heavy use: there is always an account that reset recently.
  align    every account on the same chain: maximum total quota at the start of the day.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

WINDOW_MIN = 300
REANCHOR_DELAY_MIN = 2
DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]  # launchd Weekday 0..6


def parse_hhmm(s: str) -> int:
    s = s.strip()
    if ":" in s:
        h, m = s.split(":", 1)
    else:
        h, m = s, "0"
    hh, mm = int(h), int(m)
    if not (0 <= hh <= 24 and 0 <= mm < 60) or hh * 60 + mm > 1440:
        raise ValueError(f"bad time '{s}' (use HH:MM)")
    return hh * 60 + mm


def fmt_hhmm(minute: int) -> str:
    minute %= 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def parse_days(spec) -> List[int]:
    """'mon-fri', 'daily', 'weekdays', 'weekends', 'mon,wed,fri' or a list -> launchd weekdays."""
    if isinstance(spec, (list, tuple)):
        items = [str(x) for x in spec]
    else:
        s = str(spec).strip().lower()
        if s in ("daily", "all", "everyday"):
            return list(range(7))
        if s in ("weekdays", "workdays"):
            return [1, 2, 3, 4, 5]
        if s in ("weekends", "weekend"):
            return [0, 6]
        items = [x for x in s.replace(" ", "").split(",") if x]
    out: List[int] = []
    for it in items:
        it = it.lower()
        if "-" in it:
            a, b = (DAY_NAMES.index(x[:3]) for x in it.split("-", 1))
            d = a
            while True:
                out.append(d)
                if d == b:
                    break
                d = (d + 1) % 7
        else:
            out.append(DAY_NAMES.index(it[:3]))
    return sorted(set(out))


def plan(profiles: Sequence[str], start: int, end: int, strategy: str = "stagger",
         first_reset_after: int = 120, min_useful: int = 60, window: int = WINDOW_MIN,
         delay: int = REANCHOR_DELAY_MIN) -> Dict[str, List[int]]:
    """Ping times per profile, in minutes from the work day's midnight (may be negative)."""
    if end <= start:
        end += 1440  # work day crosses midnight
    if strategy not in ("stagger", "align"):
        raise ValueError("strategy must be 'stagger' or 'align'")
    n = max(1, len(profiles))
    base = start + first_reset_after - window
    result: Dict[str, List[int]] = {}
    for i, name in enumerate(profiles):
        a = base + (window * i // n if strategy == "stagger" else 0)
        anchors: List[int] = []
        while a < end:
            overlap = min(a + window, end) - max(a, start)
            if overlap >= min_useful:
                anchors.append(a)
            a += window + delay
        result[name] = anchors
    return result


def calendar_entries(anchors: Sequence[int], days: Sequence[int]) -> List[Dict[str, int]]:
    """launchd StartCalendarInterval dicts. Anchors before midnight belong to the previous day."""
    seen = set()
    out: List[Dict[str, int]] = []
    for d in days:
        for a in anchors:
            shift, minute = divmod(a, 1440)
            key = ((d + shift) % 7, minute // 60, minute % 60)
            if key not in seen:
                seen.add(key)
                out.append({"Weekday": key[0], "Hour": key[1], "Minute": key[2]})
    out.sort(key=lambda e: (e["Weekday"], e["Hour"], e["Minute"]))
    return out


def worth_pinging(now_struct, days: Sequence[int], anchors: Sequence[int], start: int, end: int) -> bool:
    """Should a scheduled ping that fires at `now` (time.struct_time) still run?

    launchd runs a missed job when the Mac wakes up, possibly hours late. A ping is still
    useful if it lands between the first planned ping and the end of work hours of a
    scheduled day; after work hours it would only burn a window nobody uses.
    """
    if not anchors:
        return False
    if end <= start:
        end += 1440
    wd = (now_struct.tm_wday + 1) % 7  # struct_time Monday=0 -> launchd Monday=1
    minute = now_struct.tm_hour * 60 + now_struct.tm_min
    for offset in (-1, 0, 1):  # work day = yesterday (overnight), today, tomorrow (early pings)
        if (wd + offset) % 7 not in days:
            continue
        m = minute - 1440 * offset  # minutes relative to that work day's midnight
        if min(anchors) - 5 <= m < end:
            return True
    return False


def render(plan_: Dict[str, List[int]], start: int, end: int, window: int = WINDOW_MIN) -> str:
    """Text timeline, one row per profile, 30 minutes per character."""
    if end <= start:
        end += 1440
    all_anchors = [a for v in plan_.values() for a in v] or [start]
    lo = (min(min(all_anchors), start) // 60) * 60
    hi = -(-max(max(a + window for a in all_anchors), end) // 60) * 60
    step = 30
    width = (hi - lo) // step
    namew = max([len(n) for n in plan_] + [4])
    lines = []
    if lo % 120:  # align the 2-hour labels (4 slots each) to even hours
        lo -= 60
        width += 2
    lines.append(" " * (namew + 2) + "".join(fmt_hhmm(m)[:2].ljust(4) for m in range(lo, hi, 120)))
    work = " " * (namew + 2) + "".join("▔" if start <= lo + i * step < end else " " for i in range(width))
    lines.append(work + "  work hours")
    for name, anchors in plan_.items():
        row = []
        for i in range(width):
            m = lo + i * step
            ch = "·"
            for k, a in enumerate(anchors):
                if a < m + step and m < a + window:  # window overlaps this slot
                    ch = "█" if k % 2 == 0 else "▓"
            row.append(ch)
        times = " ".join(fmt_hhmm(a) for a in anchors) or "(no useful window)"
        lines.append(f"{name.ljust(namew)}  {''.join(row)}  pings {times}")
    return "\n".join(lines)
