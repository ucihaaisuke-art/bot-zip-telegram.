import asyncio
import ctypes
import ctypes.util
import io
import logging
import os
import re
import shutil
import tempfile
import warnings
import zipfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps
from telegram import InputMediaPhoto
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler

Image.MAX_IMAGE_PIXELS = 25_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)
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

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
RAR_SIGNATURES = (
    b"Rar!\x1a\x07\x00",
    b"Rar!\x1a\x07\x01\x00",
)
MIN_FREE = 64 * 1024**2


class ArchiveError(Exception):
    pass


def local_enabled():
    return os.environ.get(
        "ARSIP_LOCAL_BOT_API", "off"
    ).strip().lower() in ("on", "true", "1")


def api_directory():
    return Path(
        os.environ.get("ARSIP_API_DIR", "/data/arsip-bot-api")
    )


def local_port():
    port = int(os.environ.get("ARSIP_API_PORT", "8081"))
    if not 1024 <= port <= 65535:
        raise ValueError("ARSIP_API_PORT harus 1024–65535.")
    return port


def ensure_space(directory, needed=0):
    reserve = max(
        MIN_FREE,
        int(os.environ.get("ARSIP_MIN_FREE_MB", "64")) * 1024**2,
    )
    if shutil.disk_usage(directory).free < max(0, needed) + reserve:
        raise ArchiveError(
            "Ruang penyimpanan server tidak cukup. "
            "Kosongkan ruang atau tambah kapasitas penyimpanan."
        )


@lru_cache(maxsize=1)
def rar_library():
    name = ctypes.util.find_library("archive")
    lib = None

    for candidate in (name, "libarchive.so.13", "libarchive.so"):
        if not candidate:
            continue
        try:
            lib = ctypes.CDLL(candidate)
            break
        except OSError:
            continue

    if lib is None:
        raise ArchiveError(
            "Pembaca RAR belum terpasang. "
            "Gunakan Dockerfile yang menyertakan libarchive."
        )

    declarations = {
        "archive_read_new": (
            ctypes.c_void_p, []
        ),
        "archive_read_support_filter_all": (
            ctypes.c_int, [ctypes.c_void_p]
        ),
        "archive_read_support_format_rar": (
            ctypes.c_int, [ctypes.c_void_p]
        ),
        "archive_read_support_format_rar5": (
            ctypes.c_int, [ctypes.c_void_p]
        ),
        "archive_read_open_filename": (
            ctypes.c_int,
            [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t],
        ),
        "archive_read_next_header": (
            ctypes.c_int,
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
        ),
        "archive_entry_pathname_utf8": (
            ctypes.c_char_p, [ctypes.c_void_p]
        ),
        "archive_entry_pathname": (
            ctypes.c_char_p, [ctypes.c_void_p]
        ),
        "archive_entry_filetype": (
            ctypes.c_uint, [ctypes.c_void_p]
        ),
        "archive_entry_size": (
            ctypes.c_int64, [ctypes.c_void_p]
        ),
        "archive_read_data": (
            ctypes.c_ssize_t,
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t],
        ),
        "archive_read_free": (
            ctypes.c_int, [ctypes.c_void_p]
        ),
    }

    for function, (result, arguments) in declarations.items():
        method = getattr(lib, function)
        method.restype = result
        method.argtypes = arguments

    return lib


class RarReader:
    def __init__(self, path):
        self.lib = rar_library()
        self.handle = self.lib.archive_read_new()

        if not self.handle:
            raise ArchiveError("Pembaca RAR kehabisan memori.")

        self.buffer = ctypes.create_string_buffer(128 * 1024)
        self.lib.archive_read_support_filter_all(self.handle)
        self.lib.archive_read_support_format_rar(self.handle)
        self.lib.archive_read_support_format_rar5(self.handle)

        result = self.lib.archive_read_open_filename(
            self.handle,
            os.fsencode(path),
            128 * 1024,
        )

        if result != 0:
            self.close()
            raise ArchiveError(
                "RAR tidak dapat dibuka. Pastikan arsip lengkap, "
                "tidak rusak, dan tidak berpassword."
            )

    def entries(self):
        while True:
            entry = ctypes.c_void_p()
            status = self.lib.archive_read_next_header(
                self.handle, ctypes.byref(entry)
            )

            if status == 1:
                return

            if status != 0:
                raise ArchiveError(
                    "RAR tidak dapat dibaca sampai selesai. "
                    "Periksa password, kerusakan, atau bagian "
                    "arsip yang belum lengkap."
                )

            raw = (
                self.lib.archive_entry_pathname_utf8(entry)
                or self.lib.archive_entry_pathname(entry)
            )
            name = (raw or b"").decode("utf-8", errors="replace")
            filetype = self.lib.archive_entry_filetype(entry)

            if filetype & 0o170000 == 0o100000:
                yield (
                    name,
                    self.lib.archive_entry_size(entry),
                    self,
                )

    def read(self, size):
        count = self.lib.archive_read_data(
            self.handle,
            self.buffer,
            min(size, len(self.buffer)),
        )

        if count < 0:
            raise ArchiveError(
                "Isi RAR gagal dibaca. Arsip mungkin rusak, "
                "berpassword, atau membutuhkan bagian lain."
            )

        return self.buffer.raw[:count]

    def close(self):
        if self.handle:
            self.lib.archive_read_free(self.handle)
            self.handle = None


