#!/usr/bin/env python3
"""
Idealista -> Qobrix Lead Capture  v4.0 (cloud edition)
======================================================
Monitoriza DOS cuentas de email en busca de leads de Idealista:
  1. ivanfaurar@gmail.com        (Gmail IMAP)
  2. info@ifrealestate.es        (Hostinger IMAP)

Por cada email nuevo crea Contacto + Oportunidad en Qobrix.
- Nombre   -> cabecera Subject ("Nuevo mensaje de NOMBRE sobre...")
- Email    -> cabecera Reply-To  (la mas fiable)
- Telefono -> cuerpo HTML con regex
- Trackeo  -> archivo JSON commiteado al repo (persistente entre runs)

Cambios v4.0:
- Credenciales sensibles leidas SOLO de env vars (no hardcoded).
- Socket timeout global de 30s para que IMAP nunca se cuelgue.
- Diseñado para correr en GitHub Actions cron cada 5 min.
"""

import imaplib
import email
import re
import json
import os
import sys
import socket
import logging
from email.header import decode_header
from datetime import datetime
from html.parser import HTMLParser

import requests

# Timeout global para todas las conexiones (IMAP, HTTP, etc.)
# Si una conexion tarda mas, se aborta y el script termina con error.
# Es mejor fallar rapido que quedarse colgado para siempre.
socket.setdefaulttimeout(30)


# ──────────────────────────────────────────────
# CONFIGURACION DESDE ENV VARS
# ──────────────────────────────────────────────
def required_env(name):
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"FATAL: missing required env var {name}\n")
        sys.exit(2)
    return val


ACCOUNTS = [
    {
        "label":    "Gmail",
        "host":     "imap.gmail.com",
        "port":     993,
        "user":     required_env("GMAIL_USER"),
        "password": required_env("GMAIL_APP_PASSWORD"),
        "folders":  ["INBOX", "[Gmail]/Spam"],
    },
    {
        "label":    "Hostinger",
        "host":     "imap.hostinger.com",
        "port":     993,
        "user":     required_env("HOSTINGER_USER"),
        "password": required_env("HOSTINGER_PASSWORD"),
        "folders":  ["INBOX", "INBOX.Junk"],
    },
]

# Filtro IMAP — solo emails de leads reales de Idealista (no admin/billing)
IMAP_SEARCH = 'FROM "idealista" SINCE 01-Jan-2026 SUBJECT "Nuevo mensaje de"'

QOBRIX_BASE_URL = required_env("QOBRIX_URL").rstrip("/") + "/api/v2"
QOBRIX_HEADERS  = {
    "X-Api-User":   required_env("QOBRIX_USER"),
    "X-Api-Key":    required_env("QOBRIX_KEY"),
    "Content-Type": "application/json",
    "Accept":       "application/json",
}

SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
LOG_FILE        = os.path.join(SCRIPT_DIR, "idealista_qobrix.log")
PROCESSED_FILE  = os.path.join(SCRIPT_DIR, "processed_ids.json")


# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# TRACKEO DE EMAILS PROCESADOS
# IDs se guardan por cuenta: {"Gmail": ["1","2"], "Hostinger": ["1"]}
# El fichero se commitea de vuelta al repo desde el workflow.
# ──────────────────────────────────────────────
def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                return {}
            if isinstance(data, list):
                return {"Gmail": set(data), "Hostinger": set()}
            return {k: set(v) for k, v in data.items()}
    return {}


def save_processed(processed_dict):
    with open(PROCESSED_FILE, "w") as f:
        json.dump({k: sorted(v) for k, v in processed_dict.items()}, f, indent=2)


# ──────────────────────────────────────────────
# UTILIDADES DE EMAIL
# ──────────────────────────────────────────────
def decode_str(s):
    if not s:
        return ""
    parts = decode_header(s)
    result = ""
    for raw, enc in parts:
        if isinstance(raw, bytes):
            result += raw.decode(enc or "utf-8", errors="replace")
        else:
            result += raw
    return result


class _HTMLTextExtractor(HTMLParser):
    """Extrae texto plano de HTML eliminando tags."""
    def __init__(self):
        super().__init__()
        self._parts = []
    def handle_data(self, data):
        self._parts.append(data)
    def get_text(self):
        return " ".join(self._parts)


