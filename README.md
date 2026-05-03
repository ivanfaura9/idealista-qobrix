# Idealista → Qobrix monitor (cloud)

Captura leads de Idealista que llegan por email y los crea como Contacto +
Oportunidad en Qobrix CRM. Corre en GitHub Actions cada 5 minutos. No
necesita ningún ordenador encendido.

## Cómo funciona

1. Cada 5 min un workflow de GitHub Actions arranca un runner de Ubuntu.
2. Conecta por IMAP a Gmail (`ivanfaurar@gmail.com`) y a Hostinger
   (`info@ifrealestate.es`) con credenciales guardadas como Secrets del repo.
3. Busca emails que cumplan el filtro
   `FROM "idealista" SUBJECT "Nuevo mensaje de"` desde el 1 de enero de 2026.
4. Para cada email nuevo:
   - Parsea nombre / email / teléfono / URL del inmueble.
   - Crea un Contacto en Qobrix vía API.
   - Crea una Oportunidad ligada al contacto.
5. Marca el email como procesado en `processed_ids.json` y commitea el
   archivo de vuelta al repo, así no se reprocesan en la siguiente ejecución.

## Secrets requeridos

Configurados en *Settings → Secrets and variables → Actions*:

| Secret | Descripción |
|---|---|
| `GMAIL_USER` | `ivanfaurar@gmail.com` |
| `GMAIL_APP_PASSWORD` | App password de Gmail |
| `HOSTINGER_USER` | `info@ifrealestate.es` |
| `HOSTINGER_PASSWORD` | Contraseña de la cuenta IMAP de Hostinger |
| `QOBRIX_URL` | `https://ifrealestate4571.eu1.qobrix.com` |
| `QOBRIX_USER` | UUID de usuario API |
| `QOBRIX_KEY` | API key |

## Lanzar manualmente

*Actions → "Idealista to Qobrix monitor" → Run workflow*.

## Logs

- Cada ejecución sube `idealista_qobrix.log` como artifact (se guarda 7 días).
- Si una ejecución falla, GitHub manda email automático al dueño del repo.
