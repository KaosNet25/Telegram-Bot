import imaplib
import email
import json
import re
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIGURACIÓN (usar variables de entorno) ---
# Para configurar, ejecuta en tu terminal antes de iniciar el bot:
#   export GMAIL_PASSWORD="tu_contraseña_de_aplicacion"
#   export TELEGRAM_TOKEN="tu_token_de_telegram"
#   export MI_CHAT_ID="tu_chat_id"
#   export MASTER_INBOX="34kaosnet@gmail.com"
#
# O crea un archivo .env y usa python-dotenv para cargarlo.

PASSWORD       = os.environ.get("GMAIL_PASSWORD", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
MI_CHAT_ID     = int(os.environ.get("MI_CHAT_ID", "0"))
MASTER_INBOX   = os.environ.get("MASTER_INBOX", "")

if not PASSWORD or not TELEGRAM_TOKEN or not MI_CHAT_ID or not MASTER_INBOX:
    raise RuntimeError(
        "❌ Faltan variables de entorno.\n"
        "Define GMAIL_PASSWORD, TELEGRAM_TOKEN, MI_CHAT_ID y MASTER_INBOX antes de iniciar el bot."
    )

# Regex para extraer el número de cuenta de una dirección NNkaosnet@gmail.com
ACCOUNT_REGEX = re.compile(r'(\d{1,3})kaosnet@gmail\.com', re.IGNORECASE)

# Número de cuenta del master (None si el master no tiene formato NNkaosnet)
_match_master = ACCOUNT_REGEX.search(MASTER_INBOX)
MASTER_NUMERO = f"{int(_match_master.group(1)):02d}" if _match_master else None

# Cuántos correos recientes leer del master en cada consulta
MAX_CORREOS = 30

# Cuántos códigos mostrar cuando /netflix se invoca sin argumento
MAX_RESULTADOS_LISTADO = 5


# --------------------------------------------------
# AUTORIZACIÓN MULTI-USUARIO
# Adriana (MI_CHAT_ID) es admin permanente, hardcoded vía env var.
# Otros usuarios autorizados viven en AUTH_PATH (Railway Volume persistente).
# Si /data no está montado (no hay volume), fallback a /tmp — funciona pero
# la lista NO sobrevive reinicios del contenedor.
# --------------------------------------------------
AUTH_PATH = os.environ.get("AUTH_PATH", "/data/authorized.json")
try:
    os.makedirs(os.path.dirname(AUTH_PATH), exist_ok=True)
    _probe = AUTH_PATH + ".probe"
    with open(_probe, "w") as _f:
        _f.write("")
    os.remove(_probe)
except OSError:
    AUTH_PATH = "/tmp/authorized.json"
    print(f"⚠️ /data no escribible (Railway Volume no montado). Fallback: {AUTH_PATH} (NO persiste entre reinicios)")


def cargar_autorizados() -> dict:
    if not os.path.exists(AUTH_PATH):
        return {}
    try:
        with open(AUTH_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️ No pude leer {AUTH_PATH}: {e}. Empezando con lista vacía.")
        return {}


def guardar_autorizados(data: dict) -> None:
    with open(AUTH_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


autorizados = cargar_autorizados()


def es_admin(user_id: int) -> bool:
    return user_id == MI_CHAT_ID


def es_autorizado(user_id: int) -> bool:
    return es_admin(user_id) or str(user_id) in autorizados


# --------------------------------------------------
# REGEX PARA CÓDIGOS OTP
# Busca exactamente 4, 6 u 8 dígitos seguidos,
# que NO estén pegados a otros dígitos (evita fechas, teléfonos, etc.)
# --------------------------------------------------
OTP_REGEX = re.compile(r'(?<!\d)(\d{4}|\d{6}|\d{8})(?!\d)')

# Palabras clave en el cuerpo que indican que el código está cerca
CONTEXT_KEYWORDS = ["código", "code", "verificación", "verification", "acceso", "access"]

# Link del botón "Obtener código" en correos de "código de acceso temporal" (viaje).
# El path /account/travel/verify es único de ese tipo de correo.
TRAVEL_REGEX = re.compile(
    r'https?://[^\s<>"\']*netflix\.com/account/travel/verify[^\s<>"\']*',
    re.IGNORECASE
)


def extraer_codigo_otp(cuerpo: str) -> str | None:
    """
    Intenta extraer un código OTP del cuerpo del correo.
    Primero busca dígitos con espacios entre ellos (ej: "1 2 3 4"),
    luego busca un bloque de 4/6/8 dígitos cerca de palabras clave.
    """
    # 1. Dígitos separados por espacios (ej: "1 2 3 4" o "1 2 3 4 5 6")
    match_espacios = re.search(r'(?<!\d)(\d)\s+(\d)\s+(\d)\s+(\d)(?:\s+(\d)\s+(\d))?(?!\d)', cuerpo)
    if match_espacios:
        grupos = [g for g in match_espacios.groups() if g is not None]
        return "".join(grupos)

    # 2. Bloque de dígitos cerca de una palabra clave
    lineas = cuerpo.lower().splitlines()
    for i, linea in enumerate(lineas):
        if any(kw in linea for kw in CONTEXT_KEYWORDS):
            # Revisar esta línea y las 2 siguientes
            fragmento = "\n".join(lineas[i:i+3])
            match = OTP_REGEX.search(fragmento)
            if match:
                return match.group(1)

    # 3. Fallback: primer bloque de dígitos que encuentre en el texto
    match = OTP_REGEX.search(cuerpo)
    if match:
        return match.group(1)

    return None


def extraer_link_viaje(cuerpo: str) -> str | None:
    """Devuelve la URL del botón 'Obtener código' si el correo es de viaje, sino None."""
    match = TRAVEL_REGEX.search(cuerpo)
    return match.group(0) if match else None


def extraer_cuenta_origen(msg) -> str | None:
    """
    Devuelve el número de cuenta original (zero-padded a 2 dígitos) del correo.
    Usa X-Forwarded-For (primera dirección de la cadena) si el correo llegó
    via reenvío; si no, asume que llegó directo al master y devuelve MASTER_NUMERO.
    """
    forwarded = msg.get("X-Forwarded-For")
    if forwarded:
        partes = forwarded.split()
        if partes:
            match = ACCOUNT_REGEX.search(partes[0])
            if match:
                return f"{int(match.group(1)):02d}"
        return None
    # No hay X-Forwarded-For → llegó directo al master
    return MASTER_NUMERO


def tiempo_relativo(dt: datetime) -> str:
    """Formatea un datetime como tiempo relativo: 'hace 15 seg', 'hace 2 min', etc."""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    segundos = int(delta.total_seconds())
    if segundos < 60:
        return f"hace {segundos} seg"
    minutos = segundos // 60
    if minutos < 60:
        return f"hace {minutos} min"
    horas = minutos // 60
    if horas < 24:
        return f"hace {horas} h"
    dias = horas // 24
    return f"hace {dias} d"


def buscar_codigos_en_master() -> list[dict]:
    """
    Conecta al MASTER_INBOX, lee los últimos correos de Netflix, y devuelve
    una lista de dicts:
      {"cuenta": "17", "tipo": "otp",   "valor": "1234",          "dt": datetime}
      {"cuenta": "42", "tipo": "viaje", "valor": "https://...",   "dt": datetime}
    Ordenada del más reciente al más antiguo.
    """
    resultados = []
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
        mail.login(MASTER_INBOX, PASSWORD)
        mail.select("inbox")

        # Buscar correos de Netflix directamente desde el servidor (más eficiente)
        status, ids = mail.search(None, 'FROM "netflix.com"')
        ids_lista = ids[0].split()

        if not ids_lista:
            return []

        # Revisar los últimos MAX_CORREOS correos de Netflix (de más reciente a más antiguo)
        for id_correo in reversed(ids_lista[-MAX_CORREOS:]):
            _, data = mail.fetch(id_correo, "(RFC822)")
            for part in data:
                if not isinstance(part, tuple):
                    continue

                msg = email.message_from_bytes(part[1])
                asunto = msg.get("Subject", "").lower()

                # Filtrar solo correos con "netflix" en el asunto
                if "netflix" not in asunto:
                    continue

                # Extraer el cuerpo en texto plano
                cuerpo = ""
                if msg.is_multipart():
                    for p in msg.walk():
                        if p.get_content_type() == "text/plain":
                            payload = p.get_payload(decode=True)
                            if payload:
                                cuerpo += payload.decode("utf-8", errors="ignore")
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        cuerpo = payload.decode("utf-8", errors="ignore")

                codigo = extraer_codigo_otp(cuerpo)
                link_viaje = None if codigo else extraer_link_viaje(cuerpo)
                if not codigo and not link_viaje:
                    continue

                cuenta = extraer_cuenta_origen(msg)
                if not cuenta:
                    # No identificable (master sin formato NN y sin X-Forwarded-For)
                    continue

                try:
                    dt = parsedate_to_datetime(msg.get("Date", ""))
                except (TypeError, ValueError):
                    dt = datetime.now(timezone.utc)

                if codigo:
                    resultados.append({"cuenta": cuenta, "tipo": "otp", "valor": codigo, "dt": dt})
                else:
                    resultados.append({"cuenta": cuenta, "tipo": "viaje", "valor": link_viaje, "dt": dt})

        return resultados

    except imaplib.IMAP4.error as e:
        print(f"   [IMAP Error] {MASTER_INBOX}: {e}")
        return []
    except Exception as e:
        print(f"   [Error] {MASTER_INBOX}: {e}")
        return []
    finally:
        # Siempre cerrar la conexión, sin importar qué pasó
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


# --------------------------------------------------
# HANDLERS DE TELEGRAM
# --------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not es_autorizado(user_id):
        return
    texto = (
        "🤖 *Bot de Códigos v14.2*\n\n"
        "✅ Lee todos los códigos desde un solo buzón master\n"
        f"📥 Buzón master actual: `{MASTER_INBOX}`\n\n"
        "📌 *Comandos:*\n"
        "• `/netflix 17` → código de la cuenta `17kaosnet`\n"
        "• `/netflix 42` → código de la cuenta `42kaosnet`\n"
        "• `/netflix` (sin número) → últimos códigos con cuenta y hora\n\n"
        "🧳 *Códigos de viaje:* Netflix los manda como link (no como número). "
        "El bot detecta el link y te lo pasa; al abrirlo verás el código (vence en 15 min)."
    )
    if es_admin(user_id):
        texto += (
            "\n\n🔐 *Comandos de admin:*\n"
            "• `/autorizar <id> <nombre>` → dar acceso a otra persona\n"
            "• `/revocar <id>` → quitar acceso\n"
            "• `/lista` → ver quién tiene acceso\n\n"
            "_Cada persona obtiene su ID escribiéndole `/start` a_ `@userinfobot` _en Telegram._"
        )
    await update.message.reply_text(texto, parse_mode="Markdown")


async def autorizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Uso: `/autorizar <id> <nombre>`\n"
            "Ejemplo: `/autorizar 1091637952 Pedro`\n\n"
            "El ID lo obtiene cada persona escribiéndole `/start` a `@userinfobot`.",
            parse_mode="Markdown"
        )
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ El ID debe ser un número entero.", parse_mode="Markdown")
        return
    if target_id == MI_CHAT_ID:
        await update.message.reply_text(
            "ℹ️ Ya eres admin permanente, no hace falta autorizarte.",
            parse_mode="Markdown"
        )
        return
    nombre = " ".join(context.args[1:])
    autorizados[str(target_id)] = {
        "nombre": nombre,
        "agregado": datetime.now(timezone.utc).isoformat()
    }
    guardar_autorizados(autorizados)
    await update.message.reply_text(
        f"✅ Autorizado: *{nombre}* (`{target_id}`)\n\n"
        f"Total: {len(autorizados)} usuarios (sin contarte).",
        parse_mode="Markdown"
    )


async def revocar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ Uso: `/revocar <id>`\n"
            "Ejemplo: `/revocar 1091637952`\n\n"
            "Usa `/lista` para ver los IDs autorizados.",
            parse_mode="Markdown"
        )
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ El ID debe ser un número entero.", parse_mode="Markdown")
        return
    if target_id == MI_CHAT_ID:
        await update.message.reply_text(
            "❌ No puedes revocarte a ti misma (eres admin permanente).",
            parse_mode="Markdown"
        )
        return
    info = autorizados.pop(str(target_id), None)
    if info is None:
        await update.message.reply_text(
            f"ℹ️ El ID `{target_id}` no estaba en la lista. Nada que hacer.",
            parse_mode="Markdown"
        )
        return
    guardar_autorizados(autorizados)
    await update.message.reply_text(
        f"✅ Revocado: *{info.get('nombre', '?')}* (`{target_id}`).\n\n"
        f"Quedan {len(autorizados)} usuarios autorizados (sin contarte).",
        parse_mode="Markdown"
    )


async def lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if not autorizados:
        await update.message.reply_text(
            "👤 Solo tú tienes acceso por ahora.\n\n"
            "Para agregar a alguien: `/autorizar <id> <nombre>`",
            parse_mode="Markdown"
        )
        return
    lineas = [f"👥 *Usuarios autorizados ({len(autorizados)}):*\n"]
    for uid, info in autorizados.items():
        try:
            dt = datetime.fromisoformat(info["agregado"])
            tiempo = tiempo_relativo(dt)
        except (KeyError, ValueError, TypeError):
            tiempo = "?"
        lineas.append(f"• *{info.get('nombre', '?')}* — `{uid}` — agregado {tiempo}")
    lineas.append("\n_(Tú eres admin permanente, no apareces en la lista)_")
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")


async def netflix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_autorizado(update.effective_user.id):
        return

    filtro_raw = context.args[0] if context.args else ""
    filtro = None
    if filtro_raw:
        try:
            n = int(filtro_raw)
            if not (1 <= n <= 100):
                await update.message.reply_text(
                    "❌ El número de cuenta debe estar entre 01 y 100.",
                    parse_mode="Markdown"
                )
                return
            filtro = f"{n:02d}"
        except ValueError:
            await update.message.reply_text(
                "❌ Formato inválido. Usa por ejemplo: `/netflix 17`",
                parse_mode="Markdown"
            )
            return

    destino = f"la cuenta *{filtro}kaosnet@gmail.com*" if filtro else "el buzón master"
    mensaje_espera = await update.message.reply_text(
        f"🔍 Buscando en {destino}...",
        parse_mode="Markdown"
    )

    resultados = buscar_codigos_en_master()
    await mensaje_espera.delete()

    if not resultados:
        await update.message.reply_text(
            "❌ No encontré correos de Netflix recientes en el buzón master.\n\n"
            "💡 Verifica que el cliente haya intentado entrar y vuelve a intentar en unos segundos.",
            parse_mode="Markdown"
        )
        return

    # Ordenar siempre del más reciente al más antiguo
    resultados.sort(key=lambda r: r["dt"], reverse=True)

    if filtro:
        coincidencias = [r for r in resultados if r["cuenta"] == filtro]
        if not coincidencias:
            await update.message.reply_text(
                f"❌ No encontré códigos para la cuenta *{filtro}*.\n\n"
                "💡 Verifica que el cliente haya intentado entrar y vuelve a intentar en unos segundos.",
                parse_mode="Markdown"
            )
            return
        mas_reciente = coincidencias[0]
        if mas_reciente['tipo'] == 'otp':
            await update.message.reply_text(
                f"✅ *Código de Netflix encontrado*\n\n"
                f"📧 Cuenta: `{mas_reciente['cuenta']}kaosnet@gmail.com`\n"
                f"🔢 Código: `{mas_reciente['valor']}`\n"
                f"⏱ Recibido: {tiempo_relativo(mas_reciente['dt'])}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"🧳 *Código de viaje*\n\n"
                f"📧 Cuenta: `{mas_reciente['cuenta']}kaosnet@gmail.com`\n"
                f"⏱ Recibido: {tiempo_relativo(mas_reciente['dt'])}\n"
                f"_Abre este enlace para ver el código (vence en 15 min):_\n\n"
                f"{mas_reciente['valor']}",
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        return

    # Sin filtro: listar los últimos N códigos
    top = resultados[:MAX_RESULTADOS_LISTADO]
    lineas = ["📧 *Códigos recientes:*\n"]
    for r in top:
        if r['tipo'] == 'otp':
            lineas.append(
                f"• `{r['valor']}` — cuenta `{r['cuenta']}` — {tiempo_relativo(r['dt'])}"
            )
        else:
            lineas.append(
                f"• 🧳 viaje — cuenta `{r['cuenta']}` — {tiempo_relativo(r['dt'])}\n   {r['valor']}"
            )
    await update.message.reply_text(
        "\n".join(lineas),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    print("🤖 Bot v14.2 iniciando...")
    print(f"📥 Buzón master: {MASTER_INBOX}")
    if MASTER_NUMERO:
        print(f"   Número de cuenta del master: {MASTER_NUMERO}")
    else:
        print("   ⚠️ El master no tiene formato NNkaosnet@gmail.com — correos sin X-Forwarded-For serán ignorados")
    print(f"👥 Autorizados al arrancar: {len(autorizados)} usuario(s) + admin {MI_CHAT_ID}")
    print(f"💾 Persistencia: {AUTH_PATH}")
    print("✅ Bot listo!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("netflix", netflix))
    app.add_handler(CommandHandler("autorizar", autorizar))
    app.add_handler(CommandHandler("revocar", revocar))
    app.add_handler(CommandHandler("lista", lista))
    app.run_polling()


if __name__ == "__main__":
    main()
