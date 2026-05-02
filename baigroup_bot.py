import os
import re
import json
import httpx
import asyncio
import anthropic
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# TOKENS
TELEGRAM_TOKEN = "8764473072:AAG1v2uRuyNFaxxW9gl_xddwZNS2cx4Bvwc"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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
    }
    try:
        async with httpx.AsyncClient(timeout=20, verify=False, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                return {"ok": True, "data": r.json()}
            elif r.status_code == 404:
                return {"ok": False, "error": "CUIT no encontrado en Central de Deudores — puede no tener deuda registrada"}
            else:
                return {"ok": False, "error": f"Error BCRA HTTP {r.status_code}"}
    except httpx.SSLError:
        # Fallback: intentar sin verificacion SSL estricta
        try:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            async with httpx.AsyncClient(timeout=20, verify=False) as client:
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    return {"ok": True, "data": r.json()}
                else:
                    return {"ok": False, "error": f"Error BCRA: {r.status_code}"}
        except Exception as e2:
            return {"ok": False, "error": f"SSL Error: {str(e2)}"}
    except Exception as e:
        return {"ok": False, "error": f"Conexión fallida: {str(e)}"}

async def consultar_cheques_rechazados(cuit: str) -> dict:
    cuit_limpio = re.sub(r"[-\s]", "", cuit)
    url = f"https://api.bcra.gob.ar/centraldedeudores/v1.0/cheques/{cuit_limpio}"
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
        
        prompt = f"""Sos el agente de crédito de BAI Group SA, una financiera argentina especializada en descuento de cheques.

Analizá la siguiente información del BCRA para el CUIT {cuit} y generá un informe de evaluación crediticia.

DATOS BCRA:
{json.dumps(bcra_data, ensure_ascii=False, indent=2)}

CHEQUES RECHAZADOS ({len(cheques_data.get("cheques", []))} registros):
{json.dumps(cheques_data.get("cheques", [])[:5], ensure_ascii=False, indent=2)}

POLÍTICA CREDITICIA BAI GROUP:
- Solo aceptamos libradores en Situación 1 o máximo Situación 2 (con justificación)
- Situación 3 o superior: rechazo automático
- Cheques rechazados en los últimos 6 meses: rechazo automático
- Plazo máximo de cheques: 180 días
- Vigilar saltos bruscos de deuda (posible refinanciación)

Respondé en este formato exacto con emojis:

🏢 *LIBRADOR:* [nombre]
🔢 *CUIT:* {cuit}

📊 *SITUACIÓN ACTUAL:*
[Lista cada entidad con situación y monto]

🚨 *ALERTAS:*
[Lista alertas detectadas o "Sin alertas"]

📋 *HISTORIAL:*
[Resumen breve del comportamiento en 24 meses]

🎯 *RECOMENDACIÓN:*
[✅ APROBAR / 🟡 APROBAR CON CONDICIONES / ❌ RECHAZAR]
[Breve justificación en 1-2 líneas]

⚠️ *CONDICIONES:*
[Si aplica, qué condiciones poner]"""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
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
                entidades_lineas.append(f"• {nombre_entidad}: Sit. {sit} | ${monto:,}k")
        
        # Cheques rechazados
        cheques = cheques_data.get("cheques", [])
        if cheques:
            alertas.append(f"⚠️ {len(cheques)} cheque(s) rechazado(s)")
        
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
    tasa_periodo = (tna / 100) * (dias / 365)
    interes = monto * tasa_periodo
    neto = monto - interes
    cft = tasa_periodo * (365 / dias) * 100
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
    texto = """🏦 *BAI Group SA — Bot Operativo*

Comandos disponibles:

🔍 `/evaluar [CUIT]`
Evaluación crediticia completa vía BCRA

💰 `/cotizar [monto] [días] [tna]`
Calcula el descuento de un cheque

📋 `/nuevo [CUIT] [monto] [días] [tna]`
Registra una operación nueva

📅 `/vencer`
Cheques que vencen en los próximos 7 días

📊 `/cartera`
Resumen de operaciones activas

_Ej: /evaluar 30578639868_
_Ej: /cotizar 1000000 90 85_"""
    await update.message.reply_text(texto, parse_mode="Markdown")

async def evaluar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usá: `/evaluar [CUIT]`\nEj: `/evaluar 30578639868`", parse_mode="Markdown")
        return

    cuit = context.args[0]
    msg = await update.message.reply_text(f"🔍 Consultando BCRA para CUIT `{cuit}`...", parse_mode="Markdown")

    # Consultar BCRA
    bcra = await consultar_bcra(cuit)
    cheques = await consultar_cheques_rechazados(cuit)

    if not bcra["ok"]:
        await msg.edit_text(f"⚠️ *BCRA:* {bcra['error']}\n\nEl CUIT puede no tener deuda registrada en el sistema financiero.", parse_mode="Markdown")
        return

    await msg.edit_text("🤖 Analizando con IA...", parse_mode="Markdown")

    # Analizar
    analisis = await analizar_con_claude(cuit, bcra["data"], cheques)

    await msg.edit_text(analisis, parse_mode="Markdown")

async def cotizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "❌ Usá: `/cotizar [monto] [días] [tna]`\nEj: `/cotizar 1000000 90 85`",
            parse_mode="Markdown"
        )
        return

    try:
        monto = float(context.args[0].replace(",", "").replace(".", ""))
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
📊 *CFT anual:* {r['cft_anual']}%
━━━━━━━━━━━━━━━

_Vence en {dias} días — {(datetime.now() + timedelta(days=dias)).strftime('%d/%m/%Y')}_"""

        await update.message.reply_text(texto, parse_mode="Markdown")

    except ValueError:
        await update.message.reply_text("❌ Datos inválidos. Usá números: `/cotizar 1000000 90 85`", parse_mode="Markdown")

async def nuevo(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 BAI Group Bot iniciado...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
