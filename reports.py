# reports.py
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from db import SessionLocal, AlertSnapshot

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

# <<< unify timezone here >>>
LOCAL_TZ = ZoneInfo("Asia/Tehran")
# (optionally: from os import getenv; LOCAL_TZ = ZoneInfo(getenv("APP_LOCAL_TZ","Asia/Tehran")))

def _bounds_day_local(day_local: datetime):
    start_local = datetime(day_local.year, day_local.month, day_local.day, 0, 0, 0, tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

def _bounds_week_ending_local(end_local_day: datetime):
    end_local = datetime(end_local_day.year, end_local_day.month, end_local_day.day, 23, 59, 59, tzinfo=LOCAL_TZ) + timedelta(seconds=1)
    start_local = end_local - timedelta(days=7)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

def _bounds_month_local(year: int, month: int):
    start_local = datetime(year, month, 1, tzinfo=LOCAL_TZ)
    nm = (month % 12) + 1
    ny = year + (1 if month == 12 else 0)
    end_local = datetime(ny, nm, 1, tzinfo=LOCAL_TZ)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

# ... (rest of file unchanged)


# ---- Utilities ----
def _to_utc_aware(dt):
    """Ensure datetime is timezone-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _alert_key(row: AlertSnapshot) -> str:
    """Stable identity per alert time-series within a DC."""
    return row.fingerprint or f"{row.alertname}|{row.source}"


# ---- Summary: unique fired & unique suppressed in the window ----
def _summary_totals(start_utc, end_utc):
    """
    For each DC, count unique alerts that:
      - fired at least once in [start_utc, end_utc)   (status in {'active','firing'})
      - were suppressed at least once in the window   (status == 'suppressed')
    """
    with SessionLocal() as s:
        rows = (
            s.execute(
                select(AlertSnapshot).where(
                    AlertSnapshot.ts >= start_utc, AlertSnapshot.ts < end_utc
                )
            )
            .scalars()
            .all()
        )

    per_dc = {}
    for r in rows:
        dc = r.dc or "Unassigned"
        entry = per_dc.setdefault(dc, {"fired": set(), "supp": set(), "samples": 0})
        entry["samples"] += 1
        status = (r.status or "").lower()
        key = _alert_key(r)
        if status in ("active", "firing"):
            entry["fired"].add(key)
        if status == "suppressed":
            entry["supp"].add(key)

    out = []
    for dc, v in sorted(per_dc.items()):
        out.append(
            {
                "dc": dc,
                "fired": len(v["fired"]),
                "suppressed": len(v["supp"]),
                "samples": v["samples"],  # for reference/debug
            }
        )
    return out


# ---- Details: durations clipped to window ----
def _detail_alerts(start_utc, end_utc):
    """
    Group alert snapshots by (dc, fingerprint or (alertname,source)).
    Report effective start/end within window and duration seconds.
    """
    with SessionLocal() as s:
        rows = (
            s.execute(
                select(AlertSnapshot).where(
                    AlertSnapshot.ts >= start_utc, AlertSnapshot.ts < end_utc
                )
            )
            .scalars()
            .all()
        )

    agg = {}
    for r in rows:
        key = (r.dc, _alert_key(r))
        it = agg.get(key)

        ts = _to_utc_aware(r.ts)
        starts_at = _to_utc_aware(r.starts_at)
        ends_at = _to_utc_aware(r.ends_at)

        if not it:
            it = agg[key] = {
                "dc": r.dc,
                "alertname": r.alertname,
                "source": r.source,
                "fingerprint": r.fingerprint,
                "statuses": set(),
                "min_seen_ts": ts,
                "max_seen_ts": ts,
                "min_starts_at": starts_at,
                "max_ends_at": ends_at,
                "labels": r.labels or {},
                "annotations": r.annotations or {},
            }
        it["statuses"].add((r.status or "").lower())
        it["min_seen_ts"] = min(it["min_seen_ts"], ts)
        it["max_seen_ts"] = max(it["max_seen_ts"], ts)
        if starts_at:
            it["min_starts_at"] = (
                starts_at if (it["min_starts_at"] is None) else min(it["min_starts_at"], starts_at)
            )
        if ends_at:
            it["max_ends_at"] = (
                ends_at if (it["max_ends_at"] is None) else max(it["max_ends_at"], ends_at)
            )

    # finalize + split by dc
    by_dc = {}
    for it in agg.values():
        # If we never captured an explicit ends_at, stop at the last time we saw it
        # (avoids over-counting if it resolved between polls).
        start = it["min_starts_at"] or it["min_seen_ts"]
        end = it["max_ends_at"] or it["max_seen_ts"]

        eff_start = max(start, start_utc)
        eff_end = min(end, end_utc)
        dur = max(0, int((eff_end - eff_start).total_seconds()))

        rec = {
            "alertname": it["alertname"],
            "source": it["source"],
            "fingerprint": it["fingerprint"],
            "statuses": sorted(it["statuses"]),
            "start_utc": eff_start.isoformat(),
            "end_utc": eff_end.isoformat(),
            "duration_seconds": dur,
            "labels": it["labels"],
            "annotations": it["annotations"],
        }
        by_dc.setdefault(it["dc"], []).append(rec)

    for dc in by_dc:
        by_dc[dc].sort(key=lambda x: (-x["duration_seconds"], x["alertname"]))
    return by_dc


# ---- Builders (daily / weekly / monthly) ----
def build_report_daily(date_local: datetime):
    start_utc, end_utc = _bounds_day_local(date_local)
    return {
        "period": "daily",
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "summary": _summary_totals(start_utc, end_utc),
        "details": _detail_alerts(start_utc, end_utc),
    }


def build_report_weekly(end_day_local: datetime):
    start_utc, end_utc = _bounds_week_ending_local(end_day_local)
    return {
        "period": "weekly",
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "summary": _summary_totals(start_utc, end_utc),
        "details": _detail_alerts(start_utc, end_utc),
    }


def build_report_monthly(year: int, month: int):
    start_utc, end_utc = _bounds_month_local(year, month)
    return {
        "period": "monthly",
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "summary": _summary_totals(start_utc, end_utc),
        "details": _detail_alerts(start_utc, end_utc),
    }

