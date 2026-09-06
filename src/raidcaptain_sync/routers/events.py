"""
事件路由 - RAID Captain Sync
"""
import json
import time

from fastapi import APIRouter, Depends, Header

from raidcaptain_sync.deps import auth_parent, get_db, parent_sockets, ws_push

router = APIRouter()


@router.get("/api/events")
def list_events(
    since: int = 0,
    limit: int = 100,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """家长拉取增量事件（since = _id 游标）。"""
    fam = auth_parent(db, authorization)
    rows = db.execute(
        "SELECT * FROM event WHERE family_id=? AND _id>? ORDER BY _id ASC LIMIT ?",
        (fam["id"], since, min(limit, 500)),
    ).fetchall()
    return {"events": [dict(r) for r in rows]}


@router.get("/api/parent/history")
def history_events(
    since_days: int = 7,
    kind: str = "",
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """历史事件（按天分组，含 patrol_session）。"""
    from collections import defaultdict
    fam = auth_parent(db, authorization)
    since_ts = int(time.time()) - since_days * 86400
    sql = "SELECT * FROM event WHERE family_id=? AND created_at>=?"
    args = [fam["id"], since_ts]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY created_at DESC LIMIT 500"
    rows = db.execute(sql, args).fetchall()

    ps_sql = "SELECT * FROM patrol_session WHERE family_id=? AND started_at>=?"
    ps_rows = db.execute(ps_sql + " ORDER BY started_at DESC LIMIT 500",
                         (fam["id"], since_ts)).fetchall()

    by_day = defaultdict(list)
    for r in rows:
        p = {}
        try:
            p = json.loads(r["payload"] or "{}")
        except Exception:
            pass
        ev = {
            "id": r["_id"], "kind": r["kind"], "created_at": r["created_at"],
            "device_name": r.get("device_name", ""),
            "task_id": p.get("task_id", ""),
            "title": p.get("title", ""),
            "state": p.get("state", ""),
            "merit_delta": p.get("merit_delta", 0),
            "points_delta": p.get("points_delta", 0),
            "evidence": p.get("evidence", False),
            "score": p.get("score"),
            "result": p.get("result"),
            "appeal_status": p.get("status", ""),
        }
        day = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
        by_day[day].append(ev)

    for r in ps_rows:
        ev = {
            "id": r["_id"], "kind": "patrol_session",
            "created_at": r["started_at"] * 1000,
            "device_name": r.get("device_name", ""),
            "task_id": "", "title": r.get("task_name", ""),
            "state": "", "merit_delta": r["merit_delta"],
            "points_delta": r["points_delta"],
            "evidence": False,
            "valid_minutes": r["valid_minutes"],
            "sessions": r["sessions"],
            "outcome": r.get("outcome", ""),
        }
        day = time.strftime("%Y-%m-%d", time.localtime(r["started_at"]))
        by_day[day].append(ev)

    summaries = {}
    for day, evs in by_day.items():
        done = sum(1 for e in evs if e["kind"] == "task_completion" and e["state"] == "DONE")
        overdue = sum(1 for e in evs if e["kind"] == "task_completion" and e["state"] == "OVERDUE")
        merit = sum(e["merit_delta"] for e in evs)
        points = sum(e["points_delta"] for e in evs)
        summaries[day] = {
            "done": done, "overdue": overdue,
            "merit": merit, "points": points, "total": len(evs)
        }

    return {
        "days": list(reversed(sorted(by_day.keys()))),
        "by_day": {k: by_day[k] for k in sorted(by_day)},
        "summaries": summaries,
        "total": len(rows) + len(ps_rows),
    }


@router.get("/api/parent/patrol-sessions")
def parent_patrol_sessions(
    since_days: int = 7,
    task: str = "",
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """家长拉取学习时段。"""
    from collections import defaultdict
    fam = auth_parent(db, authorization)
    since_ts = int(time.time()) - since_days * 86400
    sql = "SELECT * FROM patrol_session WHERE family_id=? AND started_at>=?"
    args = [fam["id"], since_ts]
    if task:
        sql += " AND task_name=?"
        args.append(task)
    sql += " ORDER BY started_at DESC LIMIT 500"
    rows = db.execute(sql, args).fetchall()

    by_day = defaultdict(list)
    for r in rows:
        d = time.strftime("%Y-%m-%d", time.localtime(r["started_at"]))
        by_day[d].append({
            "id": r["_id"], "device_name": r["device_name"],
            "task_name": r["task_name"],
            "started_at": r["started_at"], "ended_at": r["ended_at"],
            "valid_minutes": r["valid_minutes"],
            "points_delta": r["points_delta"], "merit_delta": r["merit_delta"],
            "sessions": r["sessions"], "outcome": r["outcome"],
        })

    totals = {
        "valid_minutes": sum(r["valid_minutes"] for r in rows),
        "sessions_count": sum(r["sessions"] for r in rows),
        "points": sum(r["points_delta"] for r in rows),
        "merit": sum(r["merit_delta"] for r in rows),
    }

    task_rows = db.execute(
        "SELECT DISTINCT task_name FROM patrol_session WHERE family_id=? AND task_name!=''",
        (fam["id"],)).fetchall()
    tasks = [r["task_name"] for r in task_rows]

    return {
        "days": list(reversed(sorted(by_day.keys()))),
        "by_day": {k: by_day[k] for k in sorted(by_day)},
        "totals": totals,
        "tasks": tasks,
    }


@router.get("/api/parent/learning-stats")
def parent_learning_stats(
    days: int = 7,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """每日学习汇总（折线图数据）。"""
    import datetime
    fam = auth_parent(db, authorization)
    days = max(1, min(30, days))
    today = datetime.date.today()
    start_dt = today - datetime.timedelta(days=days - 1)
    start_ts = int(datetime.datetime(start_dt.year, start_dt.month, start_dt.day).timestamp())
    rows = db.execute(
        """SELECT started_at, valid_minutes, sessions, points_delta, merit_delta
           FROM patrol_session WHERE family_id=? AND started_at>=?""",
        (fam["id"], start_ts)).fetchall()

    by_day = {}
    for i in range(days):
        d = (start_dt + datetime.timedelta(days=i)).isoformat()
        by_day[d] = {"date": d, "valid_minutes": 0, "sessions": 0,
                     "points": 0, "merit": 0}
    for r in rows:
        d = datetime.datetime.fromtimestamp(r["started_at"]).date().isoformat()
        if d in by_day:
            by_day[d]["valid_minutes"] += r["valid_minutes"]
            by_day[d]["sessions"] += r["sessions"]
            by_day[d]["points"] += r["points_delta"]
            by_day[d]["merit"] += r["merit_delta"]

    return {
        "days": list(by_day.values()),
        "totals": {
            "valid_minutes": sum(d["valid_minutes"] for d in by_day.values()),
            "sessions_count": sum(d["sessions"] for d in by_day.values()),
            "points": sum(d["points"] for d in by_day.values()),
            "merit": sum(d["merit"] for d in by_day.values()),
        },
    }


@router.get("/api/parent/stats")
def parent_stats(
    days: int = 7,
    authorization: str | None = Header(None),
    db=Depends(get_db),
):
    """战绩统计（完成率/功绩/纲纪）。"""
    import datetime
    fam = auth_parent(db, authorization)
    days = max(1, min(30, days))
    today = datetime.date.today()
    start = int(datetime.datetime(today.year, today.month, today.day).timestamp() * 1000) - (days - 1) * 86_400_000
    events = db.execute(
        """SELECT _id, kind, payload, created_at FROM event
           WHERE family_id=? AND kind='task_completion' AND created_at>=?
           ORDER BY _id ASC""",
        (fam["id"], start)).fetchall()

    by_day = {}
    for i in range(days):
        d = (today - datetime.timedelta(days=days - 1 - i)).isoformat()
        by_day[d] = {"date": d, "done": 0, "overdue": 0}

    total_done = total_overdue = 0
    for r in events:
        p = {}
        try:
            p = json.loads(r["payload"]) if r["payload"] else {}
        except Exception:
            pass
        ts = r["created_at"]
        d = datetime.datetime.fromtimestamp(
            ts / 1000, tz=datetime.timezone.utc
        ).astimezone().date().isoformat()
        if d in by_day:
            if p.get("state") == "DONE":
                by_day[d]["done"] += 1
                total_done += 1
            else:
                by_day[d]["overdue"] += 1
                total_overdue += 1

    mission_events = db.execute(
        """SELECT payload FROM event
           WHERE family_id=? AND kind='mission_result' AND created_at>=?""",
        (fam["id"], start)).fetchall()

    total_merit = total_points = 0
    for r in mission_events:
        try:
            p = json.loads(r["payload"])
        except Exception:
            p = {}
        total_merit += int(p.get("meritDelta", p.get("merit_delta", 0)) or 0)
        total_points += int(p.get("pointsDelta", p.get("points_delta", 0)) or 0)

    completion_rate = round(total_done / max(1, total_done + total_overdue) * 100, 1)
    return {
        "days": list(by_day.values()),
        "totals": {
            "done": total_done, "overdue": total_overdue,
            "completion_rate": completion_rate,
            "merit": total_merit, "points": total_points,
        },
    }
