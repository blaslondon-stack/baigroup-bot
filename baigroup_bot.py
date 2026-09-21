import os
import re
import json
import httpx
import asyncio
import anthropic
from datetime import datetime, timedelta, time as dtime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Endpoint HTTP /notify (corre en thread separado)
from notify_api import start_notify_api

# TOKENS — leer SIEMPRE de env vars (nunca hardcodear)
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
INDICADORES_API_KEY = os.environ.get("INDICADORES_API_KEY", "")

# SEGURIDAD — solo responde en este grupo y a este usuario
GRUPO_PERMITIDO = -5265832156
USUARIOS_PERMITIDOS = {55179603, 1056894992}  # Agregar más IDs acá si sumás equipo

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
# INDICADORES.AR API — reemplaza BCRA
# Devuelve situación BCRA + cheques rechazados
# + score + apoc + repsal en una sola llamada
# ─────────────────────────────────────────────
async def consultar_indicadores(cuit: str) -> dict:
    """Consulta indicadores.ar y retorna el JSON completo.
    Formato de retorno:
      {"ok": True, "data": {...}}  o  {"ok": False, "error": "..."}
    """
    cuit_limpio = re.sub(r"[-\s]", "", cuit)
    url = f"https://indicadores.ar/v1/empresa?cuit={cuit_limpio}"
    headers = {"api-key": INDICADORES_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=15, verify=False, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
        if r.status_code == 200:
            return {"ok": True, "data": r.json()}
        elif r.status_code == 404:
            return {"ok": False, "error": "CUIT no encontrado en indicadores.ar"}
        elif r.status_code == 401:
            return {"ok": False, "error": "API key de indicadores.ar inválida"}
        else:
            return {"ok": False, "error": f"indicadores.ar HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": f"Error consultando indicadores.ar: {e}"}

# ─────────────────────────────────────────────
# CLAUDE ANALYSIS — usa estructura indicadores.ar
# ─────────────────────────────────────────────
async def analizar_con_claude(cuit: str, ind_data: dict) -> str:
    """Analiza los datos de indicadores.ar con Claude.
    ind_data es el JSON completo devuelto por indicadores.ar.
    """
    if not ANTHROPIC_API_KEY:
        return analizar_sin_claude(cuit, ind_data)

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        razon = ind_data.get("razon_social", "Desconocido")
        bcra = ind_data.get("deudas_bcra") or {}
        cheques_obj = ind_data.get("cheques_rechazados") or {}
        apoc = ind_data.get("apoc") or {}
        repsal = ind_data.get("sanciones_laborales_repsal") or {}

        # Flags automáticos
        flags = []
        sit_principal = bcra.get("situacion_principal", 0)
        if sit_principal >= 4:
            flags.append(f"🔴 Situación {sit_principal} — {bcra.get('situacion_principal_label', '')}")
        elif sit_principal == 3:
            flags.append(f"🟠 Situación 3 — Con problemas graves")
        elif sit_principal == 2:
            flags.append(f"🟡 Situación 2 — Con seguimiento")

        for ent in bcra.get("entidades_detalle", []):
            if ent.get("en_juicio"):
                flags.append(f"🔴 {ent['entidad']}: en juicio")
            if ent.get("refinanciado"):
                flags.append(f"⚠️ {ent['entidad']}: refinanciado")
            if ent.get("dias_atraso", 0) > 0:
                flags.append(f"⚠️ {ent['entidad']}: {ent['dias_atraso']}d de atraso")

        impagos = cheques_obj.get("impagos", 0)
        total_cheques = cheques_obj.get("total", 0)
        if impagos > 0:
            flags.append(f"🔴 {impagos} cheques rechazados IMPAGAS (de {total_cheques} total)")
        elif total_cheques > 0:
            flags.append(f"🟡 {total_cheques} cheques rechazados — todos pagados")

        if apoc.get("incluido"):
            flags.append("🔴 APOC: incluido en padrón AFIP de facturas apócrifas")
        if repsal.get("total", 0) > 0:
            flags.append(f"⚠️ REPSAL: {repsal['total']} sanciones laborales")

        flags_txt = "\n".join(flags) if flags else "Ninguno"

        # Resumen cheques para el prompt
        cheques_detalle = cheques_obj.get("detalle", [])[:8]

        prompt = f"""Sos el agente de crédito de BAI Group SA, financiera argentina especializada en descuento de cheques (ECHEQs y físicos).

Analizá la siguiente información de indicadores.ar para el CUIT {cuit} ({razon}).

SITUACIÓN BCRA (período {bcra.get('periodo', '-')}):
- Situación principal: {sit_principal} — {bcra.get('situacion_principal_label', '-')}
- Deuda total: ${bcra.get('deuda_total_miles', 0):,} miles
- Entidades: {bcra.get('entidades', 0)}
{json.dumps(bcra.get('entidades_detalle', []), ensure_ascii=False, indent=2)}

CHEQUES RECHAZADOS:
- Total: {total_cheques} | Impagas: {impagos}
{json.dumps(cheques_detalle, ensure_ascii=False, indent=2)}

APOC (facturas apócrifas AFIP): {"SÍ incluido 🔴" if apoc.get("incluido") else "No incluido ✅"}
REPSAL (sanciones laborales): {repsal.get("total", 0)} registros

FLAGS DETECTADOS AUTOMÁTICAMENTE:
{flags_txt}

SEMÁFORO BAI GROUP:
✅ APROBAR: Sit 1, sin cheques impagas, sin APOC, sin juicios
🟡 CON CONDICIONES: Sit 1-2 con alertas menores o cheques pagados
🟠 ALTO RIESGO: Sit 2 con flags, cheques impagas recientes, o APOC
❌ RECHAZAR: Sit 3+ / en juicio / cheques SIN FONDOS impagas recientes / APOC activo

CRITERIOS:
- Montos siempre en millones: $27,63M (no en miles)
- Deuda >$500M = exposición alta, exigir mayor tasa
- 3+ entidades simultáneas = analizar concentración
- Cheques impagas recientes (<6 meses) = rechazo casi automático

Respondé en este formato exacto:

🏢 *LIBRADOR:* {razon}
🔢 *CUIT:* {cuit}

📊 *SITUACIÓN BCRA ({bcra.get('periodo', '-')}):*
[Cada entidad: nombre | Sit X | $montoM | flags]

🚨 *CHEQUES RECHAZADOS:*
[Cantidad total, impagas, últimos rechazos con fecha/monto/causal]
[Si no hay: "Sin cheques rechazados ✅"]

⚠️ *FLAGS DETECTADOS:*
[Lista concreta o "Sin flags críticos ✅"]

💰 *EXPOSICIÓN TOTAL:*
[$totalM en X entidades]

🎯 *RECOMENDACIÓN:*
[✅ APROBAR / 🟡 CON CONDICIONES / 🟠 ALTO RIESGO / ❌ RECHAZAR]
[Justificación en 2-3 líneas concretas]

📋 *CONDICIONES:*
[Tasa sugerida, monto máximo o "Sin condiciones adicionales"]"""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        return analizar_sin_claude(cuit, ind_data)


def analizar_sin_claude(cuit: str, ind_data: dict) -> str:
    """Análisis básico sin Claude como fallback — usa estructura indicadores.ar"""
    try:
        razon = ind_data.get("razon_social", "Desconocido")
        bcra = ind_data.get("deudas_bcra") or {}
        cheques_obj = ind_data.get("cheques_rechazados") or {}

        sit = bcra.get("situacion_principal", 0)
        sit_label = bcra.get("situacion_principal_label", "-")
        deuda = bcra.get("deuda_total_miles", 0)
        periodo = bcra.get("periodo", "-")

        emoji_sit = {1: "🟢", 2: "🟡", 3: "🟠", 4: "🔴", 5: "🔴"}.get(sit, "⚪")

        entidades_lineas = []
        for ent in bcra.get("entidades_detalle", []):
            e_sit = ent.get("situacion", 1)
            e_emoji = {1: "🟢", 2: "🟡", 3: "🟠", 4: "🔴", 5: "🔴"}.get(e_sit, "⚪")
            e_deuda = ent.get("deuda_miles", 0)
            e_fmt = f"${e_deuda/1000:,.2f}M" if e_deuda >= 1000 else f"${e_deuda:,}k"
            flags = []
            if ent.get("en_juicio"): flags.append("en juicio")
            if ent.get("refinanciado"): flags.append("refinanciado")
            flag_txt = f" ⚠️ {', '.join(flags)}" if flags else ""
            entidades_lineas.append(f"  {e_emoji} {ent['entidad']}: Sit {e_sit} | {e_fmt}{flag_txt}")

        # Cheques
        total_ch = cheques_obj.get("total", 0)
        impagos = cheques_obj.get("impagos", 0)
        if total_ch > 0:
            cheques_txt = f"🚨 {total_ch} rechazados — {impagos} impagas"
            for ch in cheques_obj.get("detalle", [])[:4]:
                monto = float(ch.get("monto", 0))
                monto_fmt = f"${monto/1_000_000:,.2f}M" if monto >= 1_000_000 else f"${monto/1000:,.0f}k"
                pagado = "✅" if ch.get("pagado") else "❌"
                cheques_txt += f"\n  {pagado} {ch['fecha']} {monto_fmt} — {ch['causal']}"
        else:
            cheques_txt = "Sin cheques rechazados ✅"

        # Semáforo
        if impagos > 0 or sit >= 4:
            semaforo = "❌ RECHAZAR"
        elif sit == 3 or (impagos == 0 and total_ch > 0):
            semaforo = "🟠 ALTO RIESGO"
        elif sit == 2:
            semaforo = "🟡 APROBAR CON CONDICIONES"
        else:
            semaforo = "✅ APROBAR"

        deuda_fmt = f"${deuda/1000:,.2f}M" if deuda >= 1000 else f"${deuda:,}k"
        ents_txt = "\n".join(entidades_lineas) if entidades_lineas else "  Sin deuda reportada"

        return (
            f"🏢 *LIBRADOR:* {razon}\n"
            f"🔢 *CUIT:* {cuit}\n\n"
            f"📊 *SITUACIÓN BCRA ({periodo}):*\n"
            f"{emoji_sit} Sit {sit} — {sit_label}\n"
            f"{ents_txt}\n\n"
            f"🚨 *CHEQUES RECHAZADOS:*\n{cheques_txt}\n\n"
            f"💰 *EXPOSICIÓN:* {deuda_fmt} en {bcra.get('entidades', 0)} entidades\n\n"
            f"🎯 *RECOMENDACIÓN:* {semaforo}"
        )
    except Exception:
        return f"✅ Consulta exitosa para CUIT {cuit}\nRevisá los datos manualmente."

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
    msg = await update.message.reply_text(
        f"🔍 Consultando indicadores.ar para CUIT `{cuit}`...",
        parse_mode="Markdown"
    )

    # Consultar indicadores.ar y cartera simultáneamente
    ind_result, registros = await asyncio.gather(
        consultar_indicadores(cuit),
        leer_cheques_sheet()
    )

    if not ind_result["ok"]:
        await msg.edit_text(
            f"⚠️ {ind_result['error']}\n\nEl CUIT puede no estar registrado o sin actividad.",
            parse_mode="Markdown"
        )
        return

    await msg.edit_text("🤖 Analizando con IA...", parse_mode="Markdown")

    ind_data = ind_result["data"]
    analisis = await analizar_con_claude(cuit, ind_data)

    # Verificar en cartera del Sheet
    hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    en_cartera = [
        r for r in registros
        if r.get("cuit", "").strip() == cuit
        and r.get("fecha_dt") and r["fecha_dt"] >= hoy
        and not r.get("cerrado")
    ]

    if en_cartera:
        total_cartera_cuit = sum(r["importe"] for r in en_cartera)
        total_cartera_general = sum(
            r["importe"] for r in registros
            if r.get("fecha_dt") and r["fecha_dt"] >= hoy
            and not r.get("cerrado")
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
    """Lee el Google Sheet de cheques y retorna lista de registros.

    Usa cache-buster + headers anti-cache para evitar que Google sirva
    una versión cacheada del CSV (problema conocido del endpoint /export).
    """
    # Cache-buster con timestamp en milisegundos
    cache_buster = int(datetime.now().timestamp() * 1000)
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export"
        f"?format=csv&gid={SHEET_GID}&t={cache_buster}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    try:
        async with httpx.AsyncClient(timeout=30, verify=False, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
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

            # ─── Nueva estructura del Sheet ───
            # A(0)=CHEQUE B(1)=FECHA C(2)=FECHA ENTREGO D(3)=NUMERO E(4)=TITULAR
            # F(5)=CUIT G(6)=IMPORTE H(7)=CLIENTE I(8)=DESTINATARIO
            # J(9)=ESTADO CHEQUE K(10)=DIAS EN CALLE L(11)=TENENCIA M(12)=DIA ANOTADO N(13)=BANCO
            estado_cheque = cols[9].strip().upper() if len(cols) > 9 else ""
            dias_en_calle = cols[10].strip() if len(cols) > 10 else ""
            tenencia = cols[11].strip() if len(cols) > 11 else ""
            dia_anotado = cols[12].strip() if len(cols) > 12 else ""
            banco = cols[13].strip() if len(cols) > 13 else ""

            # Un cheque se considera CERRADO SOLO si tiene destinatario (col I).
            # Cuando col I tiene valor = ya lo depositamos.
            cerrado = bool(cols[8].strip())

            # "En la calle" = col J contiene "CALLE" en cualquier forma
            # ("CALLE", "EN LA CALLE", "EN CALLE") o dice OJO
            en_la_calle = (
                "CALLE" in estado_cheque
                or estado_cheque == "OJO"
                or dias_en_calle.upper() == "OJO"
            )
            # Alerta OJO: la fórmula de col K lo marca cuando pasa >1 día en la calle
            alerta_ojo = estado_cheque == "OJO" or dias_en_calle.upper() == "OJO"

            registros.append({
                "tipo": cols[0].strip(),           # ECHEQ / vacío
                "fecha": cols[1].strip(),           # fecha de liberación (col B)
                "fecha_dt": parse_fecha(cols[1]),
                "fecha_entrego": cols[2].strip() if len(cols) > 2 else "",
                "numero": cols[3].strip(),
                "titular": titular,
                "cuit": cols[5].strip(),
                "importe": parse_importe(importe_str),
                "cliente": cols[7].strip(),
                "destinatario": cols[8].strip(),    # vacío = pendiente en col I
                "estado_cheque": estado_cheque,     # col J
                "dias_en_calle": dias_en_calle,     # col K (número o "OJO")
                "tenencia": tenencia,               # col L (cuenta donde está)
                "dia_anotado": dia_anotado,         # col M (fecha de carga)
                "banco": banco,                     # col N
                # Derivados de conveniencia
                "cerrado": cerrado,
                "en_la_calle": en_la_calle,
                "alerta_ojo": alerta_ojo,
                "rechazado": estado_cheque == "RECHAZADO",
                # Compatibilidad con código viejo
                "vencimiento": "",
                "venc_dt": None,
                "estado": estado_cheque,
                "rechazos": 1 if estado_cheque == "RECHAZADO" else 0,
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

    pendientes = [r for r in registros if not r.get("cerrado") and r["fecha_dt"] and r["fecha_dt"] >= hoy]

    if not pendientes:
        await msg.edit_text("✅ No hay cheques pendientes sin destinatario.", parse_mode="Markdown")
        return

    pendientes.sort(key=lambda x: x["fecha_dt"])

    disponibles_hoy = [p for p in pendientes if p["fecha_dt"] and p["fecha_dt"] <= hoy]
    no_disponibles = [p for p in pendientes if not p["fecha_dt"] or p["fecha_dt"] > hoy]

    total = sum(p["importe"] for p in pendientes)
    total_disp = sum(p["importe"] for p in disponibles_hoy)

    urgentes = [p for p in disponibles_hoy if p["fecha_dt"] and (p["fecha_dt"] - hoy).days <= 7]
    esta_semana = [p for p in disponibles_hoy if p["fecha_dt"] and 7 < (p["fecha_dt"] - hoy).days <= 30]
    mas_adelante = [p for p in disponibles_hoy if p["fecha_dt"] and (p["fecha_dt"] - hoy).days > 30]

    lineas = ["📋 *CHEQUES PENDIENTES — SIN DESTINATARIO*\n"]
    lineas.append(f"💰 Total: {len(pendientes)} cheques | *${total:,.0f}*")
    lineas.append(f"✅ Disponibles para depositar: {len(disponibles_hoy)} | *${total_disp:,.0f}*")
    lineas.append(f"⏳ No disponibles aún: {len(no_disponibles)}\n")

    if urgentes:
        lineas.append(f"🔴 *URGENTE — vencen en 7 días ({len(urgentes)} cheques)*")
        for p in urgentes[:10]:
            dias = (p["fecha_dt"] - hoy).days
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
            dias = (p["fecha_dt"] - hoy).days
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
    """Muestra cheques cuya fecha de liberación es HOY o anterior, todavía sin acreditar.
    Incluye los que ya están 'en la calle' y los que tienen alerta OJO."""
    msg = await update.message.reply_text("Consultando cheques disponibles hoy...", parse_mode="Markdown")
    registros = await leer_cheques_sheet()
    if not registros:
        await msg.edit_text("No se pudo leer la planilla.", parse_mode="Markdown")
        return

    hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Cheques cuya fecha de liberación es <= hoy y todavía no acreditados/rechazados
    # (no filtramos por "cerrado" porque queremos incluir los EN LA CALLE)
    estados_finales = ("OK", "ACREDITADO", "RECHAZADO")
    disponibles = [
        r for r in registros
        if r.get("fecha_dt") and r["fecha_dt"] <= hoy
        and not r.get("destinatario")
        and r.get("estado_cheque", "") not in estados_finales
    ]

    if not disponibles:
        await msg.edit_text("No hay cheques disponibles para depositar hoy.", parse_mode="Markdown")
        return

    # Separar en 3 grupos según estado
    en_ojo = [r for r in disponibles if r.get("alerta_ojo")]
    en_calle = [r for r in disponibles if r.get("en_la_calle") and not r.get("alerta_ojo")]
    en_oficina = [r for r in disponibles if not r.get("en_la_calle")]

    def linea_cheque(p):
        dias_atraso = (hoy - p["fecha_dt"]).days
        tipo = "ECHEQ" if p["tipo"] == "ECHEQ" else "FÍSICO"
        titular = p["titular"][:28]
        numero = p["numero"]
        imp = p["importe"]
        cli = p["cliente"]
        fecha = p["fecha"]
        atraso_txt = f"(hoy)" if dias_atraso == 0 else f"({dias_atraso}d atrás)"
        base = (f"*{titular}* | {tipo} Nº {numero} | "
                f"${imp:,.0f} | {cli} | Se liberó: {fecha} {atraso_txt}")
        return base

    total = sum(p["importe"] for p in disponibles)
    lineas = [f"*CHEQUES DISPONIBLES — {hoy.strftime('%d/%m/%Y')}*"]
    lineas.append(f"Total: {len(disponibles)} cheques | *${total:,.0f}*\n")

    if en_oficina:
        subtotal = sum(p["importe"] for p in en_oficina)
        lineas.append(f"🏢 *EN LA OFICINA — {len(en_oficina)} cheques — ${subtotal:,.0f}*")
        lineas.append("_(pendientes de mandar a depositar)_")
        for p in en_oficina:
            lineas.append(f"• {linea_cheque(p)}")
        lineas.append("")

    if en_calle:
        subtotal = sum(p["importe"] for p in en_calle)
        lineas.append(f"📤 *EN LA CALLE — {len(en_calle)} cheques — ${subtotal:,.0f}*")
        lineas.append("_(el cliente los dejó, aún no llegaron a la oficina)_")
        for p in en_calle:
            lineas.append(f"• {linea_cheque(p)}")
        lineas.append("")

    if en_ojo:
        subtotal = sum(p["importe"] for p in en_ojo)
        lineas.append(f"🚨 *OJO — {len(en_ojo)} cheques — ${subtotal:,.0f}*")
        lineas.append("_(más de 1 día en la calle sin llegar, revisar)_")
        for p in en_ojo:
            dias_calle = p.get("dias_en_calle", "")
            linea = f"• {linea_cheque(p)}"
            if dias_calle.isdigit():
                linea += f"\n   ⏱️ {dias_calle}d en calle"
            lineas.append(linea)

    texto = "\n".join(lineas)
    # Truncar si supera límite de Telegram
    if len(texto) > 3800:
        texto = texto[:3800] + "\n\n_...lista truncada_"

    try:
        await msg.edit_text(texto, parse_mode="Markdown")
    except Exception:
        # Fallback sin markdown si algo rompe el parseo
        plano = texto.replace("*", "").replace("_", "").replace("`", "")
        await msg.edit_text(plano)



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

    # Cheques SIN destinatario cuya fecha de liberación cae:
    #  - En los próximos 7 días (todavía no llegaron a fecha), o
    #  - Ya está liberado pero aún no acreditado (atrasado / en la calle / OJO)
    proximos = [
        r for r in registros
        if not r.get("cerrado")
        and r["fecha_dt"]
        and r["fecha_dt"] <= limite  # hasta 7 días adelante (los atrasados también entran)
    ]

    if not proximos:
        await msg.edit_text("✅ No hay cheques venciendo en los próximos 7 días.", parse_mode="Markdown")
        return

    proximos.sort(key=lambda x: x["fecha_dt"])

    # Separar por tipo
    echeq = [p for p in proximos if p["tipo"] == "ECHEQ"]
    fisicos = [p for p in proximos if p["tipo"] != "ECHEQ"]

    # Separar por estado (depositado vs pendiente)
    def formato_cheque(p):
        cuit = p["cuit"]
        titular = p["titular"][:28]
        dias_venc = (p["fecha_dt"] - hoy).days if p.get("fecha_dt") else 0
        lineas = [f"⚠️ *{titular}* | ${p['importe']:,.0f}"]
        fecha_lib = p['fecha']
        cliente_short = p['cliente'][:20]
        lineas.append(f"   🟢 Disponible: {fecha_lib} | Se libera: {fecha_lib} ({dias_venc}d) | {cliente_short}")
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
        lineas.append(f"_(acreditación automática al liberarse)_\n")
        for p in echeq:
            lineas.append(formato_cheque(p))
            lineas.append("")

    if fisicos:
        lineas.append(f"📄 *FÍSICOS — {len(fisicos)} cheques — ${total_fisicos:,.0f}*")
        lineas.append(f"_(requieren depósito manual en banco)_\n")
        for p in fisicos:
            lineas.append(formato_cheque(p))
            lineas.append("")

    # Dividir en mensajes si es muy largo (por líneas completas, no por caracteres)
    texto = "\n".join(lineas)
    LIMITE = 3800  # margen seguro bajo el límite de Telegram (4096)

    async def enviar_seguro(texto_a_enviar, es_primero=False):
        """Envía con Markdown; si Telegram lo rechaza, reintenta sin formato."""
        try:
            if es_primero:
                await msg.edit_text(texto_a_enviar, parse_mode="Markdown")
            else:
                await update.message.reply_text(texto_a_enviar, parse_mode="Markdown")
        except Exception:
            # Markdown roto → mandar en texto plano
            plano = texto_a_enviar.replace("*", "").replace("_", "").replace("`", "")
            if es_primero:
                await msg.edit_text(plano)
            else:
                await update.message.reply_text(plano)

    if len(texto) <= LIMITE:
        await enviar_seguro(texto, es_primero=True)
    else:
        # Partir por líneas, respetando estructura
        chunks = []
        actual = []
        largo_actual = 0
        for linea in lineas:
            largo_linea = len(linea) + 1  # +1 por el \n
            if largo_actual + largo_linea > LIMITE and actual:
                chunks.append("\n".join(actual))
                actual = [linea]
                largo_actual = largo_linea
            else:
                actual.append(linea)
                largo_actual += largo_linea
        if actual:
            chunks.append("\n".join(actual))

        for i, chunk in enumerate(chunks):
            await enviar_seguro(chunk, es_primero=(i == 0))


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
        if not r.get("cerrado")
        and r["fecha_dt"] and r["fecha_dt"].date() == manana.date()
        and r["fecha_dt"] and r["fecha_dt"] >= hoy
    ]

    if not disponibles:
        await msg.edit_text(f"✅ No hay cheques que se habiliten mañana ({manana.strftime('%d/%m/%Y')}).", parse_mode="Markdown")
        return

    disponibles.sort(key=lambda x: x["fecha_dt"])
    total = sum(p["importe"] for p in disponibles)

    echeq = [p for p in disponibles if p["tipo"] == "ECHEQ"]
    fisicos = [p for p in disponibles if p["tipo"] != "ECHEQ"]

    lineas = [f"📅 *MAÑANA {manana.strftime('%d/%m/%Y')} — {len(disponibles)} cheques*\n"]
    lineas.append(f"💰 Total: *${total:,.0f}*\n")

    if echeq:
        lineas.append(f"💻 *ECHEQ — {len(echeq)} cheques — ${sum(p['importe'] for p in echeq):,.0f}*")
        for p in echeq:
            dias_venc = (p["fecha_dt"] - hoy).days
            cuit = p["cuit"]
            fecha_p = p['fecha']
            lineas.append(f"• *{p['titular'][:28]}* | ${p['importe']:,.0f} | Se libera: {fecha_p} ({dias_venc}d) | {p['cliente']}")
            if cuit:
                lineas.append(f"  👉 /evaluar {cuit}")

    if fisicos:
        lineas.append(f"\n📄 *FÍSICOS — {len(fisicos)} cheques — ${sum(p['importe'] for p in fisicos):,.0f}*")
        for p in fisicos:
            dias_venc = (p["fecha_dt"] - hoy).days
            cuit = p["cuit"]
            fecha_p = p['fecha']
            lineas.append(f"• *{p['titular'][:28]}* | ${p['importe']:,.0f} | Se libera: {fecha_p} ({dias_venc}d) | {p['cliente']}")
            if cuit:
                lineas.append(f"  👉 /evaluar {cuit}")

    await msg.edit_text("\n".join(lineas), parse_mode="Markdown")

# ─────────────────────────────────────────────
# ALERTA MATUTINA AUTOMÁTICA
# ─────────────────────────────────────────────
async def alerta_matutina(context, target_chat_id=None):
    """Se ejecuta automáticamente cada mañana a las 8hs.

    Si se invoca con target_chat_id, envía a ese chat (útil para /buenos_dias
    desde privado). Si no, envía al grupo de operaciones por defecto.
    """
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
            if not r.get("cerrado")
            and r["fecha_dt"] and r["fecha_dt"].date() == hoy.date()
            and r["fecha_dt"] and r["fecha_dt"] >= hoy
        ]

        # Habilitados mañana
        manana_list = [
            r for r in registros
            if not r.get("cerrado")
            and r["fecha_dt"] and r["fecha_dt"].date() == manana.date()
            and r["fecha_dt"] and r["fecha_dt"] >= hoy
        ]

        # Vencen esta semana sin destinatario
        vencen_semana = [
            r for r in registros
            if not r.get("cerrado")
            and r["fecha_dt"]
            and hoy <= r["fecha_dt"] <= semana
        ]

        # Total cartera pendiente
        total_cartera = sum(
            r["importe"] for r in registros
            if not r.get("cerrado")
            and r["fecha_dt"] and r["fecha_dt"] >= hoy
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
            urgentes = [p for p in vencen_semana if (p["fecha_dt"] - hoy).days <= 2]
            lineas.append(f"\n⚠️ *Vencen esta semana sin depositar:* {len(vencen_semana)} — ${total_vencen:,.0f}")
            if urgentes:
                lineas.append(f"🔴 *URGENTE ({len(urgentes)} vencen en 2 días):*")
                for p in urgentes:
                    dias = (p["fecha_dt"] - hoy).days
                    lineas.append(f"   • {p['titular'][:25]} | ${p['importe']:,.0f} | Vence en {dias}d")

        lineas.append(f"\n💰 *Cartera total pendiente:* ${total_cartera:,.0f}")

        # Desglose por mes (según fecha de pago / col B)
        from collections import defaultdict
        meses_es = ["Enero","Febrero","Marzo","Abril","Mayo","Junio",
                    "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
        por_mes = defaultdict(float)
        for r in registros:
            if r.get("cerrado"):
                continue
            f_dt = r.get("fecha_dt")
            if not f_dt or f_dt < hoy:
                continue
            clave = (f_dt.year, f_dt.month)
            por_mes[clave] += r["importe"]
        if por_mes:
            lineas.append("")  # línea en blanco
            for (anio, mes) in sorted(por_mes.keys()):
                nombre_mes = meses_es[mes-1]
                # Si es el año actual no lo muestro; si no, sí
                etiqueta = nombre_mes if anio == hoy.year else f"{nombre_mes} {anio}"
                lineas.append(f"   • {etiqueta}: ${por_mes[(anio, mes)]:,.0f}")

        lineas.append("\n_Usá /hoy, /semana o /cheques para más detalle._")

        await context.bot.send_message(
            chat_id=target_chat_id if target_chat_id else GRUPO_PERMITIDO,
            text="\n".join(lineas),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error en alerta matutina: {e}")


# ─────────────────────────────────────────────
# ALERTA CHEQUES EN LA CALLE — 11:00 AR
# ─────────────────────────────────────────────
async def alerta_cheques_calle(context, target_chat_id=None):
    """Cheques con estado OJO (más de 1 día en la calle sin novedades).
    Se ejecuta automáticamente todos los días a las 11:00 hora Argentina.
    """
    try:
        registros = await leer_cheques_sheet()
        if not registros:
            return

        # Cheques con alerta OJO (col K con fórmula o col J)
        en_ojo = [r for r in registros if r.get("alerta_ojo") and not r.get("rechazado")]

        if not en_ojo:
            # Si es invocación manual desde comando, avisar; si es automática, callar
            if target_chat_id:
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    text="✅ No hay cheques con alerta OJO en este momento.",
                )
            return

        # Ordenar por cliente y luego por importe descendente
        en_ojo.sort(key=lambda x: (x.get("cliente", ""), -x.get("importe", 0)))

        total = sum(r["importe"] for r in en_ojo)

        lineas = [f"🚨 *CHEQUES EN LA CALLE — REVISAR*"]
        lineas.append(f"_Más de 1 día sin llegar a la oficina_\n")
        lineas.append(f"📊 {len(en_ojo)} cheques — *${total:,.0f}*\n")

        for r in en_ojo:
            dias = r.get("dias_en_calle", "")
            if dias.isdigit():
                dias_txt = f"en la calle hace {dias} días"
            else:
                dias_txt = "en la calle hace más de 1 día"

            titular = r.get("titular", "-")[:30]
            importe = r.get("importe", 0)
            numero = r.get("numero", "")
            cliente = r.get("cliente", "")

            lineas.append(f"📄 *{titular}* | ${importe:,.0f}")
            detalles = []
            if numero:
                detalles.append(f"Nº `{numero}`")
            if cliente:
                detalles.append(f"🧑‍💼 {cliente}")
            if detalles:
                lineas.append("   " + " | ".join(detalles))
            lineas.append(f"   ⏱️ {dias_txt}")
            lineas.append("")

        texto = "\n".join(lineas)
        # Cortar si es muy largo
        if len(texto) > 3800:
            texto = texto[:3800] + "\n\n_...lista truncada_"

        await context.bot.send_message(
            chat_id=target_chat_id if target_chat_id else GRUPO_PERMITIDO,
            text=texto,
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error en alerta cheques calle: {e}")


async def evaluar_completo(update, context):
    """/evaluar_completo — alias de /evaluar (indicadores.ar ya incluye todo)"""
    await evaluar(update, context)


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
        elif r.get("fecha_dt") and r["fecha_dt"] < hoy:
            estado_dep = "🔴 VENCIDO — sin depositar"
        else:
            estado_dep = "⚠️ PENDIENTE de depósito"

        # Días a la fecha de depósito
        if r.get("fecha_dt"):
            dias = (r["fecha_dt"] - hoy).days
            if dias < 0:
                dias_txt = f"habilitado hace {abs(dias)}d"
            elif dias == 0:
                dias_txt = "HOY"
            elif dias == 1:
                dias_txt = "mañana"
            else:
                dias_txt = f"en {dias}d"
        else:
            dias_txt = "-"

        # Marca visual de habilitación
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
        lineas.append(f"💳 *Fecha de depósito:* {disp_desde} ({dias_txt})")
        lineas.append(f"🏷️ *Tipo:* {tipo}")
        if r.get("cliente"):
            lineas.append(f"🧑‍💼 *Cliente:* {r['cliente']}")
        rechazos = r.get("rechazos", 0)
        if rechazos > 0:
            lineas.append(f"🚨 *Rechazos del librador:* {rechazos}")
        if r.get("estado"):
            lineas.append(f"📊 *Estado planilla:* {r['estado']}")
        lineas.append(f"\n{estado_dep}")
        if cuit_limpio:
            lineas.append(f"\n👉 /evaluar {cuit_limpio}")

        await msg.edit_text("\n".join(lineas), parse_mode="Markdown")
        return

    # ───── MÚLTIPLES RESULTADOS → RESUMEN + LISTA ─────
    # Separar pendientes y depositados
    pendientes = [r for r in encontrados if not r.get("cerrado") and r.get("fecha_dt") and r["fecha_dt"] >= hoy]
    depositados = [r for r in encontrados if r.get("cerrado")]
    vencidos = [r for r in encontrados if not r.get("cerrado") and r.get("fecha_dt") and r["fecha_dt"] < hoy]

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
        lineas.append(f"\n📋 *PENDIENTES — ordenados por fecha de pago:*")
        # Ordenar por fecha de pago (col B), no por vencimiento
        pendientes.sort(key=lambda x: x.get("fecha_dt") or hoy)
        for r in pendientes[:30]:
            fecha_pago = r.get("fecha", "") or "-"
            dias_pago = (r["fecha_dt"] - hoy).days if r.get("fecha_dt") else None
            if dias_pago is None:
                dias_txt = ""
            elif dias_pago < 0:
                dias_txt = f"(habilitado, hace {abs(dias_pago)}d)"
            elif dias_pago == 0:
                dias_txt = "(HOY)"
            elif dias_pago == 1:
                dias_txt = "(mañana)"
            else:
                dias_txt = f"(en {dias_pago}d)"
            numero = r.get("numero", "")
            tipo = "ECHEQ" if r.get("tipo") == "ECHEQ" else "FÍSICO"
            rechazos = r.get("rechazos", 0)
            flag_rechazo = f" 🚨 x{rechazos}" if rechazos > 0 else ""
            linea = f"• {fecha_pago} {dias_txt} | ${r['importe']:,.0f} | {tipo}{flag_rechazo}"
            linea += f"\n  {r['titular'][:30]}"
            if numero:
                linea += f" — Nº `{numero}`"
            lineas.append(linea)
        if len(pendientes) > 30:
            lineas.append(f"_...y {len(pendientes)-30} más_")

    lineas.append(f"\n💡 _Buscá un número específico para ver el detalle completo_")

    await msg.edit_text("\n".join(lineas), parse_mode="Markdown")

# ─────────────────────────────────────────────
# COMANDO /buenos_dias — Ejecuta la alerta matutina on-demand
# ─────────────────────────────────────────────
async def buenos_dias_cmd(update, context):
    """Dispara manualmente el mensaje de alerta matutina (para testing).
    Responde en el mismo chat desde donde se invocó (grupo o privado)."""
    if not await check_acceso(update): return
    await update.message.reply_text("☀️ Generando reporte matutino...")
    await alerta_matutina(context, target_chat_id=update.effective_chat.id)


async def en_la_calle_cmd(update, context):
    """Dispara manualmente la alerta de cheques en la calle con OJO."""
    if not await check_acceso(update): return
    await update.message.reply_text("🚨 Buscando cheques en la calle...")
    await alerta_cheques_calle(context, target_chat_id=update.effective_chat.id)

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
    app.add_handler(CommandHandler("buenos_dias", buenos_dias_cmd))

    # Alerta matutina automática a las 8:00hs (UTC-3 = 11:00 UTC)
    app.job_queue.run_daily(
        alerta_matutina,
        time=dtime(hour=11, minute=0, second=0),  # 8hs Argentina (UTC-3)
        days=(0, 1, 2, 3, 4, 5, 6)  # todos los días
    )
    # Alerta cheques en la calle (OJO) — 11hs Argentina (UTC-3 = 14:00 UTC)
    app.job_queue.run_daily(
        alerta_cheques_calle,
        time=dtime(hour=14, minute=0, second=0),  # 11hs Argentina (UTC-3)
        days=(0, 1, 2, 3, 4, 5, 6)  # todos los días
    )
    app.add_handler(CommandHandler("en_la_calle", en_la_calle_cmd))
    app.add_handler(CommandHandler("hoy", cheques_hoy_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Endpoint HTTP /notify (Claude in Chrome → grupo de Telegram)
    start_notify_api(telegram_token=TELEGRAM_TOKEN)

    print("🚀 BAI Group Bot iniciado...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
