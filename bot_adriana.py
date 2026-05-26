import imaplib
import email
import re
import time
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIGURACIÓN (usar variables de entorno) ---
# Para configurar, ejecuta en tu terminal antes de iniciar el bot:
#   export GMAIL_PASSWORD="tu_contraseña_de_aplicacion"
#   export TELEGRAM_TOKEN="tu_token_de_telegram"
#   export MI_CHAT_ID="tu_chat_id"
#
# O crea un archivo .env y usa python-dotenv para cargarlo.

PASSWORD     = os.environ.get("GMAIL_PASSWORD", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
MI_CHAT_ID   = int(os.environ.get("MI_CHAT_ID", "0"))

if not PASSWORD or not TELEGRAM_TOKEN or not MI_CHAT_ID:
    raise RuntimeError(
        "❌ Faltan variables de entorno.\n"
        "Define GMAIL_PASSWORD, TELEGRAM_TOKEN y MI_CHAT_ID antes de iniciar el bot."
    )

# ==================================================
# LISTA DE CUENTAS (01kaosnet@gmail.com ... 100kaosnet@gmail.com)
# ==================================================
NUMEROS = [str(n).zfill(2) for n in range(1, 101)]  # ["01", "02", ..., "99", "100"]


# --------------------------------------------------
# REGEX PARA CÓDIGOS OTP
# Busca exactamente 4, 6 u 8 dígitos seguidos,
# que NO estén pegados a otros dígitos (evita fechas, teléfonos, etc.)
# --------------------------------------------------
OTP_REGEX = re.compile(r'(?<!\d)(\d{4}|\d{6}|\d{8})(?!\d)')

# Palabras clave en el cuerpo que indican que el código está cerca
CONTEXT_KEYWORDS = ["código", "code", "verificación", "verification", "acceso", "access"]


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


def buscar_en_cuenta(correo: str) -> tuple[str | None, str | None]:
    """
    Conecta a una cuenta de Gmail vía IMAP y busca el código más reciente
    de un correo de Netflix. Cierra la conexión siempre, incluso si hay error.
    """
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
        mail.login(correo, PASSWORD)
        mail.select("inbox")

        # Buscar correos de Netflix directamente desde el servidor (más eficiente)
        status, ids = mail.search(None, 'FROM "netflix.com"')
        ids_lista = ids[0].split()

        if not ids_lista:
            return None, None

        # Revisar los últimos 30 correos de Netflix (de más reciente a más antiguo)
        for id_correo in reversed(ids_lista[-30:]):
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
                if codigo:
                    return correo, codigo

        return None, None

    except imaplib.IMAP4.error as e:
        print(f"   [IMAP Error] {correo}: {e}")
        return None, None
    except Exception as e:
        print(f"   [Error] {correo}: {e}")
        return None, None
    finally:
        # Siempre cerrar la conexión, sin importar qué pasó
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


def buscar_codigo(numero_filtro: str = "") -> tuple[str | None, str | None]:
    """
    Busca en una cuenta específica (si se da filtro) o en todas las cuentas.
    """
    if numero_filtro:
        numeros_a_buscar = [numero_filtro.zfill(2)]
    else:
        numeros_a_buscar = NUMEROS

    print(f"🔍 Buscando en {len(numeros_a_buscar)} cuenta(s)...")

    for numero in numeros_a_buscar:
        correo = f"{numero}kaosnet@gmail.com"
        print(f"   Revisando: {correo}")

        cuenta, codigo = buscar_en_cuenta(correo)
        if codigo:
            return cuenta, codigo

        time.sleep(1)  # Pausa de 1 segundo para no saturar Gmail

    return None, None


# --------------------------------------------------
# HANDLERS DE TELEGRAM
# --------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MI_CHAT_ID:
        return
    await update.message.reply_text(
        "🤖 *Bot de Códigos v13.0*\n\n"
        "✅ Revisa las 100 cuentas individualmente\n"
        "📧 Rango: `01kaosnet@gmail.com` → `100kaosnet@gmail.com`\n\n"
        "📌 *Ejemplos:*\n"
        "• `/netflix` → Busca en TODAS las cuentas\n"
        "• `/netflix 46` → Busca SOLO en `46kaosnet@gmail.com`\n"
        "• `/netflix 60` → Busca SOLO en `60kaosnet@gmail.com`\n\n"
        "⚡ Revisa la bandeja de cada cuenta individualmente.",
        parse_mode="Markdown"
    )


async def netflix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MI_CHAT_ID:
        return

    filtro = context.args[0] if context.args else ""
    destino = f"la cuenta *{filtro.zfill(2)}kaosnet@gmail.com*" if filtro else "todas las cuentas"
    mensaje_espera = await update.message.reply_text(
        f"🔍 Buscando en {destino}...\n_(puede tomar hasta 60 segundos)_",
        parse_mode="Markdown"
    )

    cuenta, codigo = buscar_codigo(filtro)

    await mensaje_espera.delete()

    if codigo:
        await update.message.reply_text(
            f"✅ *Código de Netflix encontrado*\n\n"
            f"📧 Cuenta: `{cuenta}`\n"
            f"🔢 Código: `{codigo}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "❌ No encontré códigos de Netflix.\n\n"
            "💡 *Posibles causas:*\n"
            "• El correo aún no llegó (espera unos segundos y vuelve a intentar)\n"
            "• La contraseña de aplicación expiró\n"
            "• El número de cuenta no tiene correos de Netflix",
            parse_mode="Markdown"
        )


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    print("🤖 Bot v13.0 iniciando...")
    print(f"📧 Revisará {len(NUMEROS)} cuentas individualmente")
    print("   Rango: 01kaosnet@gmail.com → 100kaosnet@gmail.com")
    print("✅ Bot listo!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("netflix", netflix))
    app.run_polling()


if __name__ == "__main__":
    main()
