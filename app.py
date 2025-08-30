import os
import time
import threading
import logging
import hashlib
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from flask import Flask, jsonify, render_template, request
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import urllib3

# ---- local modules
from db import init_db, SessionLocal, DcCount, AlertSnapshot, utcnow
from reports import build_report_daily, build_report_weekly, build_report_monthly, LOCAL_TZ

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)

GRAFANAS = [
    {
        "name": os.getenv("GRAFANA_NAME_MAIN", "Grafana1"),
        "base_url": os.getenv("GRAFANA_URL_MAIN", "https://grafana.domain.tld/"),
        "token": os.getenv("GRAFANA_TOKEN_MAIN", ""),
        "user": os.getenv("GRAFANA_USER_MAIN", "admin"),
        "password": os.getenv("GRAFANA_PASS_MAIN", "password"),
    },
    {
        "name": os.getenv("GRAFANA_NAME_TBRZ", "Grafana2"),
        "base_url": os.getenv("GRAFANA_URL_TBRZ", "https://grafana2.domain.tld/"),
        "token": os.getenv("GRAFANA_TOKEN_TBRZ", ""),
        "user": os.getenv("GRAFANA_USER_TBRZ", "admin"),
        "password": os.getenv("GRAFANA_PASS_TBRZ", "password"),
    },
    {
        "name": os.getenv("GRAFANA_NAME_SHZ", "Grafana3"),
        "base_url": os.getenv("GRAFANA_URL_SHZ", "https://grafana3.domain.tld/"),
        "token": os.getenv("GRAFANA_TOKEN_SHZ", ""),
        "user": os.getenv("GRAFANA_USER_SHZ", "admin"),
        "password": os.getenv("GRAFANA_PASS_SHZ", "password"),
    },
    {
        "name": os.getenv("GRAFANA_NAME_FN", "Grafana4"),
        "base_url": os.getenv("GRAFANA_URL_FN", "https://grafana4.domain.tld/"),
        "token": os.getenv("GRAFANA_TOKEN_FN", ""),
        "user": os.getenv("GRAFANA_USER_FN", "admin"),
        "password": os.getenv("GRAFANA_PASS_FN", "password"),
    },
]

DC_CANONICAL = ["Tehran", "Shiraz", "Tabriz", "Mashhad", "Esfahan"]
DC_SYNONYMS = {
    "Tehran":  ["tehran", "teh"],
    "Shiraz":  ["shiraz", "siraz", "shz"],
    "Tabriz":  ["tabriz", "tabz", "tbz", "tab"],
    "Mashhad": ["mashhad", "mashhd"],
    "Esfahan":  ["esfahan", "esf", "esf_dc"],
}

FORBIDDEN_MATCHER_NAMES = {"__alert_rule_uid__"}  # NEVER send

POLL_INTERVAL   = 60
REQUEST_TIMEOUT = 120
VERIFY_TLS      = False

FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3"))
FETCH_RETRY_DELAY = float(os.getenv("FETCH_RETRY_DELAY", "5"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alerts")

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "DELETE"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=64)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

http = make_session()
app = Flask(__name__)

CACHE_LOCK = threading.RLock()
CACHE = {"generated_at": None, "by_dc": {}, "sources": [], "last_error": None}

def utc_now_iso(): return datetime.now(timezone.utc).isoformat()
def _lower(s): return str(s or "").lower()

def _auth_headers(g):
    headers, auth = {}, None
    if g.get("token"):
        headers["Authorization"] = f"Bearer {g['token']}"
    elif g.get("user"):
        auth = (g["user"], g.get("password") or "")
    return headers, auth

def _am_urls(base_url, path):
    return (
        urljoin(base_url, "/api/alertmanager/grafana" + path),
        urljoin(base_url, "/api/alertmanager" + path),
    )

def _am_get(g, path, params=None):
    headers, auth = _auth_headers(g)
    last_err = None
    for u in _am_urls(g["base_url"], path):
        try:
            r = http.get(u, headers=headers, auth=auth, params=params,
                         timeout=REQUEST_TIMEOUT, verify=VERIFY_TLS)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            last_err = e
    raise RuntimeError(f"GET failed for {g['name']} {path}: {last_err or 'unknown'}")

def _am_post(g, path, json_body):
    headers, auth = _auth_headers(g)
    last_err = None
    for u in _am_urls(g["base_url"], path):
        try:
            r = http.post(u, headers=headers, auth=auth, json=json_body,
                          timeout=REQUEST_TIMEOUT, verify=VERIFY_TLS)
            if 200 <= r.status_code < 300:
                return r.json() if r.text else {}
        except Exception as e:
            last_err = e
    raise RuntimeError(f"POST failed for {g['name']} {path}: {last_err or 'unknown'}")

def _am_delete(g, path):
    headers, auth = _auth_headers(g)
    last_err = None
    for u in _am_urls(g["base_url"], path):
        try:
            r = http.delete(u, headers=headers, auth=auth,
                            timeout=REQUEST_TIMEOUT, verify=VERIFY_TLS)
            if 200 <= r.status_code < 300:
                return {}
        except Exception as e:
            last_err = e
    raise RuntimeError(f"DELETE failed for {g['name']} {path}: {last_err or 'unknown'}")

def _parse_ts(ts):
    if not ts: return 0.0
    try: return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception: return 0.0

def _parse_dt(ts):
    if not ts: return None
    try: return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception: return None

def alert_key(raw) -> str:
    fp = (raw.get("fingerprint") or "").strip()
    if fp: return fp
    labels = raw.get("labels") or {}
    blob = json.dumps(sorted(labels.items()), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()

def _state_weight(raw) -> int:
    s = ((raw.get("status") or {}).get("state")
         or (raw.get("status") or {}).get("status") or "").lower()
    return {"firing": 3, "active": 2, "suppressed": 1}.get(s, 0)

def _get_alerts(g):
    params = {"active": "true", "inhibited": "false", "silenced": "true"}
    data = _am_get(g, "/api/v2/alerts", params=params)
    if isinstance(data, dict) and "alerts" in data:
        data = data["alerts"]
    return data if isinstance(data, list) else []

def _list_silences(g):
    data = _am_get(g, "/api/v2/silences", params=None)
    return data if isinstance(data, list) else []

def fetch_from_grafana(g):
    last_err = None
    attempts = FETCH_RETRIES + 1
    for i in range(1, attempts + 1):
        try:
            alerts   = _get_alerts(g)
            silences = _list_silences(g)
            for a in alerts:
                a["_source"]   = g["name"]
                a["_base_url"] = g["base_url"]
            for s in silences:
                s["_source"]   = g["name"]
                s["_base_url"] = g["base_url"]
            log.info("Grafana %s: OK (alerts=%d, silences=%d)", g["name"], len(alerts), len(silences))
            return alerts, silences, True, None
        except Exception as e:
            last_err = e
            log.warning("Grafana %s fetch attempt %d/%d failed: %s", g["name"], i, attempts, e)
            if i < attempts:
                time.sleep(FETCH_RETRY_DELAY)
    log.error("Grafana %s: ERROR after %d attempts (%s)", g["name"], attempts, last_err)
    return [], [], False, str(last_err)

def detect_dc(alert) -> set[str]:
    labels = alert.get("labels", {}) or {}
    ann    = alert.get("annotations", {}) or {}
    text = " ".join([
        _lower(labels.get("DC")), _lower(labels.get("dc")),
        _lower(ann.get("summary")), _lower(ann.get("message")),
        _lower(ann.get("description")), _lower(ann.get("body")),
    ])
    found = set()
    dc_label = _lower(labels.get("DC") or labels.get("dc"))
    for canon in DC_CANONICAL:
        if dc_label and dc_label == _lower(canon):
            found.add(canon)
    for canon in DC_CANONICAL:
        for syn in DC_SYNONYMS.get(canon, []):
            if syn in text:
                found.add(canon)
    return found

def aggregate_by_dc(all_alerts, silence_map_by_id):
    grouped_maps = {dc: {} for dc in DC_CANONICAL}
    unassigned_map = {}

    def choose(old, new):
        if old is None: return new
        ow, nw = _state_weight(old), _state_weight(new)
        if nw != ow: return new if nw > ow else old
        return new if _parse_ts(new.get("startsAt")) >= _parse_ts(old.get("startsAt")) else old

    for raw in all_alerts:
        k   = alert_key(raw)
        dcs = detect_dc(raw)
        if dcs:
            for dc in dcs:
                grouped_maps[dc][k] = choose(grouped_maps[dc].get(k), raw)
        else:
            unassigned_map[k] = choose(unassigned_map.get(k), raw)

    def normalize(raw):
        labels = raw.get("labels", {}) or {}
        ann    = raw.get("annotations", {}) or {}
        status = (raw.get("status") or {}).get("state") or "active"
        sil_ids = (raw.get("status") or {}).get("silencedBy") or []
        sil_details = [silence_map_by_id.get(sid) for sid in sil_ids if sid in silence_map_by_id]
        return {
            "alertname":   labels.get("alertname") or labels.get("alert_name") or "unknown",
            "status":      status,
            "labels":      labels,
            "annotations": ann,
            "startsAt":    raw.get("startsAt"),
            "endsAt":      raw.get("endsAt"),
            "fingerprint": raw.get("fingerprint") or "",
            "generatorURL":raw.get("generatorURL") or "",
            "sourceGrafana": raw.get("_source"),
            "sourceBaseURL": raw.get("_base_url"),
            "silencedBy":  sil_ids,
            "silences":    [s for s in sil_details if s],
        }

    grouped = {dc: [normalize(v) for v in grouped_maps[dc].values()] for dc in DC_CANONICAL}
    unassigned = [normalize(v) for v in unassigned_map.values()]

    for dc in grouped:
        grouped[dc].sort(key=lambda x: _parse_ts(x.get("startsAt")), reverse=True)
    unassigned.sort(key=lambda x: _parse_ts(x.get("startsAt")), reverse=True)
    grouped["Unassigned"] = unassigned
    return grouped

def _refresh_now():
    combined_alerts, combined_silences, sources = [], [], []
    for g in GRAFANAS:
        alerts, sils, ok, err = fetch_from_grafana(g)
        combined_alerts.extend(alerts)
        combined_silences.extend(sils)
        sources.append({"name": g["name"], "base_url": g["base_url"], "ok": bool(ok), "error": err})

    silence_map_by_id = {
        s.get("id"): {
            "id": s.get("id"),
            "createdBy": s.get("createdBy"),
            "comment": s.get("comment"),
            "startsAt": s.get("startsAt"),
            "endsAt": s.get("endsAt"),
            "matchers": s.get("matchers") or [],
            "status": (s.get("status") or {}).get("state"),
            "sourceGrafana": s.get("_source"),
            "sourceBaseURL": s.get("_base_url"),
        }
        for s in combined_silences if s.get("id")
    }

    by_dc = aggregate_by_dc(combined_alerts, silence_map_by_id)

    now = utcnow()
    with SessionLocal() as s:
        for dc in DC_CANONICAL + ["Unassigned"]:
            alerts = by_dc.get(dc, [])
            active = sum(1 for a in alerts if not a.get("silences"))
            suppressed = sum(1 for a in alerts if a.get("silences"))
            s.add(DcCount(ts=now, dc=dc, active=active, suppressed=suppressed, total=active+suppressed))
            for a in alerts:
                s.add(AlertSnapshot(
                    ts=now, dc=dc,
                    alertname=a.get("alertname"),
                    status=(a.get("status") or "active"),
                    fingerprint=a.get("fingerprint") or "",
                    source=a.get("sourceGrafana") or "",
                    starts_at=_parse_dt(a.get("startsAt")),
                    ends_at=_parse_dt(a.get("endsAt")),
                    labels=a.get("labels") or {},
                    annotations=a.get("annotations") or {},
                ))
        s.commit()

    with CACHE_LOCK:
        CACHE["generated_at"] = utc_now_iso()
        CACHE["by_dc"] = by_dc
        CACHE["sources"] = sources
        CACHE["last_error"] = None

def refresh_loop():
    while True:
        try:
            _refresh_now()
        except Exception as e:
            with CACHE_LOCK:
                CACHE["last_error"] = f"{utc_now_iso()} - {e}"
            log.exception("Background refresh failed: %s", e)
        time.sleep(POLL_INTERVAL)

def _find_grafana(by_name_or_base):
    for g in GRAFANAS:
        if g["name"] == by_name_or_base or g["base_url"].rstrip("/") == by_name_or_base.rstrip("/"):
            return g
    return None

def create_or_update_silence(g, *, matchers, startsAt, endsAt, comment, createdBy, silence_id=None):
    # delete old then create new (works across Grafana versions)
    if silence_id:
        try:
            _am_delete(g, f"/api/v2/silence/{silence_id}")
        except Exception as e:
            log.warning("Delete existing silence failed (%s): %s", silence_id, e)
    body = {
        "matchers":  matchers,
        "startsAt":  startsAt,
        "endsAt":    endsAt,
        "createdBy": createdBy or "dc-alerts-ui",
        "comment":   comment or "",
    }
    return _am_post(g, "/api/v2/silences", json_body=body)

@app.get("/")
def index():
    return render_template("alerts.html", dc_list=DC_CANONICAL)

@app.get("/api/alerts")
def api_alerts():
    q = _lower(request.args.get("q", "").strip())
    force = request.args.get("force", "0") in ("1", "true", "yes")
    if force:
        _refresh_now()

    with CACHE_LOCK:
        generated_at = CACHE["generated_at"]
        by_dc = CACHE["by_dc"]
        sources = CACHE["sources"]

    if q:
        def ok(a):
            s = " ".join([
                _lower(a.get("alertname")),
                _lower(a.get("annotations", {}).get("summary")),
                _lower(a.get("annotations", {}).get("message")),
                _lower(a.get("annotations", {}).get("description")),
            ])
            return q in s
        by_dc = {dc: [a for a in (by_dc.get(dc) or []) if ok(a)] for dc in by_dc}

    default_by_dc = {dc: [] for dc in DC_CANONICAL}
    default_by_dc["Unassigned"] = []

    return jsonify({
        "generated_at": generated_at or utc_now_iso(),
        "by_dc": by_dc if by_dc else default_by_dc,
        "sources": sources or [],
    })

@app.get("/api/report/daily")
def report_daily():
    now_local = datetime.now(LOCAL_TZ)
    y = int(request.args.get("y", now_local.year))
    m = int(request.args.get("m", now_local.month))
    d = int(request.args.get("d", now_local.day))
    return jsonify(build_report_daily(datetime(y, m, d, tzinfo=LOCAL_TZ)))

@app.get("/api/report/weekly")
def report_weekly():
    now_local = datetime.now(LOCAL_TZ)
    y = int(request.args.get("y", now_local.year))
    m = int(request.args.get("m", now_local.month))
    d = int(request.args.get("d", now_local.day))
    return jsonify(build_report_weekly(datetime(y, m, d, tzinfo=LOCAL_TZ)))

@app.get("/api/report/monthly")
def report_monthly():
    now_local = datetime.now(LOCAL_TZ)
    y = int(request.args.get("y", now_local.year))
    m = int(request.args.get("m", now_local.month))
    return jsonify(build_report_monthly(y, m))

@app.post("/api/silence")
def api_silence():
    data = request.get_json(force=True) or {}
    g = _find_grafana(data.get("grafana", ""))
    if not g:
        return jsonify({"ok": False, "error": "unknown grafana"}), 400

    raw_matchers = data.get("matchers") or []
    matchers = [
        {"name": m.get("name"), "value": str(m.get("value", "")), "isRegex": bool(m.get("isRegex", False))}
        for m in raw_matchers
        if m and m.get("name") not in FORBIDDEN_MATCHER_NAMES
    ]

    # HARD STOP: do not auto-build from labels on the server
    if not matchers:
        return jsonify({"ok": False, "error": "no matchers supplied"}), 400

    startsAt = data.get("startsAt")
    endsAt   = data.get("endsAt")
    now = datetime.now(timezone.utc)
    try:
        s_dt = datetime.fromisoformat(startsAt.replace("Z","+00:00")) if startsAt else now
        e_dt = datetime.fromisoformat(endsAt.replace("Z","+00:00"))   if endsAt   else (s_dt + timedelta(hours=2))
        if e_dt <= s_dt:
            e_dt = s_dt + timedelta(minutes=1)
    except Exception:
        s_dt = now
        e_dt = now + timedelta(hours=2)

    try:
        log.info("Creating silence on %s with matchers=%s", g["name"], matchers)
        res = create_or_update_silence(
            g,
            matchers=matchers,
            startsAt=s_dt.isoformat(),
            endsAt=e_dt.isoformat(),
            comment=data.get("comment") or "",
            createdBy=data.get("createdBy") or "dc-alerts-ui",
            silence_id=data.get("id"),
        )
        _refresh_now()
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/unsilence")
def api_unsilence():
    data = request.get_json(force=True) or {}
    g = _find_grafana(data.get("grafana", ""))
    if not g or not data.get("id"):
        return jsonify({"ok": False, "error": "missing grafana or id"}), 400
    try:
        _am_delete(g, f"/api/v2/silence/{data['id']}")
        _refresh_now()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/healthz")
def healthz():
    with CACHE_LOCK:
        return jsonify({
            "generated_at": CACHE["generated_at"],
            "last_error": CACHE["last_error"],
            "sources": CACHE["sources"],
            "counts": {dc: len(CACHE["by_dc"].get(dc, [])) for dc in (CACHE["by_dc"] or {})},
        })

if __name__ == "__main__":
    init_db()
    missing = [g["name"] for g in GRAFANAS if not (g.get("token") or g.get("user"))]
    if missing:
        log.warning("No token or user set for: %s", ", ".join(missing))
    t = threading.Thread(target=refresh_loop, name="refresh-loop", daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5050, debug=False)

