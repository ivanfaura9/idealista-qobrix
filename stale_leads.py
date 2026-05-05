#!/usr/bin/env python3
"""
stale_leads.py - Detector de leads sin atender > 48h
=====================================================
Cada noche (21:00 hora local):
  1. Lista oportunidades en Qobrix con status='new' asignadas a Ivan.
  2. Filtra las creadas hace > 48h y < 14 dias.
  3. Manda Web Push con resumen + URL a la primera.

No modifica nada en Qobrix. Solo recordatorio.
"""

import os
import sys
import logging
import socket
import urllib.parse
from datetime import datetime, timedelta, timezone

from if_common import qobrix_get, send_push, QOBRIX_BASE

socket.setdefaulttimeout(30)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  [stale_leads] %(message)s",
)
log = logging.getLogger(__name__)

OWNER_USER_ID = os.environ.get("OWNER_USER_ID", "").strip()


def parse_qobrix_date(s):
    """Acepta varios formatos de fecha que Qobrix devuelve."""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("Z") else s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def main():
    if not OWNER_USER_ID:
        log.warning("OWNER_USER_ID no configurado, salgo.")
        return 0

    # Buscar oportunidades nuevas asignadas al owner
    search = f'status == "new" and assigned_to == "{OWNER_USER_ID}"'
    try:
        params = {
            "search": search,
            "limit": "100",
            "sort": "-created",
        }
        data = qobrix_get("/opportunities?" + urllib.parse.urlencode(params, safe='="'))
    except Exception as exc:
        log.error(f"Qobrix list opportunities fallo: {exc}")
        return 1

    opps = data.get("data", [])
    log.info(f"Total opps con status=new del owner: {len(opps)}")

    now = datetime.now(timezone.utc)
    threshold_old = now - timedelta(hours=48)
    threshold_max = now - timedelta(days=14)
    stale = []
    for opp in opps:
        created_str = opp.get("created") or opp.get("created_at") or opp.get("date_created")
        created = parse_qobrix_date(created_str)
        if not created:
            continue
        if threshold_max < created < threshold_old:
            stale.append((created, opp))

    log.info(f"Stale (>48h, <14d): {len(stale)}")
    if not stale:
        log.info("Nada que notificar.")
        return 0

    stale.sort(key=lambda x: x[0])  # mas antiguos primero

    n = len(stale)
    lines = [f"⏰ {n} lead{'s' if n!=1 else ''} sin atender (>48h):"]
    for _, opp in stale[:5]:
        name = opp.get("contact_name_full") or opp.get("name") or opp.get("subject") or "Lead"
        if isinstance(name, dict):
            name = name.get("full_name") or "Lead"
        name = str(name).strip()[:50]
        source = (opp.get("source") or "").upper()
        prefix = f"[{source}] " if source else ""
        lines.append(f"• {prefix}{name}")
    if n > 5:
        lines.append(f"+{n-5} mas")

    first_id = stale[0][1].get("id")
    url = f"{QOBRIX_BASE}/crm/opportunities/{first_id}" if first_id else QOBRIX_BASE
    send_push("\n".join(lines), url=url, tag="stale-leads")
    return 0


if __name__ == "__main__":
    sys.exit(main())