def is_image(name):
    name = name.replace("\\", "/")
    return (
        not name.startswith("__MACOSX/")
        and name.lower().endswith(IMAGE_EXTENSIONS)
    )


def records(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            def order(item):
                return [
                    (1, int(part)) if part.isdigit()
                    else (0, part.casefold())
                    for part in re.split(r"(\d+)", item.filename)
                ]

            for item in sorted(archive.infolist(), key=order):
                if not item.is_dir() and is_image(item.filename):
                    if item.flag_bits & 1:
                        raise ArchiveError(
                            "ZIP berpassword belum dapat diproses. "
                            "Kirim arsip tanpa password."
                        )

                    with archive.open(item) as source:
                        yield item.filename, item.file_size, source
        return

    with Path(path).open("rb") as source:
        signature = source.read(8)

    if not any(
        signature.startswith(value) for value in RAR_SIGNATURES
    ):
        raise ArchiveError("File bukan ZIP atau RAR yang valid.")

    archive = RarReader(path)
    try:
        for name, size, source in archive.entries():
            if is_image(name):
                yield name, size, source
    finally:
        archive.close()


async def telegram_call(method, **kwargs):
    """Ulangi hanya penolakan rate limit yang sudah pasti."""
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


def prepare(path, folder, stats):
    """Baca gambar bertahap, maksimal satu album dalam penampung."""
    stats["total"] = sum(1 for _ in records(path))
    stats["skipped"] = 0

    if not stats["total"]:
        raise ValueError(
            "Arsip tidak berisi JPG, JPEG, PNG, atau WEBP."
        )

    batch = []

    for name, size, source in records(path):
        ensure_space(folder, size)

        with tempfile.SpooledTemporaryFile(
            max_size=2 * 1024**2,
            dir=folder,
        ) as raw:
            while chunk := source.read(128 * 1024):
                ensure_space(folder, len(chunk))
                raw.write(chunk)

            raw.seek(0)

            try:
                with Image.open(raw) as original:
                    if original.format not in ("JPEG", "PNG", "WEBP"):
                        raise ValueError(
                            "Bukan foto yang didukung."
                        )

                    image = ImageOps.exif_transpose(original).convert("RGB")
                    image.thumbnail((2560, 2560))

                    if max(image.size) / min(image.size) > 20:
                        raise ValueError("Gambar terlalu panjang.")

                    output = io.BytesIO()
                    image.save(output, "JPEG", quality=90)
                    batch.append(output.getvalue())
                    image.close()

            except (
                OSError,
                ValueError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ):
                stats["skipped"] += 1
                logger.warning(
                    "Satu gambar tidak dapat dijadikan foto Telegram; "
                    "gambar lain dilanjutkan."
                )

        if len(batch) == 10:
            yield batch
            batch = []

    if batch:
        yield batch


def next_batch(iterator):
    return next(iterator, None)


async def worker_call(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))

    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def start(update, context):
    mode_text = (
        "Ukuran arsip mengikuti ruang server yang tersedia."
        if local_enabled()
        else "Bot masih memakai Bot API cloud: "
             "unduhan dibatasi Telegram hingga 20 MB."
    )

    await telegram_call(
        update.effective_message.reply_text,
        text=(
            "Kirim ZIP atau RAR berisi JPG/JPEG, PNG, atau WEBP, "
            "lalu balas arsip dengan /proses atau "
            "/proses@username_bot. Jumlah foto tidak dibatasi. "
            + mode_text
        ),
    )


