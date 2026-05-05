#!/usr/bin/env python3
"""
daily_briefing.py - Push matinal con la agenda del dia (07:30 hora local)
==========================================================================
Lee los calendarios de TRABAJO de Iván para HOY (00:00 - 23:59 Europe/Madrid):
  - Visitas propiedades
  - IF REAL ESTATE (llamadas con clientes)
  - Valoracion propiedad

Enumera visitas/llamadas/valoraciones y manda 1 push corporativo con resumen:

  ☀️ 4 eventos hoy:
  10:40 Visita piso Pallaresa con Ana
  12:00 📞Llamada con Laura Nonell
  14:00 Captación piso venta Emilio
  16:30 Visita piso ... con Joan

Si no hay eventos, manda push con "Sin visitas hoy. Buen dia."
"""

import os
import sys
import json
import logging
import socket
import urllib.parse
from datetime import datetime, timedelta, timezone

from if_common import (
    google_access_token,
    gcal_get,
    send_push,
    QOBRIX_BASE,
)

socket.setdefaulttimeout(30)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  [daily_briefing] %(message)s",
)
log = logging.getLogger(__name__)

# Mismo set de calendarios que calendar_sync.py
# (Valoracion propiedad excluido - lo gestiona GHL)
DEFAULT_CALENDARS = [
    "a2d83dc57c44b7d82c7c1f6e3c5d173b472e27e5fd41b2596e9a0dd4a2b365a0@group.calendar.google.com",
    "8f7ebb4a3a6a4bb627446f87d2b6f0665dc2949803bb3efb2d6d5633e6045114@group.calendar.google.com",
]


def calendars_to_read():
    raw = os.environ.get("CALENDARS_TO_SYNC", "").strip()
    if not raw:
        return DEFAULT_CALENDARS
    try:
        d = json.loads(raw)
        if isinstance(d, list) and d:
            return d
    except Exception:
        pass
    return DEFAULT_CALENDARS


def madrid_tz():
    m = datetime.utcnow().month
    return timezone(timedelta(hours=2 if 3 <= m <= 10 else 1))


def fmt_hhmm(rfc3339):
    try:
        s = rfc3339.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo:
            dt = dt.astimezone(madrid_tz())
        return dt.strftime("%H:%M")
    except Exception:
        return "--:--"


def main():
    if not os.environ.get("GOOGLE_REFRESH_TOKEN"):
        log.info("Sin GOOGLE_REFRESH_TOKEN. Salgo.")
        return 0

    try:
        token = google_access_token()
    except Exception as exc:
        log.error(f"Token Google fallo: {exc}")
        return 1

    tz = madrid_tz()
    today_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = today_local + timedelta(days=1)
    time_min = today_local.isoformat()
    time_max = end_local.isoformat()

    all_events = []
    for cal_id in calendars_to_read():
        cal_path = "/calendars/" + urllib.parse.quote(cal_id, safe="") + "/events"
        try:
            data = gcal_get(
                cal_path,
                params={
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": "30",
                },
                access_token=token,
            )
            for e in data.get("items", []):
                if e.get("status") != "cancelled":
                    all_events.append(e)
        except Exception as exc:
            log.error(f"GCal API fallo en {cal_id[:30]}...: {exc}")

    # Ordenar por start time
    def start_key(e):
        return e.get("start", {}).get("dateTime") or e.get("start", {}).get("date") or ""
    all_events.sort(key=start_key)

    log.info(f"Eventos hoy en calendarios de trabajo: {len(all_events)}")

    if not all_events:
        send_push("☀️ Buen dia. Sin visitas ni llamadas hoy.", url=QOBRIX_BASE, tag="briefing")
        return 0

    n = len(all_events)
    header = "☀️ 1 evento hoy:" if n == 1 else f"☀️ {n} eventos hoy:"
    lines = [header]
    for ev in all_events[:6]:  # tope 6 lineas extra
        start = start_key(ev)
        title = (ev.get("summary") or "(sin titulo)").strip()
        if len(title) > 55:
            title = title[:52] + "..."
        lines.append(f"{fmt_hhmm(start)} {title}")
    if n > 6:
        lines.append(f"+{n - 6} mas")

    send_push("\n".join(lines), url=QOBRIX_BASE + "/crm/calendar", tag="briefing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
