#!/usr/bin/env python3
"""Analyze engine/logs/requests.jsonl* and surface duplicate / unnecessary calls.

Usage:
    python scripts/analyze_request_log.py
    python scripts/analyze_request_log.py --log engine/logs/requests.jsonl
    python scripts/analyze_request_log.py --window-ms 200 --top 30

Outputs to stdout. With --markdown <path>, also writes a Markdown report.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--log", default=None, help="Single log file (default: all engine/logs/requests.jsonl*)")
    p.add_argument("--window-ms", type=int, default=200, help="Duplicate detection window in ms")
    p.add_argument("--top", type=int, default=20, help="How many rows per table")
    p.add_argument("--markdown", default=None, help="Write Markdown report to this file")
    return p.parse_args()


def find_log_files(explicit: str | None) -> list[Path]:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            sys.exit(f"log file not found: {p}")
        return [p]
    root = Path(__file__).resolve().parent.parent
    log_dir = root / "engine" / "logs"
    files = sorted(log_dir.glob("requests.jsonl*"))
    if not files:
        sys.exit(f"no log files in {log_dir}")
    return files


def parse_iso_ms(ts: str) -> float:
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000.0
    except Exception:
        return 0.0


def load_entries(files: list[Path]) -> list[dict]:
    entries: list[dict] = []
    for f in files:
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if not isinstance(e, dict):
                    continue
                e["_ts_ms"] = parse_iso_ms(str(e.get("ts", "")))
                entries.append(e)
    entries.sort(key=lambda r: r.get("_ts_ms", 0))
    return entries


def fmt_table(title: str, headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return f"### {title}\n(no data)\n\n"
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    sep = "  ".join("-" * w for w in widths)
    head = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    body = "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in rows)
    return f"### {title}\n```\n{head}\n{sep}\n{body}\n```\n\n"


def section_overview(entries: list[dict]) -> str:
    if not entries:
        return "### Overview\n(no entries)\n\n"
    total = len(entries)
    by_source = Counter(str(e.get("source", "?")) for e in entries)
    span_ms = entries[-1]["_ts_ms"] - entries[0]["_ts_ms"] if entries else 0
    span_min = span_ms / 60000.0
    rate = total / max(span_min, 0.0001)
    rows = [
        ["total entries", str(total)],
        ["span (minutes)", f"{span_min:.1f}"],
        ["entries / minute", f"{rate:.1f}"],
    ]
    for k, v in by_source.most_common():
        rows.append([f"  source={k}", str(v)])
    return fmt_table("Overview", ["metric", "value"], rows)


def section_top_paths(entries: list[dict], top: int) -> str:
    by_key: Counter[tuple[str, str, str]] = Counter()
    for e in entries:
        if e.get("kind") == "logger_init":
            continue
        by_key[(str(e.get("source", "?")), str(e.get("method", "?")), str(e.get("path", "?")))] += 1
    rows = [[src, m, p, str(n)] for (src, m, p), n in by_key.most_common(top)]
    return fmt_table(f"Top {top} endpoints by call count", ["source", "method", "path", "count"], rows)


def section_top_callers(entries: list[dict], top: int) -> str:
    by_path_caller: dict[str, Counter] = defaultdict(Counter)
    for e in entries:
        if e.get("source") != "client":
            continue
        path = str(e.get("path", "?"))
        caller = str(e.get("caller_hint", "(no caller)"))
        by_path_caller[path][caller] += 1
    rows: list[list[str]] = []
    for path, callers in sorted(by_path_caller.items(), key=lambda kv: -sum(kv[1].values())):
        for caller, n in callers.most_common(5):
            rows.append([path, caller, str(n)])
        if len(rows) > top * 5:
            break
    return fmt_table("Frontend callers per endpoint (which component fires it)",
                     ["path", "caller_hint", "count"], rows[: top * 5])


def section_overlap(entries: list[dict], window_ms: int, top: int) -> str:
    """Detect overlapping client calls to the same path within window_ms.

    Counts pairs where two distinct callers hit the same path within window_ms of each other.
    """
    by_path: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if e.get("source") != "client":
            continue
        by_path[str(e.get("path", "?"))].append(e)
    overlap_count: Counter[tuple[str, str, str]] = Counter()
    for path, evs in by_path.items():
        evs.sort(key=lambda r: r["_ts_ms"])
        for i, a in enumerate(evs):
            for b in evs[i + 1: i + 50]:
                if b["_ts_ms"] - a["_ts_ms"] > window_ms:
                    break
                ca = str(a.get("caller_hint", "?"))
                cb = str(b.get("caller_hint", "?"))
                if ca == cb:
                    continue
                key = tuple(sorted([ca, cb]))
                overlap_count[(path, key[0], key[1])] += 1
    rows = [[p, ca, cb, str(n)] for (p, ca, cb), n in overlap_count.most_common(top)]
    return fmt_table(
        f"Overlapping client callers within {window_ms} ms (different components hitting the same path)",
        ["path", "caller_a", "caller_b", "count"], rows)


def section_duplicate_bursts(entries: list[dict], window_ms: int, top: int) -> str:
    """Same path called more than once within window_ms — regardless of caller."""
    by_path: dict[str, list[float]] = defaultdict(list)
    for e in entries:
        if e.get("source") != "client":
            continue
        by_path[str(e.get("path", "?"))].append(e["_ts_ms"])
    rows: list[list[str]] = []
    for path, ts_list in by_path.items():
        ts_list.sort()
        bursts = 0
        for i in range(1, len(ts_list)):
            if ts_list[i] - ts_list[i - 1] <= window_ms:
                bursts += 1
        if bursts:
            rows.append([path, str(bursts), str(len(ts_list))])
    rows.sort(key=lambda r: -int(r[1]))
    return fmt_table(
        f"Per-endpoint burst pairs within {window_ms} ms (consecutive client calls)",
        ["path", "burst_pairs", "total_calls"], rows[:top])


def section_status_codes(entries: list[dict]) -> str:
    by_status: Counter[tuple[str, int]] = Counter()
    for e in entries:
        src = str(e.get("source", "?"))
        st = e.get("status")
        if not isinstance(st, int):
            continue
        by_status[(src, st)] += 1
    rows = [[src, str(st), str(n)] for (src, st), n in by_status.most_common()]
    return fmt_table("Status code distribution", ["source", "status", "count"], rows)


def section_slowest(entries: list[dict], top: int) -> str:
    server = [(float(e.get("duration_ms", 0)), str(e.get("path", "?"))) for e in entries
              if e.get("source") == "server" and isinstance(e.get("duration_ms"), (int, float))]
    server.sort(reverse=True)
    rows = [[p, f"{d:.1f}"] for d, p in server[:top]]
    return fmt_table(f"Top {top} slowest server requests (ms)", ["path", "duration_ms"], rows)


def main() -> int:
    args = parse_args()
    files = find_log_files(args.log)
    entries = load_entries(files)

    parts: list[str] = []
    parts.append(f"# Request log analysis\n\nFiles: {', '.join(str(f) for f in files)}\n\n")
    parts.append(section_overview(entries))
    parts.append(section_top_paths(entries, args.top))
    parts.append(section_top_callers(entries, args.top))
    parts.append(section_overlap(entries, args.window_ms, args.top))
    parts.append(section_duplicate_bursts(entries, args.window_ms, args.top))
    parts.append(section_status_codes(entries))
    parts.append(section_slowest(entries, args.top))

    report = "".join(parts)
    print(report)
    if args.markdown:
        Path(args.markdown).write_text(report, encoding="utf-8")
        print(f"\nWrote Markdown report → {args.markdown}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
