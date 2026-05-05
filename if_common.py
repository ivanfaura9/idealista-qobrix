#!/usr/bin/env python3
"""
if_common.py - helpers compartidos por los scripts de IF Real Estate
====================================================================
- google_access_token()  : refresca access_token desde GOOGLE_REFRESH_TOKEN
- qobrix_get/post/patch  : wrappers HTTP a la API de Qobrix
- qobrix_search_contact  : busca contacto por email en Qobrix
- send_push              : envia Web Push corporativo al PWA IF Real Estate
"""

import os
import json
import logging
import urllib.parse
import urllib.request
import urllib.error
import requests

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
QOBRIX_BASE = os.environ.get(
    "QOBRIX_URL", "https://ifrealestate4571.eu1.qobrix.com"
).rstrip("/")
QOBRIX_API = QOBRIX_BASE + "/api/v2"
QOBRIX_HEADERS = {
    "X-Api-User": os.environ.get("QOBRIX_USER", ""),
    "X-Api-Key": os.environ.get("QOBRIX_KEY", ""),
    "Content-Type": "application/json",
    "Accept": "application/json",
}


# ──────────────────────────────────────────────
# GOOGLE OAUTH - refresca access_token
# ──────────────────────────────────────────────
def google_access_token():
    """Devuelve un access_token Bearer fresco usando el refresh_token guardado."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip()
    if not all([client_id, client_secret, refresh_token]):
        raise RuntimeError("GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN no estan configurados")

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())

    if "access_token" not in body:
        raise RuntimeError(f"Google refresh fallo: {body}")
    expires_in_days = body.get("refresh_token_expires_in")
    if expires_in_days is not None:
        days = expires_in_days / 86400
        if days < 2:
            log.warning(
                f"  GOOGLE_REFRESH_TOKEN expira en {days:.1f} dias. Regenerar pronto."
            )
    return body["access_token"]


def gcal_get(path, params=None, access_token=None):
    """GET a Google Calendar API v3."""
    if access_token is None:
        access_token = google_access_token()
    url = "https://www.googleapis.com/calendar/v3" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ──────────────────────────────────────────────
# QOBRIX HTTP wrappers
# ──────────────────────────────────────────────
def qobrix_get(path, params=None):
    url = QOBRIX_API + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, safe='="')
    r = requests.get(url, headers=QOBRIX_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def qobrix_post(path, payload):
    r = requests.post(QOBRIX_API + path, headers=QOBRIX_HEADERS, json=payload, timeout=30)
    if r.status_code >= 400:
        log.error(f"  POST {path} -> {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json() if r.text else {}


def qobrix_patch(path, payload):
    r = requests.patch(QOBRIX_API + path, headers=QOBRIX_HEADERS, json=payload, timeout=30)
    if r.status_code >= 400:
        log.error(f"  PATCH {path} -> {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json() if r.text else {}


def qobrix_search_contact_by_email(email_addr):
    """Devuelve el primer contacto cuyo email coincida, o None."""
    if not email_addr:
        return None
    try:
        # Qobrix search expression
        search = f'email == "{email_addr}" or email_2 == "{email_addr}"'
        url = QOBRIX_API + "/contacts?" + urllib.parse.urlencode({"search": search, "limit": 1})
        r = requests.get(url, headers=QOBRIX_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json().get("data", [])
        if data:
            return data[0]
    except Exception as exc:
        log.warning(f"  qobrix_search_contact: {exc}")
    return None


# ──────────────────────────────────────────────
# WEB PUSH
# ──────────────────────────────────────────────
def send_push(body, url=None, tag=None):
    """Envia Web Push a todas las suscripciones registradas. Silencio si no hay config."""
    subs_json = os.environ.get("WEBPUSH_SUBSCRIPTIONS", "").strip()
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not subs_json or not private_key:
        log.info("  Push deshabilitado (sin WEBPUSH_SUBSCRIPTIONS o VAPID_PRIVATE_KEY)")
        return 0

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("  pywebpush no instalado; salto Web Push")
        return 0

    try:
        subscriptions = json.loads(subs_json)
        if isinstance(subscriptions, dict):
            subscriptions = [subscriptions]
    except json.JSONDecodeError as exc:
        log.error(f"  WEBPUSH_SUBSCRIPTIONS invalido: {exc}")
        return 0

    vapid_email = os.environ.get("VAPID_EMAIL", "ivanfaurar@gmail.com")
    payload = json.dumps({
        "body": body,
        "url": url or QOBRIX_BASE,
        "tag": tag or "if-notif",
    })

    sent = 0
    for sub in subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": f"mailto:{vapid_email}"},
                ttl=3600,
            )
            sent += 1
        except WebPushException as exc:
            log.warning(f"  Push fallo a {sub.get('endpoint','?')[:60]}: {exc}")
    log.info(f"  Web Push enviado a {sent}/{len(subscriptions)} suscripciones")
    return sent
