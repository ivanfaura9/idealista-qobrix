#!/usr/bin/env python3
"""
daily_briefing.py - Push matinal con la agenda del dia (07:30 hora local)
==========================================================================
Lee Google Calendar primario para HOY (00:00 - 23:59 Europe/Madrid).
Enumera visitas/eventos con clientes y manda 1 push corporativo con resumen:

  📅 3 visitas hoy:
  10:40 Ana — piso ref 1093
  16:00 Joan Molina (revision)
  18:30 Marta Soler — Bassegoda 21

Si no hay eventos, manda push con "Sin visitas hoy. Buen dia."
"""

import os
import sys
import logging
import socket
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

# Europe/Madrid es UTC+2 en CEST (verano), UTC+1 en CET (invierno).
# Usamos offset basado en mes para evitar dependencia externa.
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

    try:
        events = gcal_get(
            "/calendars/primary/events",
            params={
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": "30",
            },
            access_token=token,
        )
    except Exception as exc:
        log.error(f"GCal API fallo: {exc}")
        return 1

    items = [e for e in events.get("items", []) if e.get("status") != "cancelled"]
    log.info(f"Eventos hoy: {len(items)}")

    if not items:
        send_push("☀️ Buen dia. Sin visitas hoy.", url=QOBRIX_BASE, tag="briefing")
        return 0

    n = len(items)
    if n == 1:
        header = "☀️ 1 evento hoy:"
    else:
        header = f"☀️ {n} eventos hoy:"

    lines = [header]
    for ev in items[:5]:  # tope 5 para que el push no sea kilometrico
        start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
        title = ev.get("summary", "(sin titulo)").strip()
        # Resumen corto
        if len(title) > 60:
            title = title[:57] + "..."
        lines.append(f"{fmt_hhmm(start)} {title}")

    if n > 5:
        lines.append(f"+{n - 5} mas")

    send_push("\n".join(lines), url=QOBRIX_BASE + "/crm/calendar", tag="briefing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
