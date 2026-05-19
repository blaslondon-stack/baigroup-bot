import os
import re
import json
import httpx
import asyncio
import anthropic
from datetime import datetime, timedelta, time as dtime

# Playwright para scraping BCRA con cheques rechazados
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Endpoint HTTP /notify (corre en thread separado)
from notify_api import start_notify_api

# TOKENS — leer SIEMPRE de env vars (nunca hardcodear)
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# SEGURIDAD — solo responde en este grupo y a este usuario
GRUPO_PERMITIDO = -5265832156
USUARIOS_PERMITIDOS = {55179603}  # Agregar más IDs acá si sumás equipo

async def check_acceso(update: Update) -> bool:
    """Verifica que el mensaje viene del grupo autorizado o del usuario autorizado"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    # Permitir si es el grupo correcto O el usuario autorizado en privado
    if chat_id == GRUPO_PERMITIDO:
        return True
    if user_id in USUARIOS_PERMITIDOS:
        return True
    return False

# In-memory cartera (para demo - en produccion usar DB)
cartera = {}

# ─────────────────────────────────────────────
# BCRA API
# ─────────────────────────────────────────────
async def consultar_bcra(cuit: str) -> dict:
    cuit_limpio = re.sub(r"[-\s]", "", cuit)
    url = f"https://api.bcra.gob.ar/centraldedeudores/v1.0/deudas/{cuit_limpio}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "es-AR,es;q=0.9",
    }
    # Intentar 2 veces — inmediato + 1 reintento rápido
    ultimo_error = ""
    for intento in range(2):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                verify=False,
                follow_redirects=True
            ) as client:
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    return {"ok": True, "data": r.json()}
                elif r.status_code == 404:
                    return {"ok": False, "error": "CUIT sin deuda registrada en Central de Deudores"}
                else:
                    ultimo_error = f"HTTP {r.status_code}"
        except Exception as e:
            ultimo_error = str(e)
        if intento == 0:
            await asyncio.sleep(1)
    return {"ok": False, "error": f"Sin conexión con BCRA: {ultimo_error}"}

async def consultar_cheques_rechazados(cuit: str) -> dict:
    cuit_limpio = re.sub(r"[-\s]", "", cuit)
    url = f"https://api.bcra.gob.ar/centraldedeudores/v1.0/cheques/{cuit_limpio}/rechazados"
    try:
        async with httpx.AsyncClient(timeout=15, verify=False, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                data = r.json()
                cheques = []
                results = data.get("results", {})
                if isinstance(results, dict):
                    cheques = results.get("cheques", [])
                elif isinstance(results, list):
                    cheques = results
                if not cheques:
                    cheques = data.get("cheques", [])
                return {"ok": True, "cheques": cheques}
            else:
                return {"ok": True, "cheques": []}
    except:
        return {"ok": True, "cheques": []}

# ─────────────────────────────────────────────
# CLAUDE ANALYSIS
# ─────────────────────────────────────────────
async def analizar_con_claude(cuit: str, bcra_data: dict, cheques_data: dict) -> str:
    if not ANTHROPIC_API_KEY:
        return analizar_sin_claude(cuit, bcra_data, cheques_data)
    
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        
        # Extraer flags críticos de la estructura BCRA
        results_data = bcra_data.get("results", {})
        periodos = results_data.get("periodos", [])
        flags_detectados = []
        for periodo in periodos:
            for entidad in periodo.get("entidades", []):
                nombre_e = entidad.get("entidad", "")
                if entidad.get("refinanciaciones"):
                    flags_detectados.append(f"⚠️ {nombre_e}: refinanciaciones activas")
                if entidad.get("recategorizacionOblig"):
                    flags_detectados.append(f"🔴 {nombre_e}: recategorización obligatoria por BCRA")
                if entidad.get("situacionJuridica"):
                    flags_detectados.append(f"🔴 {nombre_e}: situación jurídica (concurso/quiebra)")
                if entidad.get("irrecDisposicionTecnica"):
                    flags_detectados.append(f"🔴 {nombre_e}: irrecuperable por disposición técnica")
                if entidad.get("procesoJud"):
                    flags_detectados.append(f"🔴 {nombre_e}: proceso judicial activo")
                if entidad.get("enRevision"):
                    flags_detectados.append(f"⚠️ {nombre_e}: clasificación en revisión")
                dias = entidad.get("diasAtrasoPago", 0) or 0
                if dias > 0:
                    flags_detectados.append(f"⚠️ {nombre_e}: {dias} días de atraso")

        flags_txt = "\n".join(flags_detectados) if flags_detectados else "Ninguno"

        prompt = f"""Sos el agente de crédito de BAI Group SA, financiera argentina especializada en descuento de cheques.

Analizá la siguiente información del BCRA para el CUIT {cuit}.

DATOS BCRA COMPLETOS:
{json.dumps(bcra_data, ensure_ascii=False, indent=2)}

FLAGS CRÍTICOS DETECTADOS AUTOMÁTICAMENTE:
{flags_txt}

CHEQUES RECHAZADOS EN API ({len(cheques_data.get("cheques", []))} registros):
{json.dumps(cheques_data.get("cheques", [])[:5], ensure_ascii=False, indent=2)}

SEMÁFORO BAI GROUP:
✅ APROBAR: Sit 1 en todas las entidades, sin flags, sin refinanciaciones
🟡 CON CONDICIONES: Sit 1 con algún flag menor (enRevision, días atraso leve) o Sit 2 con buen historial
🟠 ALTO RIESGO: Sit 2 con flags, o Sit 1 con refinanciaciones/recategorización
❌ RECHAZAR: Sit 3+ / situaciónJuridica / procesoJud / irrecDisposicionTecnica / recategorizacionOblig

CRITERIOS ADICIONALES:
- Refinanciaciones activas = empresa en dificultades, aumentar tasa o rechazar según monto
- recategorizacionOblig = el BCRA la forzó a bajar categoría, muy negativo
- Deuda con 3+ entidades simultáneas = analizar concentración
- Monto total >$500M = exposición alta
- Mostrar montos en millones (ej: $27,63M) no en miles, exigir mayor tasa

Respondé en este formato:

🏢 *LIBRADOR:* [nombre]
🔢 *CUIT:* {cuit}

📊 *SITUACIÓN ACTUAL:*
[Cada entidad: nombre | Sit X | $monto | flags si tiene]

🚨 *FLAGS DETECTADOS:*
[Lista de flags o "Sin flags críticos ✅"]

💰 *EXPOSICIÓN TOTAL:*
[Suma de deuda y cantidad de entidades]

🎯 *RECOMENDACIÓN:*
[✅ APROBAR / 🟡 CON CONDICIONES / 🟠 ALTO RIESGO / ❌ RECHAZAR]
[Justificación concreta en 2-3 líneas]