async def process(update, context):
    message = update.effective_message
    source = message.reply_to_message

    if not source or not source.document:
        await telegram_call(
            message.reply_text,
            text="Balas pesan ZIP atau RAR dengan /proses.",
        )
        return

    document = source.document

    if not (document.file_name or "").lower().endswith((".zip", ".rar")):
        await telegram_call(
            message.reply_text,
            text="File harus berformat ZIP atau RAR.",
        )
        return

    if (
        not local_enabled()
        and (document.file_size or 0) > 20 * 1024**2
    ):
        await telegram_call(
            message.reply_text,
            text=(
                "File ini melebihi batas unduh 20 MB Bot API cloud. "
                "Aktifkan Bot API lokal pada server bot arsip."
            ),
        )
        return

    if message.chat_id in busy:
        await telegram_call(
            message.reply_text,
            text="Tunggu, arsip sebelumnya masih diproses.",
        )
        return

    busy.add(message.chat_id)
    sent = 0
    phase = "download"
    iterator = None
    stats = {}
    label = Path(document.file_name).suffix[1:].upper()

    try:
        await telegram_call(
            message.reply_text,
            text=PROCESSING_TEXT,
        )

        work = Path(
            os.environ.get(
                "ARSIP_WORK_DIR",
                "/data/arsip-work"
                if local_enabled()
                else tempfile.gettempdir(),
            )
        )
        work.mkdir(parents=True, exist_ok=True)
        ensure_space(work, document.file_size or 0)

        with tempfile.TemporaryDirectory(dir=work) as folder:
            remote = await telegram_call(
                document.get_file,
                read_timeout=1800,
            )

            if local_enabled():
                path = Path(
                    getattr(remote, "file_path", "") or ""
                ).resolve()
                root = api_directory().resolve()

                if not path.is_relative_to(root) or not path.is_file():
                    raise ArchiveError(
                        "Arsip Bot API lokal tidak ditemukan. "
                        "Pastikan bot dan server API memakai "
                        "filesystem yang sama."
                    )
            else:
                path = Path(folder) / (
                    "input" + Path(document.file_name).suffix.lower()
                )
                await remote.download_to_drive(path)

            iterator = prepare(path, folder, stats)
            part = 0

            try:
                while True:
                    phase = "prepare"
                    batch = await worker_call(next_batch, iterator)

                    if batch is None:
                        break

                    phase = "send"
                    part += 1
                    total = (stats["total"] + 9) // 10
                    caption = (
                        f"{Path(document.file_name).stem[:200]}\n"
                        f"Bagian {part}/{total}"
                    )
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
                                caption=caption if i == 0 else None,
                            )
                            for i, photo in enumerate(batch)
                        ]

                        await telegram_call(
                            context.bot.send_media_group,
                            media=media,
                            **options,
                        )
                        del media

                    sent += len(batch)
                    is_group = message.chat.type in (
                        "group", "supergroup"
                    )
                    await asyncio.sleep(
                        len(batch) * 3.5 if is_group else 1
                    )
                    del batch

            finally:
                await worker_call(iterator.close)

        result = SUCCESS_TEXT.replace("{jumlah}", str(sent))

        if stats.get("skipped"):
            result += (
                f"\n{sent} foto terkirim; {stats['skipped']} gambar "
                "rusak/tidak sesuai batas foto Telegram dilewati."
            )

        if not sent:
            result = (
                "Arsip selesai dibaca, tetapi tidak ada gambar valid "
                "yang bisa dikirim sebagai foto Telegram."
            )

        await telegram_call(
            message.reply_text,
            text=result,
        )

    except Exception as error:
        logger.error(
            "Proses arsip terhenti: tahap=%s "
            "foto_terkonfirmasi=%d jenis=%s",
            phase,
            sent,
            type(error).__name__,
        )

        if isinstance(error, ArchiveError):
            reason = str(error)
        elif isinstance(error, MemoryError):
            reason = (
                "RAM server tidak cukup untuk gambar ini. "
                "Tambah kapasitas server."
            )
        elif isinstance(error, RetryAfter):
            reason = (
                "Telegram masih membatasi pengiriman setelah "
                "beberapa percobaan. Coba lagi nanti."
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
                "Koneksi ke Telegram bermasalah "
                "atau melewati batas waktu."
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
                f"{label} tidak dapat diproses. "
                "Pastikan arsip lengkap, tidak berpassword, "
                "dan berisi JPG/JPEG, PNG, atau WEBP yang valid."
            )
        elif phase == "download":
            reason = (
                "Arsip gagal diunduh dari Telegram. "
                "Periksa koneksi dan ruang server."
            )
        else:
            reason = (
                "Terjadi kesalahan saat menyiapkan pengiriman foto."
            )

        try:
            await telegram_call(
                message.reply_text,
                text=(
                    f"Proses terhenti. {sent} foto terkonfirmasi "
                    f"terkirim. {reason} Jika /proses diulang, "
                    "pengiriman mulai dari awal dan foto "
                    "bisa terkirim dua kali."
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diatur.")

    builder = Application.builder().token(token)

    if local_enabled():
        endpoint = f"http://127.0.0.1:{local_port()}"
        builder = (
            builder.base_url(endpoint + "/bot")
            .base_file_url(endpoint + "/file/bot")
            .local_mode(True)
            .connect_timeout(15)
            .read_timeout(1800)
            .write_timeout(1800)
            .media_write_timeout(1800)
        )

    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("proses", process))
    app.run_polling()


if __name__ == "__main__":
    main()
