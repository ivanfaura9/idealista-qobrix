#!/usr/bin/env python3
"""
calendar_sync.py - Google Calendar -> Qobrix Meetings
======================================================
Cada 15 min:
  1. Lee eventos de los proximos 14 dias del calendario primario.
  2. Para cada evento con attendees externos:
     - Busca el contacto en Qobrix por email.
     - Si existe, crea o actualiza una Reunion en Qobrix vinculada a ese contacto.
  3. Mantiene synced_meetings.json para no duplicar.

NO toca contactos/oportunidades existentes - solo crea/actualiza Meetings.
NO escribe en Google Calendar (scope readonly).
"""

import os
import sys
import json
import logging
import socket
from datetime import datetime, timedelta, timezone

from if_common import (
    google_access_token,
    gcal_get,
    qobrix_post,
    qobrix_patch,
    qobrix_search_contact_by_email,
)

socket.setdefaulttimeout(30)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  [calendar_sync] %(message)s",
)
log = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SYNCED_FILE = os.path.join(SCRIPT_DIR, "synced_meetings.json")

OWNER_USER_ID = os.environ.get("OWNER_USER_ID", "").strip()


def load_synced():
    if os.path.exists(SYNCED_FILE):
        try:
            with open(SYNCED_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_synced(data):
    with open(SYNCED_FILE, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def is_external_email(addr):
    """Filtra emails que no son del propio Ivan / del bot / de Google."""
    if not addr:
        return False
    a = addr.lower()
    SKIP = (
        "ivanfaurar",
        "ifrealestate",
        "noreply",
        "no-reply",
        "calendar-notification",
        "@google.com",
        "@resource.calendar.google.com",
        "@group.v.calendar.google.com",
    )
    return not any(s in a for s in SKIP)


def fmt_time(rfc3339):
    """Convierte ISO RFC3339 a 'HH:MM dd/mm' en Europe/Madrid (mejor effort)."""
    try:
        # Permite "2026-05-05T10:40:00+02:00" o "Z"
        s = rfc3339.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        # Si viene con tzinfo, convertimos a +02:00 (CEST mayo)
        if dt.tzinfo:
            dt = dt.astimezone(timezone(timedelta(hours=2)))
        return dt.strftime("%H:%M %d/%m")
    except Exception:
        return rfc3339


def upsert_meeting(event, contact, synced):
    """Crea o actualiza Meeting en Qobrix para este evento + contacto."""
    event_id = event["id"]
    contact_id = contact.get("id") or contact.get("contact_id") or contact.get("contact_name")
    if not contact_id:
        log.warning(f"  Contacto sin id, salto: {contact}")
        return

    start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
    end = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
    if not start or not end:
        return

    summary = event.get("summary", "(sin titulo)")
    location = event.get("location", "")
    description = event.get("description", "")

    payload = {
        "subject": summary[:200],
        "description": description[:1000] if description else "Sincronizado desde Google Calendar",
        "location": location[:200],
        "start_date": start,
        "end_date": end,
        "contact_name": contact_id,
    }
    if OWNER_USER_ID:
        payload["assigned_to"] = OWNER_USER_ID
        payload["owner"] = OWNER_USER_ID

    qobrix_id = synced.get(event_id)
    try:
        if qobrix_id:
            qobrix_patch(f"/meetings/{qobrix_id}", payload)
            log.info(f"  Meeting actualizada: {summary} ({fmt_time(start)})")
        else:
            r = qobrix_post("/meetings", payload)
            new_id = (r.get("data") or {}).get("id") or r.get("id")
            if new_id:
                synced[event_id] = new_id
                log.info(f"  Meeting creada: {summary} ({fmt_time(start)}) -> {new_id}")
            else:
                log.warning(f"  Meeting POST sin id en respuesta: {r}")
    except Exception as exc:
        log.error(f"  Fallo upsert meeting '{summary}': {exc}")


def main():
    if not os.environ.get("GOOGLE_REFRESH_TOKEN"):
        log.info("Sin GOOGLE_REFRESH_TOKEN. Salgo limpiamente.")
        return 0

    try:
        access_token = google_access_token()
    except Exception as exc:
        log.error(f"No se pudo refrescar token Google: {exc}")
        return 1

    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=14)).isoformat()

    try:
        events = gcal_get(
            "/calendars/primary/events",
            params={
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": "100",
            },
            access_token=access_token,
        )
    except Exception as exc:
        log.error(f"GCal API fallo: {exc}")
        return 1

    items = events.get("items", [])
    log.info(f"GCal devuelve {len(items)} eventos en proximos 14 dias")

    synced = load_synced()
    matched = 0

    for ev in items:
        if ev.get("status") == "cancelled":
            # si la teniamos sincronizada habria que borrarla; por ahora dejamos log
            if ev["id"] in synced:
                log.info(f"  Evento cancelado: {ev.get('summary','?')} (Qobrix conserva la meeting)")
            continue

        attendees = ev.get("attendees", []) or []
        external = [a for a in attendees if is_external_email(a.get("email", ""))]

        if not external:
            # Sin attendees externos -> no es una visita con cliente; saltar
            continue

        # Probar a matchear cada attendee externo con un contacto en Qobrix
        for att in external:
            email_addr = att.get("email", "").strip()
            contact = qobrix_search_contact_by_email(email_addr)
            if contact:
                matched += 1
                upsert_meeting(ev, contact, synced)
                break  # un evento, una meeting (con primer contacto matched)
        else:
            log.info(
                f"  Evento '{ev.get('summary','?')}' tiene attendees externos pero "
                f"ninguno coincide con contacto Qobrix: {[a.get('email') for a in external]}"
            )

    save_synced(synced)
    log.info(f"Resumen: {matched} eventos sincronizados con contactos Qobrix")
    return 0


if __name__ == "__main__":
    sys.exit(main())
