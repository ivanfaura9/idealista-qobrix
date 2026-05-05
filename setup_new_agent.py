#!/usr/bin/env python3
"""
setup_new_agent.py - Wizard de onboarding para un agente nuevo
================================================================
Para usar tras hacer "Use this template" del repo en GitHub.

Modos:
  python3 setup_new_agent.py                  # OAuth flow + lista calendarios
  python3 setup_new_agent.py --list-calendars # Solo lista los calendarios
  python3 setup_new_agent.py --print-secrets  # Imprime los secrets a copiar a GitHub

Pre-requisitos:
- Tener el JSON de OAuth Client (Desktop) descargado de Google Cloud Console
- Colocarlo en ~/credentials.json o pasar --creds /ruta/a/credentials.json
"""

import argparse
import json
import os
import sys
import webbrowser
import urllib.parse
import urllib.request
import http.server
import socketserver
import secrets
import time

OAUTH_PORT = 8765
SCOPES = "https://www.googleapis.com/auth/calendar.readonly"


def find_creds(path=None):
    candidates = [
        path,
        os.path.expanduser("~/credentials.json"),
        os.path.expanduser("~/Downloads/credentials.json"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    # busca el último client_secret_*.json en Downloads
    dl = os.path.expanduser("~/Downloads")
    if os.path.isdir(dl):
        files = sorted(
            (f for f in os.listdir(dl) if f.startswith("client_secret_") and f.endswith(".json")),
            key=lambda f: os.path.getmtime(os.path.join(dl, f)),
            reverse=True,
        )
        if files:
            return os.path.join(dl, files[0])
    return None


def load_client(creds_path):
    with open(creds_path) as f:
        data = json.load(f)
    creds = data.get("installed") or data.get("web") or {}
    if not creds.get("client_id"):
        raise SystemExit(f"JSON malformado: {creds_path}")
    return creds["client_id"], creds["client_secret"]


def oauth_flow(client_id, client_secret):
    """Lanza servidor local + abre navegador + intercambia code por tokens."""
    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://localhost:{OAUTH_PORT}/"
    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(auth_params)
    received = {"code": None, "error": None}

    class CB(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(q)
            if "code" in params and params.get("state", [""])[0] == state:
                received["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body style='font-family:sans-serif;padding:40px;text-align:center'>"
                    b"<h2 style='color:#DD1806'>IF Real Estate</h2>"
                    b"<p>Autorizacion completada. Puedes cerrar esta ventana.</p>"
                    b"</body></html>"
                )
            elif "error" in params:
                received["error"] = params["error"][0]
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Error")
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *a, **kw):
            pass

    print(f"\nAbriendo navegador para autorizar acceso a tu Google Calendar...")
    print(f"(Si no se abre solo, copia esta URL: {auth_url[:80]}...)\n")

    with socketserver.TCPServer(("localhost", OAUTH_PORT), CB) as httpd:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass
        httpd.timeout = 300
        deadline = time.time() + 300
        while received["code"] is None and received["error"] is None and time.time() < deadline:
            httpd.handle_request()

    if received["error"]:
        raise SystemExit(f"OAuth error: {received['error']}")
    if not received["code"]:
        raise SystemExit("Timeout esperando autorizacion (5 min)")

    print("Autorizacion recibida, intercambiando por tokens...")
    data = urllib.parse.urlencode({
        "code": received["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        tokens = json.loads(resp.read())

    if "refresh_token" not in tokens:
        print("\n⚠️  No vino refresh_token. Posible causa: ya autorizaste antes.")
        print("    Revoca acceso en https://myaccount.google.com/permissions y reintenta.")
        raise SystemExit(1)

    return tokens["refresh_token"], tokens["access_token"]


def list_calendars(access_token):
    req = urllib.request.Request(
        "https://www.googleapis.com/calendar/v3/users/me/calendarList",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()).get("items", [])


def fmt_calendars(cals):
    lines = []
    lines.append("\n=== Tus calendarios ===")
    lines.append("Selecciona los IDs de los calendarios donde anotas VISITAS y LLAMADAS:")
    lines.append("(NO incluyas Personal, Festivos, ni los que gestiona GHL automaticamente)\n")
    for c in cals:
        flag = "📌" if c.get("primary") else "  "
        lines.append(f"{flag} {c['summary']:35s}  →  {c['id']}")
    lines.append("\nPara editar la lista, modifica DEFAULT_CALENDARS en calendar_sync.py y daily_briefing.py")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Setup wizard para nuevo agente IF Real Estate")
    ap.add_argument("--creds", help="Ruta al JSON OAuth client (default: busca en ~/Downloads)")
    ap.add_argument("--list-calendars", action="store_true", help="Solo listar calendarios disponibles")
    ap.add_argument("--print-secrets", action="store_true", help="Imprime los GitHub Secrets a configurar")
    args = ap.parse_args()

    if args.print_secrets:
        print("""
GitHub Secrets necesarios (Settings → Secrets and variables → Actions):

REQUERIDOS:
  GMAIL_USER             email del agente (para IMAP)
  GMAIL_APP_PASSWORD     App password de Gmail (16 chars sin espacios)
  QOBRIX_URL             https://ifrealestate4571.eu1.qobrix.com
  QOBRIX_USER            UUID del bot Qobrix (pedir al admin)
  QOBRIX_KEY             API key del bot Qobrix (pedir al admin)
  OWNER_USER_ID          UUID del agente en Qobrix (pedir al admin)
  GOOGLE_CLIENT_ID       Del JSON OAuth descargado
  GOOGLE_CLIENT_SECRET   Del JSON OAuth descargado
  GOOGLE_REFRESH_TOKEN   Lo genera este script
  VAPID_PRIVATE_KEY      Compartida (pedir al admin)
  VAPID_EMAIL            Email del agente
  WEBPUSH_SUBSCRIPTIONS  JSON de la suscripcion PWA (desde el iPhone)

OPCIONALES:
  HOSTINGER_USER         Si tiene buzon en Hostinger (ej: info@ifrealestate.es)
  HOSTINGER_PASSWORD     Password Hostinger
""")
        return 0

    creds_path = find_creds(args.creds)
    if not creds_path:
        print("ERROR: no encuentro credentials.json")
        print("Colócalo en ~/credentials.json o pasa --creds /ruta/al/json")
        print("(El JSON se descarga de Google Cloud Console -> APIs -> Credentials)")
        return 1
    print(f"Usando credenciales: {creds_path}")

    client_id, client_secret = load_client(creds_path)

    if args.list_calendars:
        # Solo listar requiere acceso, hace flow corto
        refresh, access = oauth_flow(client_id, client_secret)
        print(fmt_calendars(list_calendars(access)))
        print(f"\n(Refresh token tambien generado por si lo quieres: {refresh[:20]}...)")
        return 0

    # Flow completo
    refresh, access = oauth_flow(client_id, client_secret)
    cals = list_calendars(access)

    print("\n✅ OAuth flow completado.\n")
    print("=" * 70)
    print("COPIA ESTOS VALORES A GITHUB SECRETS:")
    print("=" * 70)
    print(f"GOOGLE_CLIENT_ID       = {client_id}")
    print(f"GOOGLE_CLIENT_SECRET   = {client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN   = {refresh}")
    print("=" * 70)

    print(fmt_calendars(cals))

    print("""
SIGUIENTES PASOS:
  1. Pega los 3 valores de arriba en los Secrets de tu repo
  2. Edita calendar_sync.py y daily_briefing.py con los IDs de tus calendarios de trabajo
  3. Ejecuta `python3 setup_new_agent.py --print-secrets` para ver TODOS los secrets que faltan
  4. Cuando todos esten configurados, en GitHub: Actions -> Run workflow para testear
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
