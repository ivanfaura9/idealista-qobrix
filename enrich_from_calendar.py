#!/usr/bin/env python3
"""
enrich_from_calendar.py - Enriquece contactos en Qobrix con info de Calendar
==============================================================================
Para cada evento en los calendarios de trabajo (Visitas + IF REAL ESTATE):
  1. Extrae nombre (del título) + email + teléfono (de la descripción)
  2. Busca el contacto en Qobrix por email/tel
  3. Si el contacto existe pero le falta:
        - nombre real (es genérico "Lead Idealista" etc.)  -> set first/last
        - email (no lo tiene)                              -> set email
        - teléfono (no lo tiene)                           -> set phone
     ... lo actualiza con la info del Calendar.

Diseñado para correr periódicamente (workflow_dispatch o cron horario).
NO crea contactos nuevos. Solo enriquece los existentes.
"""

import os
import sys
import re
import logging
import socket
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

from if_common import (
    google_access_token,
    gcal_get,
    qobrix_search_contact_by_email,
    QOBRIX_API,
    QOBRIX_HEADERS,
)
from calendar_sync import (
    extract_client_name,
    extract_emails,
    extract_phones,
    normalize_phone,
    qobrix_search_contact_by_phone,
    DEFAULT_CALENDARS,
    is_external_email,
)

socket.setdefaulttimeout(30)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  [enrich] %(message)s",
)
log = logging.getLogger(__name__)

GENERIC_FIRST = {"no", "lead", "cliente", ""}
GENERIC_LAST = {"especificado", "especificado.", "idealista", "fotocasa", "habitaclia",
                "milanuncios", "lead", ""}


def is_generic_name(first, last):
    f = (first or "").strip().lower()
    l = (last or "").strip().lower()
    if f in GENERIC_FIRST:
        return True
    if l in GENERIC_LAST and f in {"no", "lead", "cliente"}:
        return True
    if f == "no" and "especificad" in l:
        return True
    return False


def patch_contact(cid, payload):
    try:
        r = requests.patch(f"{QOBRIX_API}/contacts/{cid}",
                           headers=QOBRIX_HEADERS, json=payload, timeout=30)
        r.raise_for_status()
        return True
    except Exception as exc:
        log.error(f"  PATCH {cid[:8]}: {exc}")
        return False


def enrich_contact(contact, calendar_name, calendar_email, calendar_phone):
    """Decide qué actualizar y manda PATCH si hay algo que mejorar."""
    cid = contact["id"]
    current_first = (contact.get("first_name") or "").strip()
    current_last = (contact.get("last_name") or "").strip()
    current_email = (contact.get("email") or "").strip().lower()
    current_email2 = (contact.get("email_2") or "").strip().lower()
    current_phone = (contact.get("phone") or "").strip()
    current_phone2 = (contact.get("phone_2") or "").strip()
    current_phone3 = (contact.get("phone_3") or "").strip()

    patch = {}
    changes = []

    # 1. Nombre — si es genérico y Calendar tiene uno mejor
    if is_generic_name(current_first, current_last) and calendar_name:
        parts = calendar_name.split()
        new_first = parts[0]
        new_last = " ".join(parts[1:]) if len(parts) > 1 else ""
        if new_first.lower() != current_first.lower() or new_last.lower() != current_last.lower():
            patch["first_name"] = new_first[:100]
            patch["last_name"] = new_last[:100]
            changes.append(f"name '{current_first} {current_last}' -> '{new_first} {new_last}'")

    # 2. Email — si no tiene y Calendar lo trae
    if calendar_email and not current_email and calendar_email.lower() != current_email2:
        patch["email"] = calendar_email
        changes.append(f"email + {calendar_email}")

    # 3. Teléfono — si no tiene phone, y el de Calendar es distinto a phone_2/3
    if calendar_phone:
        existing_normalized = {
            normalize_phone(current_phone),
            normalize_phone(current_phone2),
            normalize_phone(current_phone3),
        }
        cal_n = normalize_phone(calendar_phone)
        if cal_n and cal_n not in existing_normalized:
            if not current_phone:
                patch["phone"] = calendar_phone[:30]
                changes.append(f"phone + {calendar_phone}")
            elif not current_phone2:
                patch["phone_2"] = calendar_phone[:30]
                changes.append(f"phone_2 + {calendar_phone}")
            elif not current_phone3:
                patch["phone_3"] = calendar_phone[:30]
                changes.append(f"phone_3 + {calendar_phone}")

    if not patch:
        return False

    log.info(f"  Enriquecer {cid[:8]}: {' | '.join(changes)}")
    return patch_contact(cid, patch)


def main():
    if not os.environ.get("GOOGLE_REFRESH_TOKEN"):
        log.info("Sin GOOGLE_REFRESH_TOKEN, salgo.")
        return 0
    try:
        access_token = google_access_token()
    except Exception as exc:
        log.error(f"Token Google fallo: {exc}")
        return 1

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=7)).isoformat()
    time_max = (now + timedelta(days=21)).isoformat()

    enriched = scanned = no_contact = 0

    for cal_id in DEFAULT_CALENDARS:
        path = "/calendars/" + urllib.parse.quote(cal_id, safe="") + "/events"
        try:
            data = gcal_get(path, params={
                "timeMin": time_min, "timeMax": time_max,
                "singleEvents": "true", "orderBy": "startTime",
                "maxResults": "100",
            }, access_token=access_token)
        except Exception as exc:
            log.error(f"GCal err {cal_id[:20]}: {exc}")
            continue

        items = data.get("items", []) or []
        log.info(f"Calendar '{data.get('summary',cal_id[:30])}': {len(items)} eventos en ventana -7d..+21d")

        for ev in items:
            if ev.get("status") == "cancelled":
                continue
            scanned += 1
            title = ev.get("summary", "")
            desc = ev.get("description", "") or ""

            # Datos del Calendar
            cal_name = extract_client_name(title)
            cal_emails = extract_emails(desc)
            cal_phones = extract_phones(desc)
            # Email del attendee externo (si lo hay)
            for a in (ev.get("attendees") or []):
                ae = (a.get("email") or "").strip()
                if is_external_email(ae) and ae not in cal_emails:
                    cal_emails.append(ae)

            # Buscar contacto por cualquiera de las pistas
            contact = None
            for em in cal_emails:
                contact = qobrix_search_contact_by_email(em)
                if contact: break
            if not contact:
                for ph in cal_phones:
                    contact = qobrix_search_contact_by_phone(ph)
                    if contact: break
            if not contact:
                no_contact += 1
                continue

            # Enriquecer si procede
            cal_email = cal_emails[0] if cal_emails else None
            cal_phone = cal_phones[0] if cal_phones else None
            if enrich_contact(contact, cal_name, cal_email, cal_phone):
                enriched += 1

    log.info(f"Resumen: scanned={scanned} | enriched={enriched} | sin contacto Qobrix={no_contact}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