def html_to_text(html):
    """Convierte HTML a texto plano basico."""
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
    html = re.sub(r'</p>', '\n', html, flags=re.I)
    parser = _HTMLTextExtractor()
    parser.feed(html)
    text = parser.get_text()
    text = re.sub(r' {2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def get_email_body(msg):
    """Devuelve (text_body, html_body) del mensaje."""
    text_body = ""
    html_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if ct == "text/plain" and not text_body:
                text_body = decoded
            elif ct == "text/html" and not html_body:
                html_body = decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            raw = payload.decode(charset, errors="replace")
            if "<html" in raw.lower() or "<div" in raw.lower():
                html_body = raw
            else:
                text_body = raw

    return text_body, html_body


# ──────────────────────────────────────────────
# PARSEO DEL LEAD
# ──────────────────────────────────────────────
def parse_lead(subject, text_body, html_body, reply_to):
    lead = {"name": "", "email": "", "phone": "", "property_url": ""}

    name_patterns = [
        r"Nuevo mensaje de (.+?) sobre",
        r"mensaje de (.+?) para",
        r"de (.+?) ha contactado",
        r"Mensaje de (.+?)[\.\,\:]",
    ]
    for pat in name_patterns:
        m = re.search(pat, subject, re.I)
        if m:
            lead["name"] = m.group(1).strip()
            break

    if not lead["name"] and html_body:
        m = re.search(
            r'<div[^>]*(?:font-weight:\s*700|font-weight:bold)[^>]*>([^<]{2,60})</div>',
            html_body, re.I,
        )
        if m:
            candidate = m.group(1).strip()
            if not any(w in candidate.lower() for w in ["nuevo", "mensaje", "idealista", "tienes"]):
                lead["name"] = candidate

    if reply_to:
        m = re.search(r'([a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,})', reply_to, re.I)
        if m:
            lead["email"] = m.group(1)

    if not lead["email"] and html_body:
        candidates = re.findall(r'([a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,})', html_body, re.I)
        for c in candidates:
            if not any(d in c for d in ["idealista", "noreply", "reply", "ifrealestate"]):
                lead["email"] = c
                break

    body_to_search = html_body or text_body
    phone_patterns = [
        r'(\+34\s?[\d\s\-\.]{8,15})',
        r'(\+\d{1,3}\s?[\d\s\-\.]{6,15})',
        r'(\b[6789]\d{8}\b)',
        r'(\b9\d{8}\b)',
    ]
    for pat in phone_patterns:
        m = re.search(pat, body_to_search, re.I)
        if m:
            raw = m.group(1).strip()
            clean = re.sub(r'[^\d\+\s\-]', '', raw).strip()
            digits = re.sub(r'\D', '', clean)
            if len(digits) >= 9:
                lead["phone"] = clean
                break

    m = re.search(r'(https?://(?:www\.)?idealista\.com/inmueble/\d+/?)', body_to_search)
    if m:
        lead["property_url"] = m.group(1)

    return lead


# ──────────────────────────────────────────────
# QOBRIX
# ──────────────────────────────────────────────
def sanitize(text, max_len=2000):
    if not text:
        return ""
    cleaned = []
    for ch in text:
        cp = ord(ch)
        if cp > 0xFFFF:
            continue
        if 0xD800 <= cp <= 0xDFFF:
            continue
        if cp < 0x20 and ch not in "\n\t":
            continue
        cleaned.append(ch)
    return "".join(cleaned)[:max_len]


def create_contact(name, email_addr, phone, description):
    parts = (name or "Lead Idealista").strip().split()
    first_name = parts[0][:100]
    last_name  = " ".join(parts[1:])[:100] if len(parts) > 1 else "Idealista"

    payload = {
        "first_name":  first_name,
        "last_name":   last_name,
        "description": sanitize(description),
    }
    if email_addr and "@" in email_addr:
        payload["email"] = email_addr.strip()[:200]
    if phone:
        clean_phone = re.sub(r"[^\d\s\+\-\(\)\.]", "", phone).strip()
        if clean_phone:
            payload["phone"] = clean_phone[:50]

    try:
        r = requests.post(
            f"{QOBRIX_BASE_URL}/contacts",
            headers=QOBRIX_HEADERS, json=payload, timeout=30,
        )
        r.raise_for_status()
        cid = r.json().get("data", {}).get("id")
        log.info(f"  Contacto creado: {first_name} {last_name}  [{cid}]")
        return cid
    except requests.HTTPError as exc:
        log.error(f"  Error contacto HTTP {exc.response.status_code}: {exc.response.text[:300]}")
    except Exception as exc:
        log.error(f"  Error contacto: {exc}")
    return None


def create_opportunity(contact_id, description, subject):
    payload = {
        "contact_name":       contact_id,
        "status":             "new",
        "source":             "external_site",
        "source_description": "Idealista",
        "buy_rent":           "to_buy",
        "description":        sanitize(description),
        "enquiry_date":       datetime.now().strftime("%Y-%m-%d"),
    }
    # Asignar al owner (necesario para que Qobrix dispare la notif push
    # "te han asignado un nuevo lead" al usuario del CRM cuya app móvil escucha).
    owner_id = os.environ.get("OWNER_USER_ID", "").strip()
    if owner_id:
        payload["owner"] = owner_id

    try:
        r = requests.post(
            f"{QOBRIX_BASE_URL}/opportunities",
            headers=QOBRIX_HEADERS, json=payload, timeout=30,
        )
        r.raise_for_status()
        oid = r.json().get("data", {}).get("id")
        log.info(f"  Oportunidad creada: {subject[:60]}  [{oid}]")
        return oid
    except requests.HTTPError as exc:
        log.error(f"  Error oportunidad HTTP {exc.response.status_code}: {exc.response.text[:300]}")
    except Exception as exc:
        log.error(f"  Error oportunidad: {exc}")
    return None


# ──────────────────────────────────────────────
# NOTIFICACION PUSH ntfy.sh (logo + texto custom)
# ──────────────────────────────────────────────
def notify_ntfy(lead, subject, opportunity_id, source_name):
    """Envia push enriquecido al telefono via ntfy.sh.
    Si el secret NTFY_TOPIC no esta definido, no hace nada (silencio limpio)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return  # ntfy desactivado

    icon_url = os.environ.get("NTFY_ICON_URL", "").strip()
    qobrix_base = os.environ.get(
        "QOBRIX_URL", "https://ifrealestate4571.eu1.qobrix.com"
    ).rstrip("/")

    # Extraer ref + calle/zona del subject de Idealista.
    # Formato tipico: "Nuevo mensaje de NOMBRE sobre tu inmueble, con ref: 1093, Piso en Avenida Pallaresa"
    ref = ""
    calle = ""
    m = re.search(r"ref[:\s]+(\d+)\s*,?\s*(.*)", subject, re.I)
    if m:
        ref = m.group(1).strip()
        calle = re.sub(r"\s+", " ", m.group(2)).strip().rstrip(".,;: -")

    # Cuerpo: solo lo esencial (calle + ref). Nombre va en el TITLE.
    body_parts = []
    if calle:
        body_parts.append(f"🏠 {calle}")
    if ref:
        body_parts.append(f"ref {ref}")
    body = " · ".join(body_parts) if body_parts else "Nuevo comprador interesado"

    name = (lead.get("name") or "Nuevo lead").strip()
    headers = {
        "Title": f"🔥 LEAD {source_name.upper()} · {name}",
        "Priority": "high",
        "Tags": "fire",
    }
    # En iOS: Attach pone la imagen DENTRO de la notif (al desplegar).
    # Icon como ICONO de la notif NO funciona en iOS por restriccion de Apple.
    if icon_url:
        headers["Attach"] = icon_url
    if opportunity_id:
        opp_url = f"{qobrix_base}/crm/opportunities/{opportunity_id}"
        headers["Click"] = opp_url
        headers["Actions"] = f"view, Abrir en Qobrix, {opp_url}, clear=true"

    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        if r.status_code == 200:
            log.info(f"  Push ntfy enviado")
        else:
            log.warning(f"  Push ntfy HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        log.warning(f"  Push ntfy fallo (no critico): {exc}")


# ──────────────────────────────────────────────
# NOTIFICACION WEB PUSH (PWA propia con logo IF)
# ──────────────────────────────────────────────
def notify_webpush(lead, subject, opportunity_id, source_name):
    """Envia push a TODAS las suscripciones registradas en WEBPUSH_SUBSCRIPTIONS.
    Cada suscripcion es un dict {endpoint, keys: {p256dh, auth}}.
    Si no hay nada configurado o falla la libreria, silencio limpio."""
    subs_json = os.environ.get("WEBPUSH_SUBSCRIPTIONS", "").strip()
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not subs_json or not private_key:
        return

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("  pywebpush no instalado; salto Web Push")
        return

    try:
        subscriptions = json.loads(subs_json)
        if isinstance(subscriptions, dict):
            subscriptions = [subscriptions]
    except json.JSONDecodeError as exc:
        log.error(f"  WEBPUSH_SUBSCRIPTIONS invalido: {exc}")
        return

    qobrix_base = os.environ.get(
        "QOBRIX_URL", "https://ifrealestate4571.eu1.qobrix.com"
    ).rstrip("/")
    vapid_email = os.environ.get("VAPID_EMAIL", "ivanfaurar@gmail.com")

    # Construir payload del push
    ref = ""
    calle = ""
    m = re.search(r"ref[:\s]+(\d+)\s*,?\s*(.*)", subject, re.I)
    if m:
        ref = m.group(1).strip()
        calle = re.sub(r"\s+", " ", m.group(2)).strip().rstrip(".,;: -")

    body_parts = []
    if calle:
        body_parts.append(f"🏠 {calle}")
    if ref:
        body_parts.append(f"ref {ref}")
    body = " · ".join(body_parts) if body_parts else "Nuevo comprador interesado"
    name = (lead.get("name") or "Nuevo lead").strip()

    # Sin "title": todo en el body para que iOS NO añada el "from <PWA name>" como subtitle.
    full_body = f"🔥 LEAD {source_name.upper()} · {name}\n{body}"
    payload = json.dumps({
        "body":  full_body,
        "url":   f"{qobrix_base}/crm/opportunities/{opportunity_id}" if opportunity_id else qobrix_base,
        "tag":   f"lead-{opportunity_id or 'new'}",
    })

    sent = 0
    for sub in subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": f"mailto:{vapid_email}"},
                ttl=86400,  # 24h de validez maxima
            )
            sent += 1
        except WebPushException as exc:
            log.warning(f"  Push Web a sub fallo: {exc}")
        except Exception as exc:
            log.warning(f"  Push Web error inesperado: {exc}")

    log.info(f"  Push Web enviado a {sent}/{len(subscriptions)} suscripcion(es)")


# ──────────────────────────────────────────────
# PROCESAR UNA CUENTA
# ──────────────────────────────────────────────
def process_account(account, processed_dict):
    label    = account["label"]
    host     = account["host"]
    port     = account["port"]
    user     = account["user"]
    password = account["password"]
    folders  = account.get("folders", ["INBOX"])

    processed = processed_dict.setdefault(label, set())

    log.info(f"--- Revisando {label} ({user}) ---")
    try:
        mail = imaplib.IMAP4_SSL(host, port, timeout=30)
        mail.login(user, password)

        for folder in folders:
            try:
                status, _ = mail.select(folder)
                if status != "OK":
                    continue

                _, ids = mail.search(None, IMAP_SEARCH)
                all_ids = ids[0].split()
                new_ids = [eid for eid in all_ids
                           if f"{folder}:{eid.decode()}" not in processed]

                if not new_ids:
                    log.info(f"  [{folder}] Sin emails nuevos de Idealista.")
                    continue

                log.info(f"  [{folder}] {len(new_ids)} email(s) nuevos.")

                for eid in new_ids:
                    try:
                        _, data = mail.fetch(eid, "(BODY.PEEK[])")
                        raw = data[0][1]
                        msg = email.message_from_bytes(raw)

                        subject  = decode_str(msg.get("Subject", ""))
                        from_hdr = decode_str(msg.get("From", ""))
                        reply_to = decode_str(msg.get("Reply-To", ""))

                        text_body, html_body = get_email_body(msg)
                        plain_text = text_body or html_to_text(html_body)

                        log.info(f"  EMAIL: {subject[:80]}")

                        lead = parse_lead(subject, text_body, html_body, reply_to)
                        log.info(f"    nombre={lead['name']!r}  "
                                 f"email={lead['email']!r}  "
                                 f"tel={lead['phone']!r}")

                        description = (
                            f"Lead Idealista\n"
                            f"Asunto: {subject}\n"
                            f"De: {from_hdr}\n"
                            f"Cuenta: {user}\n"
                            f"Propiedad: {lead['property_url']}\n\n"
                            f"--- Contenido ---\n{plain_text[:1500]}"
                        )

                        contact_id = create_contact(
                            lead["name"], lead["email"], lead["phone"], description,
                        )

                        if contact_id:
                            opp_id = create_opportunity(contact_id, description, subject)
                            # Notif push corporativa: PWA propia (con logo IF) + ntfy de respaldo
                            notify_webpush(lead, subject, opp_id, "Idealista")
                            notify_ntfy(lead, subject, opp_id, "Idealista")
                            processed.add(f"{folder}:{eid.decode()}")
                            save_processed(processed_dict)
                        else:
                            log.warning("  Contacto no creado, se reintentara en la proxima ejecucion")

                    except Exception as exc:
                        log.error(f"  Error procesando email {eid}: {exc}", exc_info=True)

            except Exception as exc:
                log.error(f"  Error en carpeta {folder} [{label}]: {exc}", exc_info=True)

        mail.logout()

    except imaplib.IMAP4.error as exc:
        log.error(f"  Error IMAP [{label}]: {exc}")
    except (socket.timeout, socket.gaierror) as exc:
        log.error(f"  Error red [{label}]: {exc}")
    except Exception as exc:
        log.error(f"  Error inesperado [{label}]: {exc}", exc_info=True)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def run():
    log.info("=== Idealista->Qobrix monitor START ===")
    processed_dict = load_processed()
    for account in ACCOUNTS:
        process_account(account, processed_dict)
    log.info("=== Idealista->Qobrix monitor END ===\n")


if __name__ == "__main__":
    run()