📋 *CONDICIONES:*
[Tasa sugerida, monto máximo, o "Sin condiciones adicionales"]"""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        return analizar_sin_claude(cuit, bcra_data, cheques_data)

def analizar_sin_claude(cuit: str, bcra_data: dict, cheques_data: dict) -> str:
    """Análisis básico sin Claude como fallback"""
    try:
        results = bcra_data.get("results", {})
        nombre = results.get("denominacion", "Desconocido")
        periodos = results.get("periodos", [])
        
        # Obtener situación más reciente
        sit_actual = 1
        entidades_lineas = []
        alertas = []
        
        for periodo in periodos[:3]:  # últimos 3 periodos
            for entidad in periodo.get("entidades", []):
                sit = entidad.get("situacion", 1)
                monto = entidad.get("monto", 0)
                nombre_entidad = entidad.get("entidad", "")
                if sit > sit_actual:
                    sit_actual = sit
                monto_m = monto / 1000
                if monto_m >= 1:
                    monto_fmt = f"${monto_m:,.2f}M"
                else:
                    monto_fmt = f"${monto:,.0f}k"
                entidades_lineas.append(f"• {nombre_entidad}: Sit. {sit} | {monto_fmt}")
        
        # Cheques rechazados
        cheques = cheques_data.get("cheques", [])
        if cheques:
            # Contar solo SIN FONDOS vs otros
            sin_fondos = [c for c in cheques if "FONDOS" in str(c).upper()]
            total = len(cheques)
            alertas.append(f"🔴 {total} cheque(s) rechazado(s) — {len(sin_fondos)} por SIN FONDOS")
            # Mostrar los 3 más recientes
            for c in cheques[:3]:
                fecha = c.get("fechaRechazo", c.get("fecha", ""))
                monto = c.get("monto", "")
                causal = c.get("causal", c.get("causa", ""))
                alertas.append(f"  → {fecha} | ${monto:,} | {causal}")
        
        # Semáforo
        if sit_actual == 1 and not cheques:
            semaforo = "✅ APROBAR"
        elif sit_actual == 2 or (sit_actual == 1 and cheques):
            semaforo = "🟡 APROBAR CON CONDICIONES"
        else:
            semaforo = "❌ RECHAZAR"

        entidades_txt = "\n".join(entidades_lineas) if entidades_lineas else "Sin deuda reportada"
        alertas_txt = "\n".join(alertas) if alertas else "Sin alertas"

        return f"""🏢 *LIBRADOR:* {nombre}
🔢 *CUIT:* {cuit}

📊 *SITUACIÓN ACTUAL:*
{entidades_txt}

🚨 *ALERTAS:*
{alertas_txt}

🎯 *RECOMENDACIÓN:*
{semaforo}"""
    except:
        return f"✅ Consulta BCRA exitosa para CUIT {cuit}\nRevisá los datos manualmente."

# ─────────────────────────────────────────────
# CALCULAR DESCUENTO
# ─────────────────────────────────────────────
def calcular_descuento(monto: float, dias: int, tna: float) -> dict:
    tasa_periodo = (tna / 100) * (dias / 360)
    interes = monto * tasa_periodo
    neto = monto - interes
    # CFT = tasa efectiva anual compuesta base 360
    tea = ((1 + tasa_periodo) ** (360 / dias) - 1) * 100
    cft = round(tea, 2)
    return {
        "monto_nominal": monto,
        "dias": dias,
        "tna": tna,
        "interes": round(interes, 2),
        "neto_a_acreditar": round(neto, 2),
        "cft_anual": round(cft, 2)
    }

# ─────────────────────────────────────────────
# COMANDOS
# ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_acceso(update): return
    texto = """🏦 *BAI Group SA — Bot Operativo*

Comandos disponibles:

🔍 `/evaluar [CUIT]` — Evaluación crediticia vía BCRA
🔍 `/evaluar_completo [CUIT]` — Evaluación completa con cheques
📋 `/analizar [texto]` — Pegá texto del BCRA para análisis completo
🔎 `/buscar [nombre]` — Buscar por cliente o titular en cartera
💰 `/cotizar [monto] [días] [tna]` — Cotización de cheque
📋 `/nuevo [CUIT] [monto] [días] [tna]` — Registrar operación
📊 `/cheques` — Pendientes sin destinatario (Google Sheets)
📅 `/hoy` — Disponibles para depositar hoy
📅 `/semana` — Se habilitan esta semana (ECHEQ vs físico)
📅 `/manana` — Cheques que se habilitan mañana
⏰ `/vencer` — Próximos a vencer en cartera interna
📈 `/cartera` — Resumen cartera interna

_Ej: /evaluar 30578639868_
_Ej: /cotizar 10.000.000 30 144_
_Ej: /cheques_"""
    await update.message.reply_text(texto, parse_mode="Markdown")

async def evaluar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_acceso(update): return
    if not context.args:
        await update.message.reply_text("❌ Usá: `/evaluar [CUIT]`\nEj: `/evaluar 30578639868`", parse_mode="Markdown")
        return

    cuit = re.sub(r"[-\s]", "", context.args[0])
    msg = await update.message.reply_text(f"🔍 Consultando BCRA para CUIT `{cuit}`...", parse_mode="Markdown")

    # Consultar BCRA y cartera simultáneamente
    bcra, cheques, registros = await asyncio.gather(
        consultar_bcra(cuit),
        consultar_cheques_rechazados(cuit),
        leer_cheques_sheet()
    )

    if not bcra["ok"]:
        await msg.edit_text(f"⚠️ *BCRA:* {bcra['error']}\n\nEl CUIT puede no tener deuda registrada en el sistema financiero.", parse_mode="Markdown")
        return

    await msg.edit_text("🤖 Analizando con IA...", parse_mode="Markdown")

    # Analizar
    analisis = await analizar_con_claude(cuit, bcra["data"], cheques)

    # Verificar en cartera del Sheet
    hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    en_cartera = [
        r for r in registros
        if r.get("cuit", "").strip() == cuit
        and r.get("venc_dt") and r["venc_dt"] >= hoy
        and not r.get("destinatario")
    ]

    if en_cartera:
        total_cartera_cuit = sum(r["importe"] for r in en_cartera)
        total_cartera_general = sum(
            r["importe"] for r in registros
            if r.get("venc_dt") and r["venc_dt"] >= hoy
            and not r.get("destinatario")
        )
        pct = (total_cartera_cuit / total_cartera_general * 100) if total_cartera_general > 0 else 0
        alerta_conc = "🔴 *ALERTA CONCENTRACIÓN*" if pct > 20 else ("⚠️ *Concentración moderada*" if pct > 10 else "")

        cartera_txt = (
            f"\n\n📋 *EN CARTERA ACTIVA:*\n"
            f"• {len(en_cartera)} cheques sin depositar — *${total_cartera_cuit:,.0f}*\n"
            f"• Representa el *{pct:.1f}%* de la cartera total"
        )
        if alerta_conc:
            cartera_txt += f"\n• {alerta_conc}"

        analisis += cartera_txt

    await msg.edit_text(analisis, parse_mode="Markdown")

async def cotizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_acceso(update): return
    if len(context.args) < 3:
        await update.message.reply_text(
            "❌ Usá: `/cotizar [monto] [días] [tna]`\nEj: `/cotizar 1000000 90 85`",
            parse_mode="Markdown"
        )
        return

    try:
        # Acepta: 10000000 / 10.000.000 / $10.000.000
        raw = context.args[0].replace("$", "").strip()
        if raw.count(".") > 1:
            raw = raw.replace(".", "")
        elif "," in raw and "." not in raw and len(raw.split(",")[-1]) == 3:
            raw = raw.replace(",", "")
        elif "," in raw and "." not in raw:
            raw = raw.replace(",", ".")
        monto = float(raw)
        dias = int(context.args[1])
        tna = float(context.args[2])

        r = calcular_descuento(monto, dias, tna)

        texto = f"""💰 *COTIZACIÓN DE CHEQUE*

