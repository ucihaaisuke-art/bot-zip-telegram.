import asyncio
import io
import logging
import os
import re
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageOps
from telegram import InputMediaPhoto
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler

Image.MAX_IMAGE_PIXELS = 25_000_000
busy = set()
logger = logging.getLogger(__name__)
MAX_RATE_LIMIT_RETRIES = 5

PROCESSING_TEXT = os.environ.get(
    "PROCESSING_TEXT",
    "Gambarnya lagi proses ya tuan/putri ,\n"
    'mohon ditunggu dengan sabar " kalo ga sabar buat sendiri aja',
)

SUCCESS_TEXT = os.environ.get(
    "SUCCESS_TEXT",
    "GAMBAR DONE YA BAPAK/IBU JOKERPLAY!!",
)


async def telegram_call(method, **kwargs):
    """Retry only explicit flood-limit rejections, never uncertain deliveries."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return await method(**kwargs)
        except RetryAfter as error:
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise

            delay = error.retry_after
            if hasattr(delay, "total_seconds"):
                delay = delay.total_seconds()

            logger.warning(
                "Telegram membatasi pengiriman; menunggu %.1f detik",
                delay + 1,
            )
            await asyncio.sleep(delay + 1)


def prepare(path):
    with zipfile.ZipFile(path) as archive:
        entries = [
            item for item in archive.infolist()
            if not item.is_dir()
            and item.filename.lower().endswith((".jpg", ".jpeg"))
            and not item.filename.startswith("__MACOSX/")
        ]

        if not entries or len(entries) > 200:
            raise ValueError("ZIP harus berisi 1–200 JPG/JPEG.")

        if sum(x.file_size for x in archive.infolist()) > 200 * 1024**2:
            raise ValueError("Isi ZIP maksimal 200 MB.")

        def order(item):
            return [
                int(part) if part.isdigit() else part.lower()
                for part in re.split(r"(\d+)", item.filename)
            ]

        photos = []
        for item in sorted(entries, key=order):
            with archive.open(item) as source:
                with Image.open(source) as image:
                    if image.format != "JPEG":
                        raise ValueError("Ada file yang bukan JPEG asli.")

                    image = ImageOps.exif_transpose(image).convert("RGB")
                    image.thumbnail((2560, 2560))

                    if max(image.size) / min(image.size) > 20:
                        raise ValueError("Gambar terlalu panjang.")

                    output = io.BytesIO()
                    image.save(output, "JPEG", quality=90)
                    photos.append(output.getvalue())

        return photos


async def start(update, context):
    await telegram_call(
        update.effective_message.reply_text,
        text="Kirim ZIP berisi JPG/JPEG, lalu balas ZIP dengan "
        "/proses atau /proses@username_bot. Maksimal ZIP 20 MB.",
    )


async def process(update, context):
    message = update.effective_message
    source = message.reply_to_message

    if not source or not source.document:
        await telegram_call(
            message.reply_text,
            text="Balas pesan ZIP dengan /proses.",
        )
        return

    document = source.document

    if not (document.file_name or "").lower().endswith(".zip"):
        await telegram_call(
            message.reply_text,
            text="File harus berformat ZIP.",
        )
        return

    if (document.file_size or 0) > 20 * 1024**2:
        await telegram_call(
            message.reply_text,
            text="Ukuran ZIP maksimal 20 MB.",
        )
        return

    if message.chat_id in busy:
        await telegram_call(
            message.reply_text,
            text="Tunggu, ZIP sebelumnya masih diproses.",
        )
        return

    busy.add(message.chat_id)
    sent = 0
    phase = "download"

    try:
        await telegram_call(
            message.reply_text,
            text=PROCESSING_TEXT,
        )

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.zip"
            remote = await telegram_call(document.get_file)
            await remote.download_to_drive(path)

            phase = "prepare"
            photos = await asyncio.to_thread(prepare, path)

        phase = "send"
        title = Path(document.file_name).stem[:200]
        total = (len(photos) + 9) // 10

        for offset in range(0, len(photos), 10):
            batch = photos[offset:offset + 10]
            caption = f"{title}\nBagian {offset // 10 + 1}/{total}"

            options = {
                "chat_id": message.chat_id,
                "message_thread_id": message.message_thread_id,
            }

            if len(batch) == 1:
                await telegram_call(
                    context.bot.send_photo,
                    photo=batch[0],
                    caption=caption,
                    **options,
                )
            else:
                media = [
                    InputMediaPhoto(
                        photo,
                        caption=caption if index == 0 else None,
                    )
                    for index, photo in enumerate(batch)
                ]

                await telegram_call(
                    context.bot.send_media_group,
                    media=media,
                    **options,
                )

            sent += len(batch)

            # Jeda grup: 3,5 detik per foto, atau 35 detik per 10 foto.
            # Japri tetap menggunakan jeda 1 detik.
            is_group = message.chat.type in ("group", "supergroup")
            await asyncio.sleep(len(batch) * 3.5 if is_group else 1)

        await telegram_call(
            message.reply_text,
            text=SUCCESS_TEXT.replace("{jumlah}", str(sent)),
        )

    except Exception as error:
        # Jangan log exception mentah karena bisa memuat URL token bot.
        logger.error(
            "Proses ZIP terhenti: tahap=%s foto_terkonfirmasi=%d jenis=%s",
            phase,
            sent,
            type(error).__name__,
        )

        if isinstance(error, RetryAfter):
            reason = (
                "Telegram masih membatasi pengiriman setelah beberapa "
                "percobaan. Coba lagi nanti."
            )
        elif isinstance(error, Forbidden):
            reason = (
                "Telegram menolak akses bot. "
                "Periksa izin bot untuk mengirim foto di grup."
            )
        elif isinstance(error, BadRequest):
            reason = (
                "Telegram menolak permintaan. "
                "Periksa izin bot, foto, dan pengaturan grup."
            )
        elif isinstance(error, NetworkError):
            reason = (
                "Koneksi ke Telegram bermasalah atau melewati batas waktu."
            )
            if phase == "send":
                reason += (
                    " Kiriman terakhir mungkin sudah diterima; "
                    "periksa chat sebelum mengulang."
                )
        elif isinstance(error, TelegramError):
            reason = (
                "Telegram menolak permintaan. "
                "Periksa izin bot dan pengaturan grup."
            )
        elif phase == "prepare":
            reason = (
                "ZIP tidak dapat diproses. Pastikan tidak berpassword, "
                "berisi 1–200 JPG/JPEG valid, maksimal 200 MB setelah dibuka, "
                "dan gambar tidak terlalu panjang."
            )
        elif phase == "download":
            reason = (
                "ZIP gagal diunduh dari Telegram. Coba kirim ulang file."
            )
        else:
            reason = (
                "Terjadi kesalahan saat menyiapkan pengiriman foto."
            )

        try:
            await telegram_call(
                message.reply_text,
                text=(
                    f"Proses terhenti. {sent} foto terkonfirmasi terkirim. "
                    f"{reason} Jika /proses diulang, pengiriman mulai dari "
                    "awal dan foto bisa terkirim dua kali."
                ),
            )
        except TelegramError as notice_error:
            logger.error(
                "Pemberitahuan gagal dikirim: jenis=%s",
                type(notice_error).__name__,
            )

    finally:
        busy.discard(message.chat_id)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diatur.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("proses", process))
    app.run_polling()


if __name__ == "__main__":
    main()
