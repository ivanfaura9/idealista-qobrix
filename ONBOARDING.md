# Onboarding · Sistema IF Real Estate Automations

Este repo es una **plantilla** (GitHub template). Cada agente comercial de IF Real Estate clona su propia copia y queda con un sistema 24/7 que:

1. Captura leads de **Idealista, Fotocasa, Habitaclia, Milanuncios** desde su email → los crea como Contacto + Oportunidad en **Qobrix CRM** → manda push corporativo a su iPhone con branding IF.
2. Sincroniza su **Google Calendar** (visitas, llamadas) con **Reuniones en Qobrix**.
3. Manda push matinal con la agenda del día (07:30) y recordatorio nocturno con leads sin atender >48h (21:00).

Todo corre en **GitHub Actions** (gratis, sin Mac/servidor propio). Tiempo de setup: **~30 min**.

---

## 🎯 Resumen rápido

| Recurso | ¿Compartido entre agentes? |
|---|---|
| Qobrix CRM (URL + Bot user) | ✅ Sí — `QOBRIX_URL`, `QOBRIX_USER`, `QOBRIX_KEY` son de la cuenta IF Real Estate |
| Owner / asignación leads | ❌ Cada agente tiene su `OWNER_USER_ID` (UUID en Qobrix) |
| Email IMAP (donde llegan los leads) | ❌ Cada agente su `GMAIL_USER` + `GMAIL_APP_PASSWORD` |
| Google Calendar | ❌ Cada agente su `GOOGLE_REFRESH_TOKEN` (OAuth propio) |
| Web Push (PWA iPhone) | ❌ Cada agente su `WEBPUSH_SUBSCRIPTIONS` |
| VAPID keys | ✅ Compartidas (las del PWA `if-real-estate-pwa`) |

---

## ✅ Pre-requisitos del agente nuevo

1. Tener **Gmail** (donde le lleguen los emails de los portales).
2. Tener su **Google Calendar** (donde anote visitas/llamadas).
3. Tener un **iPhone** con la PWA `IF` instalada (`https://ivanfaura9.github.io/ifrealestate-pwa/`).
4. Que el admin de **Qobrix** (Iván) le haya creado **un usuario** y le haya pasado:
   - Su `OWNER_USER_ID` (UUID en Qobrix)
5. Que el admin le pase las claves compartidas:
   - `QOBRIX_URL`, `QOBRIX_USER`, `QOBRIX_KEY` (del bot Idealista)
   - `VAPID_PRIVATE_KEY`, `VAPID_EMAIL`

---

## 🚀 Setup paso a paso

### Paso 1 — Crear el repo del agente (1 min)

1. Ir a `https://github.com/ivanfaura9/idealista-qobrix`
2. Click verde **"Use this template" → "Create a new repository"**
3. Nombre del repo: `idealista-qobrix-AGENTENAME` (ej: `idealista-qobrix-marta`)
4. **Visibility: Public** (importante: cron de 5 min solo funciona en repos públicos free)
5. Crear

### Paso 2 — Generar Gmail App Password (3 min)

1. Ir a `https://myaccount.google.com/apppasswords`
2. (Si pide activar 2FA, hacerlo)
3. Crear contraseña de aplicación llamada "IF Real Estate IMAP"
4. Apuntar la contraseña de 16 caracteres → es el `GMAIL_APP_PASSWORD`

### Paso 3 — Configurar Google Calendar OAuth (10 min)

1. Ir a `https://console.cloud.google.com/`
2. Crear nuevo proyecto: `if-real-estate-AGENTENAME`
3. Activar **Google Calendar API**:
   - Menu → APIs & Services → Library → buscar "Google Calendar API" → Enable
4. Configurar **OAuth consent screen**:
   - User type: External
   - App name: "IF Real Estate Automations"
   - User support email: el email del agente
   - Add test user: el email del agente
   - Crear y **publicar a producción** (botón "Publish app") para que el refresh token no expire en 7 días
5. Crear **OAuth Client ID**:
   - Type: Desktop app
   - Name: "IF Calendar Sync"
   - Descargar el JSON con `client_id` y `client_secret`

### Paso 4 — Obtener refresh token (2 min)

