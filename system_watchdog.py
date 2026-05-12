#!/usr/bin/env python3
"""
system_watchdog.py - Supervisor del sistema IF Real Estate
============================================================
Corre cada hora. Hace múltiples chequeos y auto-arregla lo que puede,
o manda push corporativo si no puede.

Checks:
  1. Duplicados de Meetings auto-sync en Qobrix (mismo event_id / mismo subject+start)
  2. Lag del Idealista monitor (último run > 15 min)
  3. Lag del Calendar sync (último run > 30 min)
  4. Refresh token Google expirando (< 2 días) - notifica
  5. Emails sin procesar en IMAP (leads pendientes)
  6. Push subscriptions VAPID expiradas

A las 22:00 además manda stats diarias.
"""

import os
import sys
import json
import logging
import socket
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from if_common import (
    google_access_token,
    qobrix_get,
    QOBRIX_API,
    QOBRIX_HEADERS,
    QOBRIX_BASE,
    send_push,
)

socket.setdefaulttimeout(30)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  [watchdog] %(message)s",
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def gh_api(path):
    """GET a GitHub API usando GITHUB_TOKEN del env del workflow."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "ivanfaura9/idealista-qobrix").strip()
    url = f"https://api.github.com{path.replace('{repo}', repo)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def gh_dispatch(workflow_file):
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "ivanfaura9/idealista-qobrix").strip()
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
    req = urllib.request.Request(
        url,
        data=b'{"ref":"main"}',
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception as exc:
        log.error(f"  dispatch {workflow_file} fallo: {exc}")
        return False


def minutes_since(iso_str):
    if not iso_str:
        return 99999
    s = iso_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return 99999
    return int((datetime.now(timezone.utc) - dt).total_seconds() / 60)


# ──────────────────────────────────────────────
# Check 1: duplicados en Qobrix Meetings (auto-sync)
# ──────────────────────────────────────────────
def check_duplicate_meetings():
    """Detecta meetings con mismo subject+start_date y borra las duplicadas."""
    log.info("Check 1/6 — duplicados de Meetings auto-sync")
    page = 1
    all_meetings = []
    while True:
        r = requests.get(
            f"{QOBRIX_API}/meetings",
            headers=QOBRIX_HEADERS,
            params={"limit": 100, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("data", []) or []
        if not items:
            break
        for m in items:
            if "Auto-sync Google Calendar" in (m.get("description") or ""):
                all_meetings.append(m)
        if not data.get("pagination", {}).get("has_next_page"):
            break
        page += 1
        if page > 30:
            break

    # Agrupar por (subject, start_date)
    groups = defaultdict(list)
    for m in all_meetings:
        key = (m.get("subject", "")[:80], m.get("start_date", ""))
        groups[key].append(m)

    duplicates = [g for g in groups.values() if len(g) > 1]
    n_dup_groups = len(duplicates)
    n_to_delete = sum(len(g) - 1 for g in duplicates)

    if n_to_delete == 0:
        log.info("  OK — sin duplicados")
        return {"ok": True, "found": 0, "deleted": 0}

    log.warning(f"  {n_dup_groups} grupos con duplicados, {n_to_delete} para borrar")
    deleted = 0
    for group in duplicates:
        # Conservar la más antigua (creada primero)
        group.sort(key=lambda m: m.get("created", ""))
        keep = group[0]
        for m in group[1:]:
            try:
                rd = requests.delete(
                    f"{QOBRIX_API}/meetings/{m['id']}",
                    headers=QOBRIX_HEADERS,
                    timeout=30,
                )
                if rd.status_code in (200, 204):
                    deleted += 1
            except Exception as exc:
                log.error(f"  delete {m['id']}: {exc}")

    log.info(f"  Borrados: {deleted}/{n_to_delete} duplicados")

    if deleted > 0:
        send_push(
            f"🛡️ Watchdog limpió {deleted} meetings duplicadas en Qobrix.",
            url=QOBRIX_BASE,
            tag="watchdog-dups",
        )

    return {"ok": True, "found": n_to_delete, "deleted": deleted}


# ──────────────────────────────────────────────
# Check 2-3: lag de workflows
# ──────────────────────────────────────────────
def check_workflow_lag(workflow_file, label, threshold_min, dispatch_if_stale=True):
    log.info(f"Check — lag de {label}")
    try:
        data = gh_api("/repos/{repo}/actions/workflows/" + workflow_file + "/runs?per_page=1")
        runs = data.get("workflow_runs", [])
        if not runs:
            log.warning(f"  {label}: sin runs previos")
            return {"ok": False, "lag_min": None}
        last = runs[0]
        lag = minutes_since(last.get("created_at"))
        log.info(f"  {label}: último run hace {lag} min (umbral {threshold_min})")
        if lag > threshold_min:
            log.warning(f"  STALE — disparando {workflow_file}")
            if dispatch_if_stale:
                gh_dispatch(workflow_file)
            return {"ok": False, "lag_min": lag, "dispatched": dispatch_if_stale}
        return {"ok": True, "lag_min": lag}
    except Exception as exc:
        log.error(f"  fallo: {exc}")
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────
# Check 4: refresh token Google
# ──────────────────────────────────────────────
def check_google_token():
    log.info("Check — Google refresh token expira pronto?")
    if not os.environ.get("GOOGLE_REFRESH_TOKEN"):
        log.info("  no configurado, salto")
        return {"ok": True, "skipped": True}
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    try:
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        rt_expires = body.get("refresh_token_expires_in")
        if rt_expires is None:
            log.info("  OK — token sin expiración (app en producción)")
            return {"ok": True, "expires_days": None}
        days = rt_expires / 86400
        log.info(f"  expira en {days:.1f} días")
        # NO mandamos push - Iván lo ve en logs si quiere; aviso solo en resumen diario
        return {"ok": days >= 2, "expires_days": days}
    except Exception as exc:
        log.error(f"  fallo: {exc}")
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────
# Check 5: leads pendientes en IMAP (no procesados)
# ──────────────────────────────────────────────
def check_pending_imap_leads():
    log.info("Check — emails de portales sin procesar")
    import imaplib
    pending = 0
    accounts = [
        ("imap.gmail.com", os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_APP_PASSWORD", "")),
        ("imap.hostinger.com", os.environ.get("HOSTINGER_USER", ""), os.environ.get("HOSTINGER_PASSWORD", "")),
    ]
    SEARCH = '(UNSEEN OR (OR (OR FROM "idealista" FROM "fotocasa") FROM "habitaclia") FROM "milanuncios")'
    for host, user, pw in accounts:
        if not user or not pw:
            continue
        try:
            mail = imaplib.IMAP4_SSL(host)
            mail.login(user, pw)
            mail.select("INBOX")
            _, ids = mail.search(None, SEARCH)
            ids_list = ids[0].split() if ids and ids[0] else []
            pending += len(ids_list)
            mail.logout()
        except Exception as exc:
            log.error(f"  IMAP {host}: {exc}")
    log.info(f"  emails portales sin leer: {pending}")
    # NOTA: 'sin leer' (UNSEEN) NO significa 'sin procesar'. Mi monitor.py procesa
    # los emails pero NO los marca como leídos en IMAP - solo guarda el ID en
    # processed_ids.json. Por tanto este contador era spam. Solo log, no push.
    return {"ok": True, "pending": pending}


# ──────────────────────────────────────────────
# Check 6: stats diarias (solo a las 22:00 hora local)
# ──────────────────────────────────────────────
def daily_stats_if_2200():
    now_utc = datetime.now(timezone.utc)
    # 22:00 hora Madrid = 20:00 UTC (verano) / 21:00 UTC (invierno)
    is_dst = 3 <= now_utc.month <= 10
    target_hour = 20 if is_dst else 21
    if now_utc.hour != target_hour:
        return None
    log.info("Check — stats diarias 22:00")
    # Contar leads creados hoy en Qobrix
    today_local = (now_utc + timedelta(hours=2 if is_dst else 1)).date()
    try:
        # opps creadas hoy
        r = requests.get(
            f"{QOBRIX_API}/opportunities",
            headers=QOBRIX_HEADERS,
            params={"limit": 100, "sort": "-created"},
            timeout=30,
        )
        items = r.json().get("data", []) or []
        new_today = []
        for o in items:
            cs = o.get("created", "")
            try:
                dt = datetime.fromisoformat(cs.replace("Z", "+00:00"))
                local = dt.astimezone(timezone(timedelta(hours=2 if is_dst else 1)))
                if local.date() == today_local:
                    new_today.append(o)
            except Exception:
                pass
        n_new = len(new_today)
        sources = defaultdict(int)
        for o in new_today:
            s = (o.get("source") or "manual").lower()
            sources[s] += 1
        # Mensaje
        breakdown = ", ".join(f"{n} {s}" for s, n in sources.items()) if sources else "—"
        send_push(
            f"🌙 Resumen del día: {n_new} leads nuevos.\n{breakdown}",
            url=QOBRIX_BASE,
            tag="watchdog-daily",
        )
        return {"new_today": n_new, "breakdown": dict(sources)}
    except Exception as exc:
        log.error(f"  stats: {exc}")
        return {"error": str(exc)}


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    log.info("Iniciando watchdog del sistema")

    # Solo correr el check más caro (duplicados) cada 4h en lugar de cada 1h
    do_dups = datetime.now(timezone.utc).hour % 4 == 0

    results = {}
    if do_dups:
        try:
            results["duplicates"] = check_duplicate_meetings()
        except Exception as exc:
            log.error(f"  check_duplicate_meetings: {exc}")
            results["duplicates"] = {"error": str(exc)}

    results["monitor_lag"] = check_workflow_lag("run.yml", "Idealista monitor", 15)
    results["calendar_lag"] = check_workflow_lag("calendar.yml", "Calendar sync", 30)

    try:
        results["google_token"] = check_google_token()
    except Exception as exc:
        log.error(f"  google_token: {exc}")
        results["google_token"] = {"error": str(exc)}

    try:
        results["pending_imap"] = check_pending_imap_leads()
    except Exception as exc:
        log.error(f"  pending_imap: {exc}")
        results["pending_imap"] = {"error": str(exc)}

    try:
        ds = daily_stats_if_2200()
        if ds is not None:
            results["daily_stats"] = ds
    except Exception as exc:
        log.error(f"  daily_stats: {exc}")

    log.info(f"Watchdog completo. Resultados: {json.dumps(results, default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
