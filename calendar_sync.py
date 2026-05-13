#!/usr/bin/env python3
"""
calendar_sync.py - Google Calendar -> Qobrix Meetings (multi-calendar, multi-match)
====================================================================================
Cada 15 min:
  1. Lee eventos de los proximos 14 dias en una lista de calendarios de trabajo:
       - Visitas propiedades
       - IF REAL ESTATE   (llamadas con clientes)
       - Valoracion propiedad
  2. Para cada evento:
     a) Si tiene attendee externo con email -> busca contacto en Qobrix por email
     b) Si no, extrae el nombre del cliente del titulo y lo busca en Qobrix por nombre
        Patrones soportados:
            "Visita ... con NOMBRE [APELLIDOS]"
            "Llamada con NOMBRE [APELLIDOS]"
            "Captacion ... NOMBRE"
            (emojis al inicio se ignoran)
     c) Encuentre o no contacto, crea/actualiza la Meeting en Qobrix con el titulo
        original (si no hay match, sin contact_name vinculado).
  3. Mantiene synced_meetings.json para no duplicar.

NO toca contactos/oportunidades existentes - solo crea/actualiza Meetings.
NO escribe en Google Calendar (scope readonly).
"""

import os
import re
import sys
import json
import logging
import socket
import urllib.parse
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

from if_common import (
    google_access_token,
    gcal_get,
    qobrix_get,
    qobrix_post,
    qobrix_patch,
    qobrix_search_contact_by_email,
    QOBRIX_API,
    QOBRIX_HEADERS,
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

# IDs de los calendarios de trabajo a sincronizar.
# IMPORTANTE: El calendar "Valoracion propiedad" NO se incluye porque
# lo gestiona GHL automaticamente al entrar nuevos propietarios por campañas.
# Tocarlo aqui crearia duplicados.
# (Override via env var CALENDARS_TO_SYNC con JSON list para añadir/quitar.)
DEFAULT_CALENDARS = [
    # Visitas propiedades
    "a2d83dc57c44b7d82c7c1f6e3c5d173b472e27e5fd41b2596e9a0dd4a2b365a0@group.calendar.google.com",
    # IF REAL ESTATE (llamadas con clientes)
    "8f7ebb4a3a6a4bb627446f87d2b6f0665dc2949803bb3efb2d6d5633e6045114@group.calendar.google.com",
]


def calendars_to_sync():
    raw = os.environ.get("CALENDARS_TO_SYNC", "").strip()
    if not raw:
        return DEFAULT_CALENDARS
    try:
        data = json.loads(raw)
        if isinstance(data, list) and data:
            return data
    except Exception:
        log.warning(f"  CALENDARS_TO_SYNC invalido, usando default: {raw[:60]}")
    return DEFAULT_CALENDARS


# ──────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────
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


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def is_external_email(addr):
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
    try:
        s = rfc3339.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo:
            dt = dt.astimezone(timezone(timedelta(hours=2)))
        return dt.strftime("%H:%M %d/%m")
    except Exception:
        return rfc3339


# ──────────────────────────────────────────────
# Extraccion del nombre del cliente desde el titulo
# ──────────────────────────────────────────────
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2700-\u27BF]",
    flags=re.UNICODE,
)
TITLE_NOISE = re.compile(
    r"\b(visita|piso|venta|alquiler|llamada|captaci[oó]n|valoraci[oó]n|reuni[oó]n|"
    r"firma|cita|contrato|propiedad|inmueble|cliente|propietario|"
    r"compra|alquiler|pareja|familia|sr|sra|don|don[ñn]a)\b",
    flags=re.IGNORECASE,
)


def clean_emojis(s):
    return EMOJI_RE.sub("", s).strip()


