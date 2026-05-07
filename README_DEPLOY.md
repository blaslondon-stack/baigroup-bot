# BAI Group Bot — Deploy con endpoint /notify

## Cambios respecto a la versión anterior

1. `TELEGRAM_TOKEN` ya **no está hardcodeado** — se lee de `os.environ["TELEGRAM_TOKEN"]`. Si no está seteado, el bot crashea en el arranque (esto es a propósito, mejor falla ruidosa que correr con un token viejo).
2. Nuevo módulo `notify_api.py` — expone `POST /notify` y `GET /health` en el puerto `$PORT` (Railway lo setea solo).
3. El endpoint corre en un **thread daemon** separado, así no toca el event loop de python-telegram-bot. Si el thread muere, el bot sigue funcionando.

## Pasos de deploy

### 1. Rotar el token del bot

Antes de tocar nada en Railway:

- Abrir @BotFather en Telegram → `/mybots` → `BAIGroup1Bot` → API Token → **Revoke current token**.
- Anotar el token nuevo.

### 2. Generar el NOTIFY_SECRET

En tu Mac:

```bash
openssl rand -base64 32
```

Anotar la salida. Ese va a ser tu `NOTIFY_SECRET`.

### 3. Variables de entorno en Railway

En el dashboard del proyecto `baigroup-bot` → Variables:

| Variable | Valor |
|----------|-------|
| `TELEGRAM_TOKEN` | el token NUEVO del paso 1 |
| `ANTHROPIC_API_KEY` | (la que ya tenés) |
| `NOTIFY_SECRET` | la salida del paso 2 |

`PORT` lo agrega Railway solo, no lo toques.

### 4. Exponer el puerto

Railway → Settings del servicio → **Networking** → **Generate Domain**.
Te va a dar una URL tipo `baigroup-bot-production.up.railway.app`. Anotala.

### 5. Subir los archivos

Reemplazá en el repo:
- `baigroup_bot.py` (versión nueva, sin token hardcodeado)
- `notify_api.py` (nuevo)
- `requirements.txt` (con fastapi y uvicorn)

Commit + push. Railway hace redeploy automático.

### 6. Verificar

Una vez deployado, en tu terminal:

```bash
# Health check (no requiere auth)
curl https://TU_URL.up.railway.app/health
# Debería responder: {"status":"ok"}

# Test de envío al grupo
curl -X POST https://TU_URL.up.railway.app/notify \
  -H "Authorization: Bearer TU_NOTIFY_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"prueba desde curl"}'
# Debería responder: {"ok":true,"message_id":...}
# Y debería llegar el mensaje al grupo BAI Group-OPERACIONES
```

Si las dos cosas funcionan, está todo OK.

### 7. Snippet para Claude in Chrome

Una vez verificado, podés usar este snippet desde cualquier pestaña con la extensión:

```javascript
fetch('https://TU_URL.up.railway.app/notify', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer TU_NOTIFY_SECRET'
  },
  body: JSON.stringify({ text: 'tu mensaje acá' })
}).then(r => r.json()).then(d => { window._r = d; });
```

## Seguridad — qué cubre y qué no

**Cubre:**
- El token de Telegram solo vive en env vars de Railway. No aparece en el código, ni en el browser, ni en chats.
- El endpoint solo puede mandar al grupo `-5265832156`. Hardcoded.
- Auth por bearer token; sin él, todo POST devuelve 401.
- Si el `NOTIFY_SECRET` se filtra, lo rotás en Railway sin tocar @BotFather.

**No cubre:**
- Si alguien con acceso a tu sesión de Claude in Chrome hace que Claude lea una página con prompt injection, podría llegar a usar el snippet. Mitigación parcial: el endpoint solo escribe, no lee, y solo a un destino. Lo peor es spam al grupo.
- Rate limiting: no implementado. Si te preocupa abuso, agregar `slowapi` o un middleware simple que limite a N requests/min.