📄 *Valor nominal:* ${r['monto_nominal']:,.0f}
📅 *Días al vencimiento:* {r['dias']} días
📈 *TNA aplicada:* {r['tna']}%

━━━━━━━━━━━━━━━
💸 *Interés a descontar:* ${r['interes']:,.2f}
✅ *Neto a acreditar:* ${r['neto_a_acreditar']:,.2f}
📊 *CFT (TEA):* {r['cft_anual']}%
📅 *Tasa mensual equiv.:* {round((tna/100)*(30/360)*100, 2)}%
━━━━━━━━━━━━━━━

_Vence en {dias} días — {(datetime.now() + timedelta(days=dias)).strftime('%d/%m/%Y')}_"""

        await update.message.reply_text(texto, parse_mode="Markdown")

    except ValueError:
        await update.message.reply_text("❌ Datos inválidos. Usá números: `/cotizar 1000000 90 85`", parse_mode="Markdown")

async def nuevo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_acceso(update): return
    if len(context.args) < 4:
        await update.message.reply_text(
            "❌ Usá: `/nuevo [CUIT] [monto] [días] [tna]`\nEj: `/nuevo 30578639868 1000000 90 85`",
            parse_mode="Markdown"
        )
        return

    try:
        cuit = context.args[0]
        monto = float(context.args[1].replace(",", ""))
        dias = int(context.args[2])
        tna = float(context.args[3])

        r = calcular_descuento(monto, dias, tna)
        vencimiento = datetime.now() + timedelta(days=dias)
        op_id = f"OP{len(cartera)+1:04d}"

        cartera[op_id] = {
            "cuit": cuit,
            "monto": monto,
            "neto": r["neto_a_acreditar"],
            "interes": r["interes"],
            "tna": tna,
            "dias": dias,
            "vencimiento": vencimiento,
            "fecha_alta": datetime.now(),
            "estado": "activa"
        }

        texto = f"""✅ *OPERACIÓN REGISTRADA*

🔖 *ID:* `{op_id}`
🏢 *CUIT:* {cuit}
💵 *Nominal:* ${monto:,.0f}
💸 *Neto acreditado:* ${r['neto_a_acreditar']:,.2f}
📈 *Interés:* ${r['interes']:,.2f} ({tna}% TNA)
📅 *Vencimiento:* {vencimiento.strftime('%d/%m/%Y')} ({dias} días)"""

        await update.message.reply_text(texto, parse_mode="Markdown")

    except ValueError:
        await update.message.reply_text("❌ Datos inválidos.", parse_mode="Markdown")

async def vencer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_acceso(update): return
    hoy = datetime.now()
    proximos = []

    for op_id, op in cartera.items():
        if op["estado"] == "activa":
            dias_restantes = (op["vencimiento"] - hoy).days
            if 0 <= dias_restantes <= 7:
                proximos.append((dias_restantes, op_id, op))

    if not proximos:
        await update.message.reply_text("✅ No hay cheques venciendo en los próximos 7 días.", parse_mode="Markdown")
        return

    proximos.sort(key=lambda x: x[0])
    lineas = ["⏰ *CHEQUES POR VENCER (7 días)*\n"]

    for dias_rest, op_id, op in proximos:
        emoji = "🔴" if dias_rest <= 1 else "🟡" if dias_rest <= 3 else "🟢"
        lineas.append(
            f"{emoji} `{op_id}` — CUIT {op['cuit']}\n"
            f"   💵 ${op['monto']:,.0f} | Vence {op['vencimiento'].strftime('%d/%m/%Y')} ({dias_rest}d)"
        )

    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

async def cartera_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_acceso(update): return
    activas = {k: v for k, v in cartera.items() if v["estado"] == "activa"}

    if not activas:
        await update.message.reply_text("📋 Cartera vacía. Registrá operaciones con `/nuevo`", parse_mode="Markdown")
        return

    total_nominal = sum(op["monto"] for op in activas.values())
    total_interes = sum(op["interes"] for op in activas.values())

    lineas = [f"📊 *CARTERA ACTIVA — {len(activas)} operaciones*\n"]
    for op_id, op in sorted(activas.items(), key=lambda x: x[1]["vencimiento"]):
        dias_rest = (op["vencimiento"] - datetime.now()).days
        lineas.append(f"• `{op_id}` CUIT {op['cuit']} | ${op['monto']:,.0f} | {dias_rest}d")

    lineas.append(f"\n━━━━━━━━━━━━━━━")
    lineas.append(f"💰 *Total nominal:* ${total_nominal:,.0f}")
    lineas.append(f"📈 *Interés total:* ${total_interes:,.2f}")

    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta CUITs en mensajes libres"""
    text = update.message.text or ""
    cuit_match = re.search(r'\b(\d{2}[-\s]?\d{8}[-\s]?\d{1})\b', text)
    if cuit_match and "evaluar" not in text.lower():
        cuit = re.sub(r"[-\s]", "", cuit_match.group(1))
        context.args = [cuit]
        await evaluar(update, context)


# ─────────────────────────────────────────────
# GOOGLE SHEETS — CHEQUES GENERALES
# ─────────────────────────────────────────────
SHEET_ID = "14WQLvak1U_1io5UlGfOMJnuvulOcVma4"
SHEET_GID = "2101330974"

def parse_csv_line(line):
    """Parser CSV con manejo de comillas"""
    result = []
    current = ""
    in_quotes = False
    for char in line:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            result.append(current.strip())
            current = ""
        else:
            current += char
    result.append(current.strip())
    return result

def parse_fecha(s):
    """Parsea fecha DD/MM/YYYY o D/M/YYYY"""
    s = (s or "").strip()
    try:
        parts = s.split("/")
        if len(parts) == 3:
            return datetime(int(parts[2]), int(parts[1]), int(parts[0]))
    except:
        pass
    return None

def parse_importe(s):
    """Parsea importe $1.000.000,00"""
    try:
        return float(re.sub(r"[\$, ]", "", s or ""))
    except:
        return 0