def extract_client_name(title):
    """Devuelve el nombre del cliente extraido del titulo del evento, o None."""
    if not title:
        return None
    s = clean_emojis(title)
    s = re.sub(r"^\d+[\s\.\-]*", "", s)  # "2 visita ..." -> "visita ..."

    # Patron 1: ".. con NOMBRE..."
    m = re.search(r"\bcon\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\wÁÉÍÓÚÜÑáéíóúüñ\s\.\-']+)", s, re.I)
    candidate = None
    if m:
        candidate = m.group(1)
    else:
        # Patron 2: "Captacion ... NOMBRE" / "Valoracion ... NOMBRE" - tomar resto tras la palabra clave
        m = re.search(
            r"(?:captaci[oó]n|valoraci[oó]n)[^\w]*(.+)$",
            s,
            re.I,
        )
        if m:
            candidate = m.group(1)

    if not candidate:
        return None

    # Cortar en separadores tipicos
    candidate = re.split(r"[(\[/–—]", candidate)[0]
    # "Albert y alexia" -> "Albert"
    candidate = re.split(r"\s+(?:y|e)\s+", candidate, maxsplit=1)[0]
    # Quitar palabras "ruido" del final ("piso", etc) si quedaron
    parts = [p for p in candidate.split() if not TITLE_NOISE.fullmatch(p)]
    if not parts:
        return None
    # Tope 3 tokens (nombre + 2 apellidos max)
    parts = parts[:3]
    name = " ".join(parts).strip(" .,-")
    if len(name) < 2:
        return None
    # Capitalize correctamente: "jaume cortina" -> "Jaume Cortina"
    name = " ".join(w.capitalize() if w.isalpha() else w for w in name.split())
    return name


