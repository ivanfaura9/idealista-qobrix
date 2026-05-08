#!/usr/bin/env python3
"""
retro_update_names.py - One-shot: actualiza contactos viejos en Qobrix
========================================================================
Recorre los contactos creados por monitor.py que tienen nombres genéricos
('No especificado', 'Lead Idealista', 'Lead Fotocasa', 'Lead Habitaclia',
'Lead Milanuncios', etc.) y, si su email coincide con un email original
encontrado en IMAP, re-parsea ese email con la lógica nueva (que mira el From)
y actualiza el contacto en Qobrix con el nombre real.

Diseñado para correr UNA SOLA VEZ tras desplegar el fix de monitor.py.

Uso:
  python3 retro_update_names.py [--dry-run]
"""

import os
import sys
import re
import imaplib
import email
import argparse
import logging
import urllib.parse
from email.header import decode_header

import requests

# Reusar lógica del monitor
from monitor import _name_from_header, decode_str

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  [retro] %(message)s",
)
log = logging.getLogger(__name__)

QOBRIX_BASE = os.environ.get("QOBRIX_URL", "").rstrip("/")
QOBRIX_API = QOBRIX_BASE + "/api/v2"
QOBRIX_HEADERS = {
    "X-Api-User": os.environ.get("QOBRIX_USER", ""),
    "X-Api-Key": os.environ.get("QOBRIX_KEY", ""),
    "Content-Type": "application/json",
    "Accept": "application/json",
}

GENERIC_FIRST = {"no", "lead", ""}
GENERIC_LAST = {"especificado", "especificado.", "idealista", "fotocasa", "habitaclia",
                "milanuncios", "lead", ""}


def is_generic_name(first, last):
    f = (first or "").strip().lower()
    l = (last or "").strip().lower()
    if f in GENERIC_FIRST: return True
    if l in GENERIC_LAST and f in {"no", "lead", "cliente"}: return True
    if f == "no" and "especificad" in l: return True
    return False


def list_generic_contacts():
    """Lista contactos creados auto con nombres genéricos."""
    page = 1
    found = []
    while True:
        r = requests.get(
            f"{QOBRIX_API}/contacts",
            headers=QOBRIX_HEADERS,
            params={"limit": 100, "page": page, "sort": "-created"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("data", []) or []
        if not items:
            break
        for c in items:
            first = c.get("first_name") or ""
            last = c.get("last_name") or ""
            if is_generic_name(first, last) and (c.get("email") or c.get("phone")):
                found.append(c)
        if not data.get("pagination", {}).get("has_next_page"):
            break
        page += 1
        if page > 30:
            break
    return found


def find_email_in_imap(host, user, pw, target_email):
    """Busca en IMAP un email que mencione target_email (normalmente el del cliente)."""
    if not target_email:
        return None
    try:
        mail = imaplib.IMAP4_SSL(host)
        mail.login(user, pw)
        mail.select("INBOX")
        # Search por TEXT (lento pero efectivo) o por FROM
        _, ids = mail.search(None, f'(BODY "{target_email}")')
        ids_list = ids[0].split() if ids and ids[0] else []
        if not ids_list:
            mail.logout()
            return None
        # Coger el más reciente
        eid = ids_list[-1]
        _, msg_data = mail.fetch(eid, "(RFC822)")
        if not msg_data or not msg_data[0]:
            mail.logout()
            return None
        msg = email.message_from_bytes(msg_data[0][1])
        from_hdr = decode_str(msg.get("From", ""))
        reply_to = decode_str(msg.get("Reply-To", ""))
        subject = decode_str(msg.get("Subject", ""))
        mail.logout()
        return {"from": from_hdr, "reply_to": reply_to, "subject": subject}
    except Exception as exc:
        log.warning(f"  IMAP search '{target_email}' en {host}: {exc}")
        return None


def update_contact_name(contact_id, new_first, new_last, dry_run=False):
    payload = {"first_name": new_first[:100], "last_name": new_last[:100]}
    if dry_run:
        log.info(f"  [dry-run] PATCH contacto {contact_id} -> {new_first} {new_last}")
        return True
    try:
        r = requests.patch(
            f"{QOBRIX_API}/contacts/{contact_id}",
            headers=QOBRIX_HEADERS, json=payload, timeout=30,
        )
        r.raise_for_status()
        log.info(f"  ✓ {contact_id[:8]} -> {new_first} {new_last}")
        return True
    except Exception as exc:
        log.error(f"  PATCH contacto {contact_id} fallo: {exc}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    accounts = [
        ("imap.gmail.com", os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_APP_PASSWORD", "")),
        ("imap.hostinger.com", os.environ.get("HOSTINGER_USER", ""), os.environ.get("HOSTINGER_PASSWORD", "")),
    ]

    log.info("Listando contactos con nombres genéricos en Qobrix...")
    candidates = list_generic_contacts()
    log.info(f"Encontrados {len(candidates)} candidatos")

    updated, no_match, errors = 0, 0, 0
    for c in candidates:
        first = c.get("first_name") or ""
        last = c.get("last_name") or ""
        email_addr = c.get("email") or c.get("email_2")
        log.info(f"\n {first!r} {last!r} | email={email_addr} | id={c['id'][:8]}")
        if not email_addr:
            no_match += 1
            continue
        # Buscar en IMAP
        meta = None
        for host, user, pw in accounts:
            if not user or not pw:
                continue
            meta = find_email_in_imap(host, user, pw, email_addr)
            if meta:
                break
        if not meta:
            log.info(f"   sin email original encontrado")
            no_match += 1
            continue

        # Extraer nombre real del From / Reply-To
        new_name = _name_from_header(meta["from"]) or _name_from_header(meta["reply_to"])
        if not new_name:
            log.info(f"   no puedo extraer nombre real del From={meta['from'][:60]!r}")
            no_match += 1
            continue
        # Split first / last
        parts = new_name.split()
        new_first = parts[0]
        new_last = " ".join(parts[1:]) if len(parts) > 1 else ""
        # Solo actualizar si distinto del actual
        if new_first.lower() == first.lower() and new_last.lower() == last.lower():
            log.info(f"   ya correcto, salto")
            continue
        ok = update_contact_name(c["id"], new_first, new_last, dry_run=args.dry_run)
        if ok:
            updated += 1
        else:
            errors += 1

    log.info(f"\n=== Resumen: {updated} actualizados | {no_match} sin match | {errors} errores ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
