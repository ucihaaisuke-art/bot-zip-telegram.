"""Jalankan Bot API lokal dan bot ekstrak arsip pada satu container."""

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class StartupError(Exception):
    pass


def settings():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()

    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise StartupError(
            "Isi TELEGRAM_BOT_TOKEN untuk bot arsip "
            "pada environment layanan."
        )

    if (
        not api_id.isdecimal()
        or int(api_id) <= 0
        or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash)
    ):
        raise StartupError(
            "Isi TELEGRAM_API_ID dan TELEGRAM_API_HASH "
            "dari my.telegram.org pada environment layanan."
        )

    os.environ["TELEGRAM_BOT_TOKEN"] = token
    os.environ["TELEGRAM_API_ID"] = api_id
    os.environ["TELEGRAM_API_HASH"] = api_hash
    os.environ["ARSIP_LOCAL_BOT_API"] = "on"

    from bot import api_directory, local_port, rar_library

    rar_library()
    port = local_port()
    public = int(os.environ.get("PORT", "8080"))

    if not 1 <= public <= 65535 or port == public:
        raise StartupError(
            "PORT dan ARSIP_API_PORT harus valid dan berbeda."
        )

    binary = shutil.which("telegram-bot-api")

    if not binary:
        raise StartupError(
            "Bot API lokal belum terpasang. "
            "Deploy dengan Dockerfile bot arsip."
        )

    directory = api_directory().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)

    temporary = Path(
        os.environ.get("ARSIP_WORK_DIR", "/data/arsip-work")
    ).resolve()
    temporary.mkdir(parents=True, exist_ok=True, mode=0o700)

    os.environ["ARSIP_API_DIR"] = str(directory)
    os.environ["ARSIP_WORK_DIR"] = str(temporary)

    return token, port, public, binary, directory, temporary


def redact(value):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_API_HASH"):
        secret = os.environ.get(name, "")
        if secret:
            value = value.replace(secret, "[rahasia]")

    return re.sub(
        r"\b[0-9]{5,}:[A-Za-z0-9_-]{20,}",
        "[token]",
        value,
    )


def logs(stream):
    for line in stream:
        logger.warning(
            "Bot API: %s",
            redact(line.rstrip())[:1000],
        )


def clean_archives(directory):
    """Bersihkan cache arsip sebelum server native dijalankan."""
    removed = 0

    for path in directory.rglob("*"):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.suffix.lower() not in (".zip", ".rar")
        ):
            continue

        if not path.resolve().is_relative_to(directory):
            continue

        with path.open("rb") as stream:
            header = stream.read(8)

        if header.startswith(
            (b"PK\x03\x04", b"PK\x05\x06", b"Rar!\x1a\x07")
        ):
            path.unlink()
            removed += 1

    logger.info(
        "Cache arsip lama dibersihkan saat server API berhenti: %d file.",
        removed,
    )


def wait_port(process, port, stopped):
    deadline = time.monotonic() + 60

    while time.monotonic() < deadline and not stopped.is_set():
        if process.poll() is not None:
            raise StartupError(
                "Bot API lokal berhenti saat startup. "
                "Periksa log layanan."
            )

        try:
            with socket.create_connection(
                ("127.0.0.1", port), timeout=1
            ):
                return
        except OSError:
            stopped.wait(0.25)

    raise StartupError("Bot API lokal belum membuka port.")


def migrate(client, token, port, directory, stopped):
    marker = directory / "migration.json"
    fingerprint = hashlib.sha256(token.encode()).hexdigest()

    try:
        migrated = (
            json.loads(marker.read_text()).get("token_hash")
            == fingerprint
        )
    except (OSError, ValueError, AttributeError):
        migrated = False

    if not migrated:
        logger.info(
            "Memindahkan bot arsip dari Bot API cloud ke Bot API lokal."
        )
        response = client.post(
            f"https://api.telegram.org/bot{token}/logOut",
            timeout=30,
        )
        data = response.json()

        if (
            not isinstance(data, dict)
            or (
                not data.get("ok")
                and response.status_code != 401
            )
        ):
            raise StartupError(
                "Migrasi cloud belum diterima Telegram. "
                "Periksa token bot arsip."
            )

    deadline = time.monotonic() + 90

    while time.monotonic() < deadline and not stopped.is_set():
        try:
            response = client.post(
                f"http://127.0.0.1:{port}/bot{token}/getMe",
                timeout=15,
            )
            data = response.json()

            if (
                isinstance(data, dict)
                and data.get("ok")
                and data.get("result", {}).get("is_bot")
            ):
                temporary = marker.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps({"token_hash": fingerprint})
                )
                os.chmod(temporary, 0o600)
                temporary.replace(marker)
                return

        except (httpx.HTTPError, ValueError, AttributeError):
            pass

        stopped.wait(2)

    raise StartupError(
        "Login Bot API lokal belum berhasil. "
        "Periksa API ID, API hash, token, dan koneksi."
    )


def terminate(process):
    if process is None or process.poll() is not None:
        return

    process.terminate()

    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    stopped = threading.Event()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())

    api = bot = server = None

    try:
        token, port, public, binary, directory, temporary = settings()
        clean_archives(directory)

        api = subprocess.Popen(
            [
                binary,
                "--local",
                "--http-ip-address=127.0.0.1",
                f"--http-port={port}",
                f"--dir={directory}",
                f"--temp-dir={temporary}",
                "--verbosity=0",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        threading.Thread(
            target=logs,
            args=(api.stderr,),
            daemon=True,
        ).start()

        wait_port(api, port, stopped)

        with httpx.Client(
            follow_redirects=False,
            trust_env=False,
        ) as client:
            migrate(client, token, port, directory, stopped)

        bot = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("bot.py")),
            ]
        )

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                healthy = (
                    self.path == "/health"
                    and api.poll() is None
                    and bot.poll() is None
                )
                status = 200 if healthy else 503
                self.send_response(status)
                self.end_headers()
                self.wfile.write(
                    b"OK" if status == 200 else b"Starting"
                )

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(
            ("0.0.0.0", public), Health
        )
        threading.Thread(
            target=server.serve_forever,
            daemon=True,
        ).start()

        logger.info(
            "Bot arsip ZIP/RAR aktif melalui Bot API lokal."
        )

        while not stopped.wait(0.25):
            if api.poll() is not None:
                raise StartupError(
                    "Bot API lokal berhenti; "
                    "layanan perlu dimulai ulang."
                )

            if bot.poll() is not None:
                return bot.returncode

        return 0

    except Exception as error:
        if not stopped.is_set():
            message = (
                str(error)
                if isinstance(error, StartupError)
                else (
                    "Startup bot arsip gagal: "
                    f"{type(error).__name__}. "
                    "Periksa file, volume, dan environment layanan."
                )
            )
            logger.error("%s", message)

        return 0 if stopped.is_set() else 1

    finally:
        if server is not None:
            server.shutdown()
            server.server_close()

        terminate(bot)
        terminate(api)


if __name__ == "__main__":
    raise SystemExit(main())