async def leer_cheques_sheet() -> list:
    """Lee el Google Sheet de cheques y retorna lista de registros"""
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    try:
        async with httpx.AsyncClient(timeout=30, verify=False, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return []
        
        lines = r.text.split("\n")
        registros = []
        
        for line in lines[5:]:  # Skip 5 filas de header
            cols = parse_csv_line(line)
            if len(cols) < 10:
                continue
            titular = cols[4].strip() if len(cols) > 4 else ""
            importe_str = cols[6].strip() if len(cols) > 6 else ""
            if not importe_str:
                continue
            # Si no hay titular, usar el número de cheque o "Sin identificar"
            if not titular:
                titular = f"Nro {cols[3].strip()}" if len(cols) > 3 and cols[3].strip() else "Sin identificar"
            
            registros.append({
                "tipo": cols[0].strip(),          # ECHEQ / vacío
                "fecha": cols[1].strip(),          # desde cuándo depositar
                "fecha_dt": parse_fecha(cols[1]),
                "numero": cols[3].strip(),
                "titular": titular,
                "cuit": cols[5].strip(),
                "importe": parse_importe(importe_str),
                "cliente": cols[7].strip(),
                "destinatario": cols[8].strip(),   # vacío = pendiente
                "vencimiento": cols[9].strip(),
                "venc_dt": parse_fecha(cols[9]),
                "estado": cols[11].strip() if len(cols) > 11 else "",
            })
        
        return registros
    except Exception as e:
        return []

async def cheques_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_acceso(update): return
    """Muestra cheques pendientes de depositar (sin destinatario) del Google Sheet"""
    msg = await update.message.reply_text("📊 Consultando planilla de cheques...", parse_mode="Markdown")

    registros = await leer_cheques_sheet()
    if not registros:
        await msg.edit_text("❌ No se pudo leer la planilla. Verificar acceso.", parse_mode="Markdown")
        return

    hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    pendientes = [r for r in registros if not r["destinatario"] and r["venc_dt"] and r["venc_dt"] >= hoy]

    if not pendientes:
        await msg.edit_text("✅ No hay cheques pendientes sin destinatario.", parse_mode="Markdown")
        return

    pendientes.sort(key=lambda x: x["venc_dt"])

    disponibles_hoy = [p for p in pendientes if p["fecha_dt"] and p["fecha_dt"] <= hoy]
    no_disponibles = [p for p in pendientes if not p["fecha_dt"] or p["fecha_dt"] > hoy]

    total = sum(p["importe"] for p in pendientes)
    total_disp = sum(p["importe"] for p in disponibles_hoy)

    urgentes = [p for p in disponibles_hoy if p["venc_dt"] and (p["venc_dt"] - hoy).days <= 7]
    esta_semana = [p for p in disponibles_hoy if p["venc_dt"] and 7 < (p["venc_dt"] - hoy).days <= 30]
    mas_adelante = [p for p in disponibles_hoy if p["venc_dt"] and (p["venc_dt"] - hoy).days > 30]

    lineas = ["📋 *CHEQUES PENDIENTES — SIN DESTINATARIO*\n"]
    lineas.append(f"💰 Total: {len(pendientes)} cheques | *${total:,.0f}*")
    lineas.append(f"✅ Disponibles para depositar: {len(disponibles_hoy)} | *${total_disp:,.0f}*")
    lineas.append(f"⏳ No disponibles aún: {len(no_disponibles)}\n")

    if urgentes:
        lineas.append(f"🔴 *URGENTE — vencen en 7 días ({len(urgentes)} cheques)*")
        for p in urgentes[:10]:
            dias = (p["venc_dt"] - hoy).days
            titular = p["titular"][:25]
            venc = p["vencimiento"]
            imp = p["importe"]
            cli = p["cliente"]
            cuit = p["cuit"]
            lineas.append(f"• {venc} ({dias}d) | {titular} | ${imp:,.0f} | {cli}")
            if cuit:
                lineas.append(f"  👉 /evaluar {cuit}")

    if esta_semana:
        lineas.append(f"\n🟡 *PRÓXIMOS 30 DÍAS ({len(esta_semana)} cheques)*")
        for p in esta_semana[:8]:
            dias = (p["venc_dt"] - hoy).days
            titular = p["titular"][:25]
            venc = p["vencimiento"]
            imp = p["importe"]
            cuit = p["cuit"]
            lineas.append(f"• {venc} ({dias}d) | {titular} | ${imp:,.0f}")
            if cuit:
                lineas.append(f"  👉 /evaluar {cuit}")

    if mas_adelante:
        total_ma = sum(p["importe"] for p in mas_adelante)
        lineas.append(f"\n🟢 *+30 DÍAS: {len(mas_adelante)} cheques — ${total_ma:,.0f}*")

    if no_disponibles:
        total_nd = sum(p["importe"] for p in no_disponibles)
        proximos_nd = sorted(no_disponibles, key=lambda x: x["fecha_dt"] or datetime.max)[:3]
        lineas.append(f"\n⏳ *NO DISPONIBLES AÚN — ${total_nd:,.0f}*")
        for p in proximos_nd:
            lineas.append(f"• Disponible {p['fecha']} | {p['titular'][:25]} | ${p['importe']:,.0f}")

    await msg.edit_text("\n".join(lineas), parse_mode="Markdown")

async def cheques_hoy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_acceso(update): return
    """Muestra solo los cheques disponibles para depositar HOY"""
    msg = await update.message.reply_text("Consultando cheques disponibles hoy...", parse_mode="Markdown")
    registros = await leer_cheques_sheet()
    if not registros:
        await msg.edit_text("No se pudo leer la planilla.", parse_mode="Markdown")
        return
    hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    disponibles = [
        r for r in registros
        if not r["destinatario"]
        and r["fecha_dt"] and r["fecha_dt"] <= hoy
        and r["venc_dt"] and r["venc_dt"] >= hoy
    ]
    if not disponibles:
        await msg.edit_text("No hay cheques disponibles para depositar hoy sin destinatario.", parse_mode="Markdown")
        return
    disponibles.sort(key=lambda x: x["venc_dt"])
    total = sum(p["importe"] for p in disponibles)
    lineas = ["*CHEQUES DISPONIBLES HOY*", f"Total: {len(disponibles)} cheques | ${total:,.0f}", ""]
    for p in disponibles:
        dias_venc = (p["venc_dt"] - hoy).days
        tipo = "ECHEQ" if p["tipo"] == "ECHEQ" else "FISICO"
        emoji = "🔴" if dias_venc <= 3 else "🟡" if dias_venc <= 7 else "🟢"
        titular = p["titular"][:28]
        numero = p["numero"]
        imp = p["importe"]
        cli = p["cliente"]
        venc = p["vencimiento"]
        cuit = p["cuit"]
        linea = (f"{emoji} *{titular}* | {tipo} Nro:{numero} | "
                 f"${imp:,.0f} | {cli} | Vence:{venc}({dias_venc}d)")
        lineas.append(linea)
        if cuit:
            lineas.append(f"   👉 /evaluar {cuit}")
    await msg.edit_text("\n".join(lineas), parse_mode="Markdown")



async def semana_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_acceso(update): return
    """Muestra cheques que se acreditan en los próximos 7 días, separados por tipo"""
    msg = await update.message.reply_text("📅 Consultando próximos 7 días...", parse_mode="Markdown")

    registros = await leer_cheques_sheet()
    if not registros:
        await msg.edit_text("❌ No se pudo leer la planilla.", parse_mode="Markdown")
        return

    hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    limite = hoy + timedelta(days=7)

    # Cheques SIN destinatario cuya FECHA DE HABILITACIÓN (col B) cae esta semana
    # Es decir: se habilitan para depositar entre hoy y los próximos 7 días
    proximos = [
        r for r in registros
        if not r["destinatario"]
        and r["venc_dt"] and r["venc_dt"] >= hoy  # no vencidos
        and r["fecha_dt"]
        and hoy <= r["fecha_dt"] <= limite  # se habilitan esta semana
    ]

    if not proximos:
        await msg.edit_text("✅ No hay cheques venciendo en los próximos 7 días.", parse_mode="Markdown")
        return

    proximos.sort(key=lambda x: x["venc_dt"])

    # Separar por tipo
    echeq = [p for p in proximos if p["tipo"] == "ECHEQ"]
    fisicos = [p for p in proximos if p["tipo"] != "ECHEQ"]

    # Separar por estado (depositado vs pendiente)
    def formato_cheque(p):
        cuit = p["cuit"]
        titular = p["titular"][:28]
        dias_venc = (p["venc_dt"] - hoy).days if p.get("venc_dt") else 0
        lineas = [f"⚠️ *{titular}* | ${p['importe']:,.0f}"]
        lineas.append(f"   🟢 Disponible: {p['fecha']} | Vence: {p['vencimiento']} ({dias_venc}d) | {p['cliente'][:20]}")
        if cuit:
            lineas.append(f"   👉 /evaluar {cuit}")
        return "\n".join(lineas)

    total_echeq = sum(p["importe"] for p in echeq)
    total_fisicos = sum(p["importe"] for p in fisicos)
    total = total_echeq + total_fisicos

    lineas = [f"📅 *SE HABILITAN ESTA SEMANA — {len(proximos)} cheques*\n"]
    lineas.append(f"💰 Total pendiente: *${total:,.0f}*\n")

    if echeq:
        lineas.append(f"💻 *ECHEQ — {len(echeq)} cheques — ${total_echeq:,.0f}*")
        lineas.append(f"_(acreditación automática al vencimiento)_\n")
        for p in echeq:
            lineas.append(formato_cheque(p))
            lineas.append("")

    if fisicos:
        lineas.append(f"📄 *FÍSICOS — {len(fisicos)} cheques — ${total_fisicos:,.0f}*")
        lineas.append(f"_(requieren depósito manual en banco)_\n")
        for p in fisicos:
            lineas.append(formato_cheque(p))
            lineas.append("")

    # Dividir en mensajes si es muy largo
    texto = "\n".join(lineas)
    if len(texto) > 4000:
        await msg.edit_text(texto[:4000], parse_mode="Markdown")
        await update.message.reply_text(texto[4000:], parse_mode="Markdown")
    else:
        await msg.edit_text(texto, parse_mode="Markdown")


# ─────────────────────────────────────────────
# COMANDO /MAÑANA
# ─────────────────────────────────────────────
async def manana_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cheques que se habilitan mañana para depositar"""
    if not await check_acceso(update): return
    msg = await update.message.reply_text("📅 Consultando cheques de mañana...", parse_mode="Markdown")

    registros = await leer_cheques_sheet()
    if not registros:
        await msg.edit_text("❌ No se pudo leer la planilla.", parse_mode="Markdown")
        return

    hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    manana = hoy + timedelta(days=1)

    disponibles = [
        r for r in registros
        if not r["destinatario"]
        and r["fecha_dt"] and r["fecha_dt"].date() == manana.date()
        and r["venc_dt"] and r["venc_dt"] >= hoy
    ]

    if not disponibles:
        await msg.edit_text(f"✅ No hay cheques que se habiliten mañana ({manana.strftime('%d/%m/%Y')}).", parse_mode="Markdown")
        return

    disponibles.sort(key=lambda x: x["venc_dt"])
    total = sum(p["importe"] for p in disponibles)

    echeq = [p for p in disponibles if p["tipo"] == "ECHEQ"]
    fisicos = [p for p in disponibles if p["tipo"] != "ECHEQ"]

    lineas = [f"📅 *MAÑANA {manana.strftime('%d/%m/%Y')} — {len(disponibles)} cheques*\n"]
    lineas.append(f"💰 Total: *${total:,.0f}*\n")

    if echeq:
        lineas.append(f"💻 *ECHEQ — {len(echeq)} cheques — ${sum(p['importe'] for p in echeq):,.0f}*")
        for p in echeq:
            dias_venc = (p["venc_dt"] - hoy).days
            cuit = p["cuit"]
            lineas.append(f"• *{p['titular'][:28]}* | ${p['importe']:,.0f} | Vence: {p['vencimiento']} ({dias_venc}d) | {p['cliente']}")
            if cuit:
                lineas.append(f"  👉 /evaluar {cuit}")

    if fisicos:
        lineas.append(f"\n📄 *FÍSICOS — {len(fisicos)} cheques — ${sum(p['importe'] for p in fisicos):,.0f}*")
        for p in fisicos:
            dias_venc = (p["venc_dt"] - hoy).days
            cuit = p["cuit"]
            lineas.append(f"• *{p['titular'][:28]}* | ${p['importe']:,.0f} | Vence: {p['vencimiento']} ({dias_venc}d) | {p['cliente']}")
            if cuit:
                lineas.append(f"  👉 /evaluar {cuit}")

    await msg.edit_text("\n".join(lineas), parse_mode="Markdown")

# ─────────────────────────────────────────────
# ALERTA MATUTINA AUTOMÁTICA
# ─────────────────────────────────────────────
async def alerta_matutina(context):
    """Se ejecuta automáticamente cada mañana a las 8hs"""
    try:
        registros = await leer_cheques_sheet()
        if not registros:
            return

        hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        manana = hoy + timedelta(days=1)
        semana = hoy + timedelta(days=7)

        # Habilitados hoy
        hoy_list = [
            r for r in registros
            if not r["destinatario"]
            and r["fecha_dt"] and r["fecha_dt"].date() == hoy.date()
            and r["venc_dt"] and r["venc_dt"] >= hoy
        ]

        # Habilitados mañana
        manana_list = [
            r for r in registros
            if not r["destinatario"]
            and r["fecha_dt"] and r["fecha_dt"].date() == manana.date()
            and r["venc_dt"] and r["venc_dt"] >= hoy
        ]

        # Vencen esta semana sin destinatario
        vencen_semana = [
            r for r in registros
            if not r["destinatario"]
            and r["venc_dt"]
            and hoy <= r["venc_dt"] <= semana
        ]

        # Total cartera pendiente
        total_cartera = sum(
            r["importe"] for r in registros
            if not r["destinatario"]
            and r["venc_dt"] and r["venc_dt"] >= hoy
        )

        # Construir mensaje
        fecha_hoy = hoy.strftime("%d/%m/%Y")
        lineas = [f"☀️ *BUENOS DÍAS — {fecha_hoy}*\n"]

        if hoy_list:
            total_hoy = sum(p["importe"] for p in hoy_list)
            lineas.append(f"🟢 *HOY disponibles para depositar:* {len(hoy_list)} cheques — ${total_hoy:,.0f}")
            for p in hoy_list[:5]:
                lineas.append(f"   • {p['titular'][:25]} | ${p['importe']:,.0f} | {p['cliente']}")
            if len(hoy_list) > 5:
                lineas.append(f"   _...y {len(hoy_list)-5} más. Usá /hoy para ver todos._")
        else:
            lineas.append("🟢 *HOY:* No hay cheques para depositar")

        if manana_list:
            total_manana = sum(p["importe"] for p in manana_list)
            lineas.append(f"\n📅 *MAÑANA se habilitan:* {len(manana_list)} cheques — ${total_manana:,.0f}")
        else:
            lineas.append("\n📅 *MAÑANA:* Sin cheques nuevos")

        if vencen_semana:
            total_vencen = sum(p["importe"] for p in vencen_semana)
            urgentes = [p for p in vencen_semana if (p["venc_dt"] - hoy).days <= 2]
            lineas.append(f"\n⚠️ *Vencen esta semana sin depositar:* {len(vencen_semana)} — ${total_vencen:,.0f}")
            if urgentes:
                lineas.append(f"🔴 *URGENTE ({len(urgentes)} vencen en 2 días):*")
                for p in urgentes:
                    dias = (p["venc_dt"] - hoy).days
                    lineas.append(f"   • {p['titular'][:25]} | ${p['importe']:,.0f} | Vence en {dias}d")

        lineas.append(f"\n💰 *Cartera total pendiente:* ${total_cartera:,.0f}")
        lineas.append("\n_Usá /hoy, /semana o /cheques para más detalle._")

        await context.bot.send_message(
            chat_id=GRUPO_PERMITIDO,
            text="\n".join(lineas),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error en alerta matutina: {e}")


# ─────────────────────────────────────────────
# BCRA SCRAPING CON PLAYWRIGHT (cheques rechazados)
# ─────────────────────────────────────────────
async def consultar_bcra_completo(cuit: str) -> dict:
    """
    Consulta el BCRA web con Playwright para obtener deudas + cheques rechazados.
    Resuelve el Cloudflare Turnstile automáticamente.
    """
    cuit_limpio = re.sub(r"[-\s]", "", cuit)
    
    if not PLAYWRIGHT_AVAILABLE:
        return {"ok": False, "error": "Playwright no disponible"}
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--disable-gpu",
                    "--window-size=1280,800",
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = await context.new_page()
            
            # Navegar al BCRA
            await page.goto(
                f"https://www.bcra.gob.ar/deudores/?cuit={cuit_limpio}",
                wait_until="domcontentloaded",
                timeout=30000
            )
            
            # Esperar a que el JS genere el token y redirija
            # La URL cambia de ?cuit=X a ?cuit=X&ts=Y&token=Z
            try:
                await page.wait_for_url(
                    lambda url: "token=" in url,
                    timeout=15000
                )
            except:
                pass
            
            # Esperar a que cargue el contenido dinámico (cheques rechazados)
            try:
                await page.wait_for_selector(
                    "text=Central de cheques rechazados",
                    timeout=15000
                )
            except:
                # Si no aparece cheques, esperar a que aparezca al menos el nombre
                try:
                    await page.wait_for_selector(
                        "#deudores-resultados",
                        timeout=10000
                    )
                except:
                    await page.wait_for_timeout(5000)
            
            # Esperar un poco más para asegurar carga completa
            await page.wait_for_timeout(2000)
            
            # Extraer el texto completo de la página
            texto = await page.inner_text("body")
            url_final = page.url
            
            await browser.close()
            
            return {
                "ok": True,
                "texto": texto,
                "url": url_final,
                "cuit": cuit_limpio
            }
    except Exception as e:
        return {"ok": False, "error": f"Error Playwright: {str(e)}"}

async def analizar_bcra_completo_con_claude(cuit: str, texto_bcra: str) -> str:
    """Analiza el texto completo del BCRA incluyendo cheques rechazados"""
    if not ANTHROPIC_API_KEY:
        return analizar_texto_bcra_basico(cuit, texto_bcra)
    
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        
        prompt = f"""Sos el agente de crédito de BAI Group SA, financiera especializada en descuento de cheques.

Analizá el siguiente texto extraído de la web del BCRA para el CUIT {cuit}.
El texto incluye: situación crediticia, historial 24 meses Y cheques rechazados.

TEXTO DEL BCRA:
{texto_bcra[:8000]}

POLÍTICA CREDITICIA BAI GROUP:
- Situación 1 sin alertas → APROBAR
- Situación 1 con alertas históricas → APROBAR CON CONDICIONES  
- Situación 2 → APROBAR CON CONDICIONES (tasa mayor)
- Situación 3 o superior → RECHAZAR
- Cheques rechazados SIN FONDOS en últimos 6 meses → RECHAZAR
- Cheques rechazados pagados → Analizar con cuidado

Respondé con este formato:

🏢 *LIBRADOR:* [nombre]
🔢 *CUIT:* {cuit}

📊 *SITUACIÓN BCRA:*
[Lista entidades con situación y monto]

🚨 *CHEQUES RECHAZADOS:*
[Cantidad, montos, causales, si están pagados o no]
[Si no hay: "Sin cheques rechazados ✅"]

📋 *ALERTAS:*
[Alertas detectadas o "Sin alertas"]

🎯 *RECOMENDACIÓN:*
[✅ APROBAR / 🟡 APROBAR CON CONDICIONES / ❌ RECHAZAR]
[Justificación en 2-3 líneas]"""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        return analizar_texto_bcra_basico(cuit, texto_bcra)

def analizar_texto_bcra_basico(cuit: str, texto: str) -> str:
    """Análisis básico del texto del BCRA sin Claude"""
    import re
    
    # Extraer nombre
    nombre_match = re.search(r'Central de Deudores.*?\n([A-ZÁÉÍÓÚÑ][\w\s\.]+?)\n', texto)
    nombre = nombre_match.group(1).strip() if nombre_match else "Desconocido"
    
    # Detectar situaciones
    sits = re.findall(r'Situación\s*(\d)', texto)
    max_sit = max([int(s) for s in sits], default=1) if sits else 1
    
    # Detectar cheques rechazados
    cheques_match = re.search(r'Total cheques rechazados\s+([\d,\.]+)\s+([\d,\.]+)', texto)
    n_cheques = cheques_match.group(1) if cheques_match else "0"
    monto_cheques = cheques_match.group(2) if cheques_match else "0"
    
    sin_fondos = "SIN FONDOS" in texto
    
    # Semáforo
    if sin_fondos or max_sit >= 3:
        semaforo = "❌ RECHAZAR"
    elif max_sit == 2:
        semaforo = "🟡 APROBAR CON CONDICIONES"
    else:
        semaforo = "✅ APROBAR"
    
    cheques_txt = f"{n_cheques} cheques por ${monto_cheques}" if n_cheques != "0" else "Sin cheques rechazados ✅"
    
    return f"""🏢 *LIBRADOR:* {nombre}
🔢 *CUIT:* {cuit}

📊 *SITUACIÓN BCRA:* Máxima Sit. {max_sit}

🚨 *CHEQUES RECHAZADOS:* {cheques_txt}

🎯 *RECOMENDACIÓN:*
{semaforo}"""

async def evaluar_completo(update, context):
    """Evaluación completa con cheques rechazados via Playwright"""
    if not await check_acceso(update): return
    
    if not context.args:
        await update.message.reply_text(
            "❌ Usá: `/evaluar_completo [CUIT]`\nEj: `/evaluar_completo 30718462440`",
            parse_mode="Markdown"
        )
        return
    
    cuit = re.sub(r"[-\s]", "", context.args[0])
    msg = await update.message.reply_text(
        f"🔍 Consultando BCRA completo para CUIT `{cuit}`...\n_(incluye cheques rechazados — puede tardar 10-15 seg)_",
        parse_mode="Markdown"
    )
    
    resultado = await consultar_bcra_completo(cuit)
    
    if not resultado["ok"]:
        # Fallback a la API normal
        await msg.edit_text(f"⚠️ Playwright no disponible: {resultado['error']}\nUsando API básica...", parse_mode="Markdown")
        bcra = await consultar_bcra(cuit)
        cheques = await consultar_cheques_rechazados(cuit)
        if bcra["ok"]:
            analisis = await analizar_con_claude(cuit, bcra["data"], cheques)
            await msg.edit_text(analisis, parse_mode="Markdown")
        return
    
    await msg.edit_text("🤖 Analizando con IA...", parse_mode="Markdown")
    analisis = await analizar_bcra_completo_con_claude(cuit, resultado["texto"])
    await msg.edit_text(analisis, parse_mode="Markdown")


# ─────────────────────────────────────────────
# COMANDO /DOLAR
# ─────────────────────────────────────────────
async def dolar_cmd(update, context):
    """Cotizaciones del dólar desde DolarApi y DolarHoy"""
    if not await check_acceso(update): return
    msg = await update.message.reply_text("💵 Consultando cotizaciones...", parse_mode="Markdown")

    async with httpx.AsyncClient(timeout=10, verify=False, follow_redirects=True) as client:
        try:
            r1 = await client.get("https://dolarapi.com/v1/dolares")
            dolarapi = r1.json() if r1.status_code == 200 else []
        except:
            dolarapi = []

        try:
            r2 = await client.get("https://api.bluelytics.com.ar/v2/latest")
            bluelytics = r2.json() if r2.status_code == 200 else {}
        except:
            bluelytics = {}

    # Parsear DolarApi
    tipos = {}
    for item in dolarapi:
        casa = item.get("casa", "").lower()
        tipos[casa] = item

    # Construir respuesta
    ahora = datetime.now().strftime("%d/%m/%Y %H:%M")
    lineas = [f"💵 *COTIZACIONES USD — {ahora}hs*\n"]

    nombres = [
        ("oficial", "🏦 Oficial"),
        ("blue", "🔵 Blue"),
        ("bolsa", "📈 MEP"),
        ("contadoconliqui", "💹 CCL"),
        ("mayorista", "🌾 Mayorista"),
        ("cripto", "🔐 Cripto"),
    ]

    if tipos:
        lineas.append("📊 *DolarApi.com:*")
        for clave, nombre in nombres:
            if clave in tipos:
                compra = tipos[clave].get("compra", "-")
                venta = tipos[clave].get("venta", "-")
                lineas.append(f"  {nombre}: ${compra} / ${venta}")
    else:
        lineas.append("❌ DolarApi no disponible")

    # Fuente 2: Bluelytics (reemplaza DolarHoy)
    if bluelytics:
        lineas.append("\n📊 *Bluelytics.com.ar:*")
        oficial = bluelytics.get("oficial", {})
        blue = bluelytics.get("blue", {})
        if oficial:
            lineas.append(f"  🏦 Oficial: ${oficial.get('value_buy', '-')} / ${oficial.get('value_sell', '-')}")
        if blue:
            lineas.append(f"  🔵 Blue: ${blue.get('value_buy', '-')} / ${blue.get('value_sell', '-')}")

    lineas.append("\n_Formato: compra / venta_")

    await msg.edit_text("\n".join(lineas), parse_mode="Markdown")



# ─────────────────────────────────────────────
# COMANDO /ANALIZAR — Analiza texto BCRA pegado
# ─────────────────────────────────────────────
async def analizar_cmd(update, context):
    """Analiza texto del BCRA pegado directamente en el chat"""
    if not await check_acceso(update): return

    if not context.args:
        await update.message.reply_text(
            "📋 *Cómo usar /analizar:*\n\n"
            "Pegá el texto del BCRA después del comando:\n"
            "`/analizar [texto completo del BCRA]`\n\n"
            "_Copiá todo el texto de la página del BCRA y pegalo acá._",
            parse_mode="Markdown"
        )
        return

    texto = " ".join(context.args)
    msg = await update.message.reply_text("🤖 Analizando texto del BCRA...", parse_mode="Markdown")

    if not ANTHROPIC_API_KEY:
        await msg.edit_text("❌ API de Claude no configurada.", parse_mode="Markdown")
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""Sos el agente de crédito de BAI Group SA, financiera especializada en descuento de cheques.

Analizá el siguiente texto extraído de la web del BCRA. Puede incluir situación crediticia, historial 24 meses y cheques rechazados.

TEXTO DEL BCRA:
{texto[:6000]}

SEMÁFORO BAI GROUP:
✅ APROBAR: Sit 1, sin flags, sin cheques rechazados recientes
🟡 CON CONDICIONES: Sit 1 con alertas menores o cheques rechazados pagados
🟠 ALTO RIESGO: Sit 2 o cheques rechazados sin pagar
❌ RECHAZAR: Sit 3+ / proceso judicial / cheques SIN FONDOS impagas recientes

Respondé en este formato:

🏢 *LIBRADOR:* [nombre]
🔢 *CUIT:* [cuit]

📊 *SITUACIÓN BCRA:*
[Cada entidad: nombre | Sit X | $monto en M o k]

🚨 *CHEQUES RECHAZADOS:*
[Cantidad, monto total, causales, pagados vs impagas, último rechazo]
[Si no hay: "Sin cheques rechazados ✅"]

⚠️ *HISTORIAL:*
[Alertas del historial 24 meses o "Sin alertas"]

💰 *EXPOSICIÓN TOTAL:*
[Suma en $M y cantidad de entidades]

🎯 *RECOMENDACIÓN:*
[✅ APROBAR / 🟡 CON CONDICIONES / 🟠 ALTO RIESGO / ❌ RECHAZAR]
[Justificación concreta en 2-3 líneas]"""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        await msg.edit_text(response.content[0].text, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Error al analizar: {str(e)}", parse_mode="Markdown")

# ─────────────────────────────────────────────
# COMANDO /BUSCAR — Busca por cliente o titular
# ─────────────────────────────────────────────
async def buscar_cmd(update, context):
    """Busca cheques por nombre de cliente o titular"""
    if not await check_acceso(update): return

    if not context.args:
        await update.message.reply_text(
            "🔍 Usá: `/buscar [texto]`\n\n"
            "Podés buscar por:\n"
            "• Cliente/titular: `/buscar GABUCCI`\n"
            "• Número de cheque: `/buscar 11201006`\n"
            "• Importe: `/buscar 2000000` o `/buscar 2M`\n"
            "• CUIT: `/buscar 20264626268`",
            parse_mode="Markdown"
        )
        return

    query = " ".join(context.args).upper().strip()
    msg = await update.message.reply_text(f"🔍 Buscando *{query}*...", parse_mode="Markdown")

    registros = await leer_cheques_sheet()
    if not registros:
        await msg.edit_text("❌ No se pudo leer la planilla.", parse_mode="Markdown")
        return

    hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Detectar si la query es un importe (números, opcionalmente con M o K)
    importe_buscado = None
    q_clean = query.replace(".", "").replace(",", "").replace("$", "").replace(" ", "")
    if q_clean.endswith("M"):
        try: importe_buscado = float(q_clean[:-1]) * 1_000_000
        except ValueError: pass
    elif q_clean.endswith("K"):
        try: importe_buscado = float(q_clean[:-1]) * 1_000
        except ValueError: pass
    elif q_clean.isdigit() and len(q_clean) >= 6:
        try:
            n = float(q_clean)
            if n >= 100_000: importe_buscado = n
        except ValueError: pass

    query_cuit = query.replace("-", "").replace(" ", "")

    # Buscar en titular, cliente, número de cheque, CUIT e importe
    encontrados = []
    for r in registros:
        titular = r.get("titular", "").upper()
        cliente = r.get("cliente", "").upper()
        numero  = str(r.get("numero", "")).upper()
        cuit    = str(r.get("cuit", "")).replace("-", "").replace(" ", "")

        # Match por texto
        if (query in titular or query in cliente or
            (numero and query in numero) or
            (query_cuit and query_cuit in cuit)):
            encontrados.append(r)
            continue

        # Match por importe (tolerancia $1)
        if importe_buscado is not None:
            try:
                if abs(float(r.get("importe", 0)) - importe_buscado) < 1:
                    encontrados.append(r)
            except (ValueError, TypeError):
                pass

    if not encontrados:
        await msg.edit_text(f"❌ No se encontraron resultados para *{query}*.", parse_mode="Markdown")
        return

    # ───── 1 SOLO RESULTADO → FICHA DETALLADA ─────
    if len(encontrados) == 1:
        r = encontrados[0]
        tipo = "📱 ECHEQ" if r.get("tipo") == "ECHEQ" else "📄 FÍSICO"

        # Estado de depósito
        destinatario = r.get("destinatario", "").strip()
        if destinatario:
            estado_dep = f"✅ DEPOSITADO en {destinatario}"
        elif r.get("venc_dt") and r["venc_dt"] < hoy:
            estado_dep = "🔴 VENCIDO — sin depositar"
        else:
            estado_dep = "⚠️ PENDIENTE de depósito"

        # Días al vencimiento
        if r.get("venc_dt"):
            dias = (r["venc_dt"] - hoy).days
            if dias < 0:
                dias_txt = f"hace {abs(dias)} días"
            elif dias == 0:
                dias_txt = "HOY"
            elif dias == 1:
                dias_txt = "mañana"
            else:
                dias_txt = f"en {dias} días"
        else:
            dias_txt = "-"

        # Disponible desde
        disp_desde = r.get("fecha", "") or "-"
        if r.get("fecha_dt"):
            dias_disp = (hoy - r["fecha_dt"]).days
            if dias_disp >= 0:
                disp_marca = "🟢"
            else:
                disp_marca = "🟡"
            disp_desde = f"{disp_marca} {disp_desde}"

        cuit = r.get("cuit", "")
        cuit_limpio = re.sub(r"[-\s]", "", cuit)

        lineas = [f"🎯 *CHEQUE ENCONTRADO*\n"]
        lineas.append(f"📄 *Número:* `{r.get('numero', '-')}`")
        lineas.append(f"👤 *Librador:* {r.get('titular', '-')}")
        if cuit:
            lineas.append(f"🆔 *CUIT:* `{cuit}`")
        lineas.append(f"💰 *Importe:* ${r.get('importe', 0):,.0f}")
        lineas.append(f"📅 *Vencimiento:* {r.get('vencimiento', '-')} ({dias_txt})")
        lineas.append(f"🗓️ *Disponible desde:* {disp_desde}")
        lineas.append(f"🏷️ *Tipo:* {tipo}")
        if r.get("cliente"):
            lineas.append(f"🧑‍💼 *Cliente:* {r['cliente']}")
        if r.get("estado"):
            lineas.append(f"📊 *Estado planilla:* {r['estado']}")
        lineas.append(f"\n{estado_dep}")
        if cuit_limpio:
            lineas.append(f"\n👉 /evaluar {cuit_limpio}")

        await msg.edit_text("\n".join(lineas), parse_mode="Markdown")
        return

    # ───── MÚLTIPLES RESULTADOS → RESUMEN + LISTA ─────
    # Separar pendientes y depositados
    pendientes = [r for r in encontrados if not r.get("destinatario") and r.get("venc_dt") and r["venc_dt"] >= hoy]
    depositados = [r for r in encontrados if r.get("destinatario")]
    vencidos = [r for r in encontrados if not r.get("destinatario") and r.get("venc_dt") and r["venc_dt"] < hoy]

    total_pendiente = sum(r["importe"] for r in pendientes)
    total_depositado = sum(r["importe"] for r in depositados)

    lineas = [f"🔍 *BÚSQUEDA: {query}*\n"]
    lineas.append(f"📋 Total registros encontrados: {len(encontrados)}")
    lineas.append(f"⚠️ Pendientes sin depositar: {len(pendientes)} — *${total_pendiente:,.0f}*")
    lineas.append(f"✅ Depositados: {len(depositados)} — ${total_depositado:,.0f}")
    if vencidos:
        total_venc = sum(r["importe"] for r in vencidos)
        lineas.append(f"🔴 Vencidos sin depositar: {len(vencidos)} — ${total_venc:,.0f}")

    if pendientes:
        lineas.append(f"\n📋 *PENDIENTES:*")
        pendientes.sort(key=lambda x: x.get("venc_dt") or hoy)
        for r in pendientes[:10]:
            dias = (r["venc_dt"] - hoy).days if r.get("venc_dt") else 0
            numero = r.get("numero", "")
            cuit = r.get("cuit", "")
            tipo = "ECHEQ" if r.get("tipo") == "ECHEQ" else "FÍSICO"
            linea = f"• {r['titular'][:25]} | {tipo} | ${r['importe']:,.0f} | Vto: {r['vencimiento']} ({dias}d)"
            if numero:
                linea += f"\n  Nº `{numero}` → /buscar {numero}"
            lineas.append(linea)
            if cuit:
                lineas.append(f"  👉 /evaluar {cuit}")
        if len(pendientes) > 10:
            lineas.append(f"_...y {len(pendientes)-10} más_")

    lineas.append(f"\n💡 _Buscá un número específico para ver el detalle completo_")

    await msg.edit_text("\n".join(lineas), parse_mode="Markdown")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("evaluar", evaluar))
    app.add_handler(CommandHandler("cotizar", cotizar))
    app.add_handler(CommandHandler("nuevo", nuevo))
    app.add_handler(CommandHandler("vencer", vencer))
    app.add_handler(CommandHandler("cartera", cartera_cmd))
    app.add_handler(CommandHandler("cheques", cheques_cmd))
    app.add_handler(CommandHandler("semana", semana_cmd))
    app.add_handler(CommandHandler("manana", manana_cmd))
    app.add_handler(CommandHandler("evaluar_completo", evaluar_completo))
    app.add_handler(CommandHandler("analizar", analizar_cmd))
    app.add_handler(CommandHandler("buscar", buscar_cmd))
    app.add_handler(CommandHandler("dolar", dolar_cmd))

    # Alerta matutina automática a las 8:00hs (UTC-3 = 11:00 UTC)
    app.job_queue.run_daily(
        alerta_matutina,
        time=dtime(hour=11, minute=0, second=0),  # 8hs Argentina (UTC-3)
        days=(0, 1, 2, 3, 4, 5, 6)  # todos los días
    )
    app.add_handler(CommandHandler("hoy", cheques_hoy_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Endpoint HTTP /notify (Claude in Chrome → grupo de Telegram)
    start_notify_api(telegram_token=TELEGRAM_TOKEN)

    print("🚀 BAI Group Bot iniciado...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
