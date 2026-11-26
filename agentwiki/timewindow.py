#!/usr/bin/env python3
"""離峰時段：搬移、重新解析只在這段時間排新工作。

時段寫法（分號分隔多段；星期可省略，等於每天）：
  "mon-fri 22:30-07:00; sat-sun all"   平日 22:30 到隔天 07:00、週末全天
  "22:00-06:00"                        每天 22:00 到 06:00
  "mon,wed,fri 12:00-13:30"

★ 每一段只看「當天的時鐘」：跨午夜的 22:30-07:00 在那幾天裡都是 [22:30, 24:00) 加 [00:00, 07:00)。
  所以 "mon-fri 22:30-07:00" 的週一 00:00-07:00 也算，週六 00:00-07:00 則要靠 sat 那一段。
  加上 "sat-sun all" 時，週五 22:30 一路到週一 07:00 是連續的。
★ 用本機時區（cron 也是）。

  timewindow.py check "<時段>" [--now 2026-11-27T23:00]
    在時段內：印 "open <結束的 epoch 秒|never>"，結束代碼 0
    時段外：  印 "closed <下次開始的 epoch 秒|never>"，結束代碼 3
    寫錯：    結束代碼 2

kb-stack/scripts/time-window.py 是同一份程式（兩個 repo 各放一份，改一邊要同步另一邊）。
"""
import re
import sys
from datetime import datetime, timedelta

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT = "mon-fri 22:30-07:00; sat-sun all"


def _minutes(s):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m or int(m.group(1)) > 24 or int(m.group(2)) > 59 or (int(m.group(1)) == 24 and m.group(2) != "00"):
        raise ValueError(f"時間要寫成 HH:MM：{s}")
    return int(m.group(1)) * 60 + int(m.group(2))


def _days(s):
    s = s.lower()
    if s in ("all", "*", "daily", "every"):
        return set(range(7))
    out = set()
    for item in s.split(","):
        a, _, b = item.strip().partition("-")
        if a not in DAYS or (b and b not in DAYS):
            raise ValueError(f"星期要寫成 mon..sun（可用 mon-fri、sat,sun）：{item}")
        i, j = DAYS.index(a), DAYS.index(b or a)
        while True:
            out.add(i)
            if i == j:
                break
            i = (i + 1) % 7   # fri-mon 也可以
    return out


def parse(spec):
    """→ [(星期集合, 起分鐘, 迄分鐘)]；起 > 迄 表示跨午夜。"""
    segs = []
    for part in re.split(r"[;\n]", spec or ""):
        toks = part.split()
        if not toks:
            continue
        if len(toks) == 1:
            days, rng = set(range(7)), toks[0]
        elif len(toks) == 2:
            days, rng = _days(toks[0]), toks[1]
        else:
            raise ValueError(f"每段是「星期 時間」，例如 mon-fri 22:30-07:00：{part.strip()}")
        if rng.lower() == "all":
            start, end = 0, 1440
        else:
            a, sep, b = rng.partition("-")
            if not sep:
                raise ValueError(f"時間要寫成 HH:MM-HH:MM 或 all：{rng}")
            start, end = _minutes(a), _minutes(b)
            if start == end:
                raise ValueError(f"開始與結束相同：{rng}（全天用 all）")
        segs.append((days, start, end))
    if not segs:
        raise ValueError("時段是空的")
    return segs


def is_open(segs, dt):
    t = dt.hour * 60 + dt.minute
    for days, start, end in segs:
        if dt.weekday() not in days:
            continue
        if (start < end and start <= t < end) or (start > end and (t >= start or t < end)):
            return True
    return False


def _next_change(segs, now, want_open):
    """從 now 起，第一個 is_open == want_open 的整分鐘；一週內都沒有就是 None。"""
    t = now.replace(second=0, microsecond=0)
    for _ in range(8 * 1440):
        t += timedelta(minutes=1)
        if is_open(segs, t) == want_open:
            return t
    return None


def closes_at(segs, now):
    """在時段內時，什麼時候結束（None = 永遠不結束）。"""
    return _next_change(segs, now, False)


def opens_at(segs, now):
    """時段外時，下次什麼時候開始（None = 永遠不開始）。"""
    return _next_change(segs, now, True)


def main(argv):
    if len(argv) < 2 or argv[0] != "check":
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        segs = parse(argv[1])
        now = datetime.fromisoformat(argv[argv.index("--now") + 1]) if "--now" in argv else datetime.now()
    except (ValueError, IndexError) as e:
        print(f"時段寫錯：{e}", file=sys.stderr)
        return 2
    if is_open(segs, now):
        end = closes_at(segs, now)
        print(f"open {int(end.timestamp()) if end else 'never'}")
        return 0
    start = opens_at(segs, now)
    print(f"closed {int(start.timestamp()) if start else 'never'}")
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