def normalize(s):
    """Normaliza para comparacion: minusculas, sin acentos, sin espacios extra."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().strip()


# ──────────────────────────────────────────────
# Extraccion de email/telefono desde el cuerpo del evento
# ──────────────────────────────────────────────
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Acepta: 612345678, 612 34 56 78, +34 612 34 56 78, +34612345678, etc.
PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s\-]?)?(?:\d[\s\-]?){8,13}\d")


def extract_emails(text):
    if not text:
        return []
    return list(dict.fromkeys(m.group(0).strip() for m in EMAIL_RE.finditer(text)))


def normalize_phone(s):
    """Devuelve solo digitos del numero, ignorando prefijo +XX si lo hay (mantiene los ultimos 9 digitos)."""
    digits = re.sub(r"\D", "", s or "")
    if len(digits) >= 9:
        return digits[-9:]  # ultimos 9 -> compara nacional
    return digits


def extract_phones(text):
    if not text:
        return []
    found = []
    for m in PHONE_RE.finditer(text):
        raw = m.group(0)
        nat = normalize_phone(raw)
        if len(nat) == 9 and nat[0] in "6789":  # móviles/fijos ES
            found.append(nat)
    return list(dict.fromkeys(found))


def qobrix_search_contact_by_phone(phone_nat):
    """Busca contacto en Qobrix por phone, phone_2 o phone_3 (ultimos 9 digitos).
    Qobrix usa 'contains' (no 'like'). Probamos varias variantes habituales."""
    if not phone_nat or len(phone_nat) != 9:
        return None
    # Variantes a probar: solo 9 digitos, con +34 prefix, con prefix sin espacios
    variants = [
        phone_nat,                    # 628209558
        f"+34{phone_nat}",            # +34628209558
        f"+34 {phone_nat}",           # +34 628209558
    ]
    seen_ids = set()
    candidates = []
    for v in variants:
        try:
            params = {
                "search": f'phone contains "{v}" or phone_2 contains "{v}" or phone_3 contains "{v}"',
                "limit": "5",
            }
            url = QOBRIX_API + "/contacts?" + urllib.parse.urlencode(params, safe='="')
            r = requests.get(url, headers=QOBRIX_HEADERS, timeout=30)
            r.raise_for_status()
            for c in (r.json().get("data") or []):
                if c.get("id") and c["id"] not in seen_ids:
                    seen_ids.add(c["id"])
                    candidates.append(c)
        except Exception as exc:
            log.warning(f"  qobrix_search_by_phone variant {v!r}: {exc}")
    # Confirmar normalizando los telefonos del candidato
    for c in candidates:
        for f in ("phone", "phone_2", "phone_3"):
            if normalize_phone(c.get(f, "") or "") == phone_nat:
                return c
    return None


# ──────────────────────────────────────────────
# Match por nombre en Qobrix
# ──────────────────────────────────────────────
def search_contact_by_name(name):
    """Devuelve el primer contacto Qobrix cuyo nombre coincida (best effort).

    Estrategia:
    - parts = name.split() -> first, last (si lo hay)
    - intenta '== "first"' y filtra resultados por last_name
    - normaliza con accent-folding
    """
    if not name:
        return None
    parts = [p for p in name.split() if p]
    first = parts[0]
    last = parts[-1] if len(parts) > 1 else ""

    try:
        # Buscar todos los contactos con ese first_name (tolerante a casing diferente
        # devolveremos uno que matchee tras normalizar)
        params = {
            "search": f'first_name == "{first}"',
            "limit": "20",
            "fields[]": ["first_name", "last_name", "id"],
        }
        url = QOBRIX_API + "/contacts?" + urllib.parse.urlencode(params, safe='="', doseq=True)
        r = requests.get(url, headers=QOBRIX_HEADERS, timeout=30)
        r.raise_for_status()
        contacts = r.json().get("data", []) or []
    except Exception as exc:
        log.warning(f"  search_contact_by_name '{name}': {exc}")
        return None

    if not contacts:
        return None

    # Si hay last name, filtrar por ese
    if last:
        last_norm = normalize(last)
        for c in contacts:
            if normalize(c.get("last_name", "")) == last_norm:
                return c
        # Tolerar coincidencia parcial (apellido contiene)
        for c in contacts:
            if last_norm and last_norm in normalize(c.get("last_name", "")):
                return c

    # Sin last o no encontrado, devolver primer match (si hay 1 unico)
    if len(contacts) == 1:
        return contacts[0]
    # Si hay varios y no podemos distinguir, devolver None para evitar falsos positivos
    log.info(f"  Match ambiguo por nombre '{name}': {len(contacts)} candidatos, omito link")
    return None


# ──────────────────────────────────────────────
# Extraccion de pistas de PROPIEDAD desde el titulo
# ──────────────────────────────────────────────
PROPERTY_NOISE = {
    "visita","piso","casa","atico","ático","duplex","dúplex","local","oficina",
    "venta","alquiler","vendido","con","sobre","de","del","en","el","la","los","las",
    "y","e","ref","referencia","2","3","captacion","captación","valoracion","valoración",
    "llamada","reunion","reunión","firma","tour","tour:","cita",
}


def extract_property_hint(title):
    """Devuelve (ref, street_keyword) extraídos del título del evento.
    - ref: número 2-7 dígitos si aparece 'ref XXX' / 'referencia XXX'
    - street_keyword: la palabra significativa (ej. 'Pallaresa', 'Bassegoda')
      que viene entre 'piso ...' y 'con NOMBRE' (o final).
    """
    if not title:
        return None, None
    s = clean_emojis(title)
    s = re.sub(r"^\d+[\s\.\-]*", "", s)  # "2 visita ..." -> "visita ..."

    ref = None
    m = re.search(r"\b(?:ref(?:erencia)?|c[oó]digo)[:\s\.]+(\d{2,7})", s, re.I)
    if m:
        ref = m.group(1)

    # Trozo entre "piso/casa/...." y "con NOMBRE" / fin
    street_kw = None
    m = re.search(
        r"(?:piso|casa|atico|ático|duplex|d[úu]plex|local|oficina|chalet|loft)\b"
        r"(?:\s+(?:venta|alquiler|de\s+venta|de\s+alquiler|en\s+venta|en\s+alquiler))?\s+"
        r"(.+?)(?:\s+con\s+|\s+ref\b|$)",
        s,
        re.I,
    )
    if m:
        chunk = m.group(1).strip()
        # Filtrar palabras "ruido"
        tokens = [t for t in re.split(r"[\s,]+", chunk) if t]
        clean = []
        for t in tokens:
            tl = t.lower().strip(".:")
            if tl in PROPERTY_NOISE: continue
            if tl.isdigit(): continue
            clean.append(t)
        if clean:
            # Tomar la PRIMERA palabra significativa (suele ser la calle)
            street_kw = clean[0].strip(".,:;")
            # Si tiene <3 caracteres ignorar
            if len(street_kw) < 3:
                street_kw = None

    return ref, street_kw


def find_property_by_hint(ref, street_kw):
    """Busca property en Qobrix por ref (primero) o por street (después).
    Si hay 1 sola coincidencia, devuelve su id. Si hay varias o ninguna, None."""
    # 1) Por ref (más fiable)
    if ref:
        try:
            url = QOBRIX_API + "/properties?" + urllib.parse.urlencode(
                {"search": f'ref == "{ref}"', "limit": "2"}, safe='="')
            r = requests.get(url, headers=QOBRIX_HEADERS, timeout=30)
            r.raise_for_status()
            items = r.json().get("data", []) or []
            if len(items) >= 1:
                return items[0]["id"], items[0].get("street","")
        except Exception as exc:
            log.warning(f"  find_property by ref={ref}: {exc}")

    # 2) Por street contains keyword
    if street_kw:
        try:
            url = QOBRIX_API + "/properties?" + urllib.parse.urlencode(
                {"search": f'street contains "{street_kw}"', "limit": "5"}, safe='="')
            r = requests.get(url, headers=QOBRIX_HEADERS, timeout=30)
            r.raise_for_status()
            items = r.json().get("data", []) or []
            if len(items) == 1:
                return items[0]["id"], items[0].get("street","")
            if len(items) > 1:
                log.info(f"  Property hint '{street_kw}': {len(items)} matches, omito link")
        except Exception as exc:
            log.warning(f"  find_property by street={street_kw}: {exc}")

    return None, None


def find_opportunity_for(contact_id, property_id):
    """Devuelve el id de una Opportunity que vincule este contacto a esta propiedad
    (vía el campo many-to-many 'properties'). Si no encuentra, None."""
    if not contact_id or not property_id:
        return None
    try:
        url = QOBRIX_API + "/opportunities?" + urllib.parse.urlencode(
            {"search": f'contact_name == "{contact_id}"', "limit": "20",
             "include[]": "Properties"}, safe='="')
        r = requests.get(url, headers=QOBRIX_HEADERS, timeout=30)
        r.raise_for_status()
        items = r.json().get("data", []) or []
        for o in items:
            props = o.get("properties") or []
            if isinstance(props, list):
                for p in props:
                    pid = p.get("id") if isinstance(p, dict) else p
                    if pid == property_id:
                        return o["id"]
    except Exception as exc:
        log.warning(f"  find_opportunity_for: {exc}")
    return None


# ──────────────────────────────────────────────
# Upsert Meeting Qobrix
# ──────────────────────────────────────────────
def upsert_meeting(event, contact, synced):
    """Crea o actualiza Meeting en Qobrix.

    - El campo `subject` lo auto-genera Qobrix; NO se manda.
    - El titulo del calendar va a la primera linea de `description`.
    - El contacto se vincula con el campo `contact` (UUID).
    - PATCH preserva el `contact` existente si la Meeting ya tenia uno
      (evita pisar un link manual).
    """
    event_id = event["id"]
    contact_id = None
    if contact:
        contact_id = contact.get("id") or contact.get("contact_id")

    start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
    end = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
    if not start or not end:
        return

    summary = event.get("summary", "(sin titulo)")
    location = event.get("location", "")
    base_desc = event.get("description", "") or ""

    # NUEVO: detectar propiedad por título y, si hay opp con esa property + contacto,
    # vincular Meeting -> Opportunity (Qobrix Meeting no tiene campo property directo)
    prop_id, prop_street = (None, None)
    opp_id = None
    try:
        ref_hint, street_kw = extract_property_hint(summary)
        if ref_hint or street_kw:
            prop_id, prop_street = find_property_by_hint(ref_hint, street_kw)
            if prop_id and contact_id:
                opp_id = find_opportunity_for(contact_id, prop_id)
    except Exception as exc:
        log.warning(f"  prop hint err: {exc}")

    description = f"{summary}\n\n{base_desc}".strip() if base_desc else summary
    if prop_id:
        prop_line = f"\n\n📍 Propiedad: {prop_street or ''}".rstrip()
        description = (description + prop_line).strip()
    description = (description + "\n\n[Auto-sync Google Calendar]").strip()

    payload = {
        "description": description[:1000],
        "location": (location or prop_street or "")[:200],
        "start_date": start,
        "end_date": end,
    }
    if OWNER_USER_ID:
        payload["assigned_to"] = OWNER_USER_ID

    qobrix_id = synced.get(event_id)
    try:
        if qobrix_id:
            # PATCH: NO tocar el campo contact si ya estaba enlazado manualmente
            try:
                existing = qobrix_get(f"/meetings/{qobrix_id}")
                existing_data = existing.get("data") or existing
                already_linked = bool(existing_data.get("contact"))
                already_opp = bool(existing_data.get("related_opportunity"))
            except Exception:
                already_linked = False
                already_opp = False
            if contact_id and not already_linked:
                payload["contact"] = contact_id
            if opp_id and not already_opp:
                payload["related_opportunity"] = opp_id
            qobrix_patch(f"/meetings/{qobrix_id}", payload)
            link = "(preservado)" if already_linked else ("✓ contacto" if contact_id else "✗ sin contacto")
            prop_tag = " ✓ opp" if opp_id else (" 📍 prop" if prop_id else "")
            log.info(f"  ↻ Meeting actualizada: {summary[:50]} ({fmt_time(start)}) [{link}{prop_tag}]")
        else:
            if contact_id:
                payload["contact"] = contact_id
            if opp_id:
                payload["related_opportunity"] = opp_id
            r = qobrix_post("/meetings", payload)
            new_id = (r.get("data") or {}).get("id") or r.get("id")
            if new_id:
                synced[event_id] = new_id
                link = "✓ contacto" if contact_id else "✗ sin contacto"
                prop_tag = " ✓ opp" if opp_id else (" 📍 prop" if prop_id else "")
                log.info(f"  ✚ Meeting creada: {summary[:50]} ({fmt_time(start)}) [{link}{prop_tag}]")
            else:
                log.warning(f"  Meeting POST sin id en respuesta: {r}")
    except Exception as exc:
        log.error(f"  Fallo upsert meeting '{summary}': {exc}")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
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

    synced = load_synced()
    cals = calendars_to_sync()
    log.info(f"Sincronizando {len(cals)} calendario(s)")

    total_events = 0
    matched_email = 0
    matched_phone = 0
    matched_name = 0
    no_match = 0

    for cal_id in cals:
        cal_path = "/calendars/" + urllib.parse.quote(cal_id, safe="") + "/events"
        try:
            events = gcal_get(
                cal_path,
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
            log.error(f"GCal API fallo en {cal_id[:30]}...: {exc}")
            continue

        items = events.get("items", [])
        cal_summary = events.get("summary", cal_id[:30])
        log.info(f"  Calendar '{cal_summary}': {len(items)} eventos")
        total_events += len(items)

        for ev in items:
            if ev.get("status") == "cancelled":
                continue

            title = ev.get("summary", "(sin titulo)")
            description = ev.get("description", "") or ""
            contact = None

            # 1) match por email del attendee
            attendees = ev.get("attendees", []) or []
            external = [a for a in attendees if is_external_email(a.get("email", ""))]
            for att in external:
                contact = qobrix_search_contact_by_email(att.get("email", "").strip())
                if contact:
                    matched_email += 1
                    log.info(f"  → Match por email attendee: {att.get('email','')}")
                    break

            # 2) match por email en la descripcion del evento
            if not contact:
                for em in extract_emails(description):
                    contact = qobrix_search_contact_by_email(em)
                    if contact:
                        matched_email += 1
                        log.info(f"  → Match por email de descripcion: {em}")
                        break

            # 3) match por telefono en la descripcion del evento
            if not contact:
                for ph in extract_phones(description):
                    contact = qobrix_search_contact_by_phone(ph)
                    if contact:
                        matched_phone += 1
                        log.info(f"  → Match por telefono de descripcion: {ph} -> {contact.get('first_name','')} {contact.get('last_name','')}")
                        break

            # 4) match por nombre del titulo (best effort)
            if not contact:
                client_name = extract_client_name(title)
                if client_name:
                    contact = search_contact_by_name(client_name)
                    if contact:
                        matched_name += 1
                        log.info(f"  → Match por nombre: '{client_name}' -> {contact.get('first_name','')} {contact.get('last_name','')}")
                else:
                    log.info(f"  - Sin nombre extraible del titulo: '{title[:60]}'")

            if not contact:
                no_match += 1

            # 5) crear/actualizar meeting (con o sin contacto)
            upsert_meeting(ev, contact, synced)

    save_synced(synced)
    log.info(
        f"Resumen: {total_events} eventos | email={matched_email} | "
        f"telefono={matched_phone} | nombre={matched_name} | sin match={no_match}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