En el Mac del agente, con Python 3 instalado:

```bash
git clone https://github.com/ivanfaura9/idealista-qobrix-AGENTENAME.git
cd idealista-qobrix-AGENTENAME
python3 setup_new_agent.py
```

El script abre el navegador, el agente da OK, y guarda el `refresh_token`.

### Paso 5 — Suscribir el PWA al móvil (2 min)

1. En el iPhone abrir Safari → `https://ivanfaura9.github.io/ifrealestate-pwa/`
2. Compartir → "Añadir a pantalla de inicio"
3. Abrir la app `IF`
4. Click "Activar notificaciones" → permitir
5. La app muestra el JSON de la suscripción → copiarlo (es el `WEBPUSH_SUBSCRIPTIONS`)

### Paso 6 — Cargar todos los Secrets en GitHub (3 min)

En el repo nuevo del agente: **Settings → Secrets and variables → Actions → New repository secret**

Crear estos 14 secrets:

| Secret | Valor |
|---|---|
| `GMAIL_USER` | email del agente |
| `GMAIL_APP_PASSWORD` | (paso 2) |
| `HOSTINGER_USER` | (vacío o si tiene buzón Hostinger) |
| `HOSTINGER_PASSWORD` | (idem) |
| `QOBRIX_URL` | `https://ifrealestate4571.eu1.qobrix.com` |
| `QOBRIX_USER` | (del admin) |
| `QOBRIX_KEY` | (del admin) |
| `OWNER_USER_ID` | UUID del agente en Qobrix (del admin) |
| `GOOGLE_CLIENT_ID` | (paso 3, JSON OAuth) |
| `GOOGLE_CLIENT_SECRET` | (paso 3, JSON OAuth) |
| `GOOGLE_REFRESH_TOKEN` | (paso 4, output del script) |
| `VAPID_PRIVATE_KEY` | (compartido — del admin) |
| `VAPID_EMAIL` | email del agente |
| `WEBPUSH_SUBSCRIPTIONS` | (paso 5, JSON de la app PWA) |

### Paso 7 — Ajustar IDs de calendarios (2 min)

Editar `calendar_sync.py` y `daily_briefing.py` → cambiar `DEFAULT_CALENDARS` por los IDs de los calendarios del agente. Para listarlos:

```bash
python3 setup_new_agent.py --list-calendars
```

Buscar los calendarios de **trabajo** (visitas, llamadas con clientes), copiar sus IDs, pegarlos en el array.

> **Importante:** NO incluir el calendar de "Valoracion propiedad" si lo gestiona GHL automáticamente — para evitar duplicados.

### Paso 8 — Test (1 min)

1. En GitHub del repo del agente: **Actions → Calendar sync → Run workflow**
2. Verificar que termina ✅
3. Mismo con `Daily briefing` y `Stale leads`
4. A los pocos minutos llega el push al iPhone

✅ **Listo.** A partir de aquí corre 24/7.

---

## 🔍 Cómo verificar que funciona

- En el repo del agente: **Actions** muestra los runs cada 5 min (Idealista→Qobrix), 15 min (Calendar), y los nocturnos.
- Si entra un lead a su Gmail desde Idealista/Fotocasa/Habitaclia/Milanuncios → en menos de 5 min llega push al iPhone y aparece en Qobrix como Oportunidad.
- Si añade un evento a Calendar con título tipo "Visita ... con NOMBRE" → en menos de 15 min aparece como Reunión en Qobrix (vinculada al contacto si existe).

---

## 🛠️ Mantenimiento

- **Refresh token Google expira:** si la app OAuth está en Production no caduca. Si está en Testing caduca cada 7 días → re-correr `setup_new_agent.py`.
- **Cambiar la suscripción del PWA:** si el agente reinstala la PWA, su suscripción cambia → actualizar el secret `WEBPUSH_SUBSCRIPTIONS`.
- **Añadir/quitar calendarios:** editar `DEFAULT_CALENDARS` en `calendar_sync.py` y `daily_briefing.py`.

---

## 📞 Soporte

Repo plantilla mantenido por Iván. Cualquier problema con un agente, abrir issue en el **template** repo (no en el del agente).
