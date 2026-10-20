from flask import Flask
import threading

app = Flask('')

@app.route('/')
def home():
    return "Zena Bot Online!"

def run():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run).start()
# -*- coding: utf-8 -*-
"""ZENA Crypto Control Center — panel Telegram untuk tiga scanner crypto."""

import html
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

# Token dan chat ID disertakan sesuai permintaan.
# Environment variable dapat digunakan untuk mengganti konfigurasi tanpa mengedit file.
TELEGRAM_BOT_TOKEN = os.getenv(
    "ZENA_TELEGRAM_BOT_TOKEN",
    "8979917342:AAETt_7wWXO_TzzYPVTLftW5-0W8MBlto2I",
).strip()
TELEGRAM_CHAT_ID = os.getenv("ZENA_TELEGRAM_CHAT_ID", "8049352232").strip()
ALLOWED_USER_ID = int(os.getenv("ZENA_ALLOWED_USER_ID", "8049352232"))

ROOT = Path(__file__).resolve().parent
DIVIDER = "━━━━━━━━━━━━━━━━━━━━"

# Nama file harus sama dengan tiga file crypto yang disediakan.
# Early Swing memakai --once agar selesai setelah satu scan dan kontrol bisa kembali.
SCRIPT_MAP = {
    "run_channel": {
        "file": "ZENA_PARALLEL_CHANNEL_CRYPTO.py",
        "title": "Parallel Channel Crypto",
        "description": "Analisis channel, bounce, dan breakout",
        "args": ["--mode", "swing"],
    },
    "run_swing": {
        "file": "ZENA_EARLY_SWING (Crypto).py",
        "title": "Early Swing / Before Pump",
        "description": "Mencari kandidat momentum tahap awal",
        "args": ["--once"],
    },
    "run_cycle": {
        "file": "ZENA_INVEST CYCLE_(Crypto).py",
        "title": "Market Cycle & Invest",
        "description": "Screening investasi dan siklus pasar",
        "args": ["--mode", "full"],
    },
}

ACTIVE_PROCESSES = {}   # task_id -> subprocess.Popen
PENDING_TASKS = set()   # task yang sudah diminta tetapi proses belum terdaftar
CANCEL_REQUESTED = set()
TASK_TITLES = {}
PROCESS_LOCK = threading.Lock()


def get_home_text():
    return (
        "📈 <b>ZENA | CRYPTO CONTROL CENTER</b>\n"
        f"{DIVIDER}\n\n"
        "Selamat datang di panel analisis aset crypto.\n"
        "Pilih modul yang ingin dijalankan:\n\n"
        "📡 <b>Parallel Channel</b>\n"
        "   <i>Analisis channel, pantulan, dan breakout.</i>\n\n"
        "⚡ <b>Early Swing / Before Pump</b>\n"
        "   <i>Screening momentum tahap awal.</i>\n\n"
        "🏦 <b>Market Cycle &amp; Invest</b>\n"
        "   <i>Analisis siklus pasar untuk investasi.</i>\n\n"
        f"{DIVIDER}\n"
        "💡 Tekan salah satu tombol di bawah untuk memulai.\n"
        "🛡️ Satu scanner dijalankan pada satu waktu agar proses tidak bertabrakan.\n\n"
        "<i>Panel ini hanya menjalankan analisis; bukan auto-trading.</i>"
    )


def get_home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡  1 · Parallel Channel", callback_data="run_channel")],
        [InlineKeyboardButton("⚡  2 · Early Swing / Before Pump", callback_data="run_swing")],
        [InlineKeyboardButton("🏦  3 · Market Cycle & Invest", callback_data="run_cycle")],
        [InlineKeyboardButton("🔌  Cek Koneksi Telegram", callback_data="test_telegram")],
    ])


def get_task_keyboard(task_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑  Batalkan proses", callback_data=f"cancel_{task_id}")],
        [InlineKeyboardButton("🏠  Menu Utama", callback_data="back_home")],
    ])


def get_home_only_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠  Menu Utama", callback_data="back_home")]
    ])


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ALLOWED_USER_ID)


async def show_home(update: Update):
    text = get_home_text()
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=get_home_keyboard(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(
                text,
                reply_markup=get_home_keyboard(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await query.message.reply_text(
                text,
                reply_markup=get_home_keyboard(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Akses tidak diizinkan untuk akun ini.")
        return
    await show_home(update)


def find_current_task():
    """Kembalikan task aktif/menunggu, dipanggil saat PROCESS_LOCK sedang dipegang."""
    if ACTIVE_PROCESSES:
        task_id = next(iter(ACTIVE_PROCESSES))
        return task_id, TASK_TITLES.get(task_id, "Scanner ZENA")
    if PENDING_TASKS:
        task_id = next(iter(PENDING_TASKS))
        return task_id, TASK_TITLES.get(task_id, "Scanner ZENA")
    return None, None


def send_final_status(chat_id, status_text):
    """Kirim status final dengan format Telegram HTML dan tombol menu utama."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"{status_text}\n\n{DIVIDER}\nPilih <b>Menu Utama</b> untuk menjalankan modul lain.",
        "reply_markup": {
            "inline_keyboard": [[{"text": "🏠  Menu Utama", "callback_data": "back_home"}]]
        },
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        result = response.json()
        if result.get("ok") is not True:
            print(f"Telegram menolak notifikasi akhir: {result}")
    except Exception as exc:
        print(f"Gagal mengirim notifikasi akhir ke Telegram: {exc}")


def run_script_process(task_id, script_info, chat_id):
    """Jalankan skrip scanner di thread terpisah dan pantau status akhirnya."""
    script_path = ROOT / script_info["file"]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["ZENA_TELEGRAM_BOT_TOKEN"] = TELEGRAM_BOT_TOKEN
    env["ZENA_TELEGRAM_CHAT_ID"] = TELEGRAM_CHAT_ID

    proc = None
    stdout = ""
    stderr = ""
    try:
        command = [sys.executable, str(script_path), *script_info["args"]]
        proc = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        with PROCESS_LOCK:
            ACTIVE_PROCESSES[task_id] = proc
            PENDING_TASKS.discard(task_id)
            cancel_early = task_id in CANCEL_REQUESTED

        # Tetap menangani tombol batal yang ditekan sebelum Popen terdaftar.
        if cancel_early:
            try:
                proc.terminate()
            except Exception:
                pass

        stdout, stderr = proc.communicate()
        return_code = proc.returncode

        with PROCESS_LOCK:
            ACTIVE_PROCESSES.pop(task_id, None)
            PENDING_TASKS.discard(task_id)
            was_cancelled = task_id in CANCEL_REQUESTED
            CANCEL_REQUESTED.discard(task_id)
            TASK_TITLES.pop(task_id, None)

        safe_title = html.escape(script_info["title"])
        safe_file = html.escape(script_info["file"])

        if was_cancelled or return_code in (130, -15, -9):
            status_text = (
                "🛑 <b>PROSES DIBATALKAN</b>\n"
                f"{DIVIDER}\n"
                f"📌 Modul: <b>{safe_title}</b>\n"
                "Proses pemindaian telah dihentikan."
            )
        elif return_code == 0:
            status_text = (
                "✅ <b>PROSES SELESAI</b>\n"
                f"{DIVIDER}\n"
                f"📌 Modul: <b>{safe_title}</b>\n"
                f"📄 Skrip: <code>{safe_file}</code>\n\n"
                "Proses selesai tanpa exit error. Laporan scanner dikirim terpisah oleh modul terkait."
            )
        else:
            status_text = (
                "⚠️ <b>PROSES SELESAI DENGAN ERROR</b>\n"
                f"{DIVIDER}\n"
                f"📌 Modul: <b>{safe_title}</b>\n"
                f"📄 Skrip: <code>{safe_file}</code>\n"
                f"🔢 Kode keluar: <code>{return_code}</code>"
            )
            if stderr and stderr.strip():
                status_text += "\n\n<b>Cuplikan error:</b>\n<code>" + html.escape(stderr.strip()[-1000:]) + "</code>"
            elif stdout and stdout.strip():
                status_text += "\n\n<b>Cuplikan output:</b>\n<code>" + html.escape(stdout.strip()[-700:]) + "</code>"

    except Exception as exc:
        with PROCESS_LOCK:
            ACTIVE_PROCESSES.pop(task_id, None)
            PENDING_TASKS.discard(task_id)
            was_cancelled = task_id in CANCEL_REQUESTED
            CANCEL_REQUESTED.discard(task_id)
            TASK_TITLES.pop(task_id, None)
        if was_cancelled:
            status_text = "🛑 <b>PROSES DIBATALKAN</b>\nPermintaan pembatalan telah diproses."
        else:
            status_text = (
                "❌ <b>TIDAK DAPAT MENJALANKAN SKRIP</b>\n"
                f"📄 Skrip: <code>{html.escape(script_info['file'])}</code>\n\n"
                f"<code>{html.escape(str(exc)[-1000:])}</code>"
            )

    send_final_status(chat_id, status_text)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(update):
        try:
            await query.answer("Akses tidak diizinkan.", show_alert=True)
        except Exception:
            pass
        return

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""
    chat_id = query.message.chat_id

    if data == "back_home":
        await show_home(update)
        return

    if data == "test_telegram":
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=10
            )
            response.raise_for_status()
            result = response.json()
            if result.get("ok"):
                bot_info = result.get("result", {})
                username = html.escape(bot_info.get("username", "(username tidak tersedia)"))
                text = (
                    "🟢 <b>KONEKSI TELEGRAM BERHASIL</b>\n"
                    f"{DIVIDER}\n"
                    f"🤖 Bot: <b>@{username}</b>\n"
                    f"🆔 Chat ID notifikasi: <code>{html.escape(TELEGRAM_CHAT_ID)}</code>"
                )
            else:
                text = "🔴 <b>Koneksi gagal.</b> Telegram tidak menerima token bot."
        except Exception as exc:
            text = "🔴 <b>KONEKSI GAGAL</b>\n" + html.escape(str(exc)[:700])
        await query.edit_message_text(
            text, reply_markup=get_home_only_keyboard(), parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if data.startswith("cancel_"):
        task_id = data[len("cancel_"):]
        with PROCESS_LOCK:
            proc = ACTIVE_PROCESSES.get(task_id)
            is_pending = task_id in PENDING_TASKS
            task_title = TASK_TITLES.get(task_id, "Scanner ZENA")
            if proc is not None or is_pending:
                CANCEL_REQUESTED.add(task_id)

        if proc is not None:
            try:
                proc.terminate()
                cancel_text = (
                    "🛑 <b>PERMINTAAN PEMBATALAN DIKIRIM</b>\n"
                    f"{DIVIDER}\nModul <b>{html.escape(task_title)}</b> sedang dihentikan."
                )
            except Exception as exc:
                cancel_text = "⚠️ Gagal menghentikan proses: <code>" + html.escape(str(exc)) + "</code>"
        elif is_pending:
            cancel_text = (
                "🛑 <b>PEMBATALAN DITERIMA</b>\n"
                f"{DIVIDER}\nModul <b>{html.escape(task_title)}</b> akan dihentikan saat proses mulai."
            )
        else:
            cancel_text = "ℹ️ Proses tersebut sudah selesai atau tidak ditemukan."

        await query.edit_message_text(
            cancel_text, reply_markup=get_home_only_keyboard(), parse_mode="HTML"
        )
        return

    if data not in SCRIPT_MAP:
        await query.edit_message_text(
            "⚠️ Perintah tidak dikenali.", reply_markup=get_home_only_keyboard()
        )
        return

    script_info = SCRIPT_MAP[data]
    script_path = ROOT / script_info["file"]
    if not script_path.is_file():
        await query.edit_message_text(
            "❌ <b>FILE SKRIP TIDAK DITEMUKAN</b>\n"
            f"{DIVIDER}\n"
            f"Pastikan file ini berada satu folder dengan main: <code>{html.escape(script_info['file'])}</code>",
            reply_markup=get_home_only_keyboard(),
            parse_mode="HTML",
        )
        return

    task_id = f"{chat_id}_{uuid.uuid4().hex[:8]}"
    with PROCESS_LOCK:
        current_id, current_title = find_current_task()
        if current_id is None:
            PENDING_TASKS.add(task_id)
            TASK_TITLES[task_id] = script_info["title"]
            accepted = True
        else:
            accepted = False

    if not accepted:
        await query.edit_message_text(
            "⏳ <b>SCANNER MASIH BERJALAN</b>\n"
            f"{DIVIDER}\n"
            f"Modul aktif: <b>{html.escape(current_title)}</b>\n\n"
            "Tunggu hingga selesai atau batalkan proses yang sedang berjalan.",
            reply_markup=get_task_keyboard(current_id),
            parse_mode="HTML",
        )
        return

    processing_text = (
        "🔄 <b>SCANNER SEDANG BERJALAN</b>\n"
        f"{DIVIDER}\n"
        f"📌 Modul: <b>{html.escape(script_info['title'])}</b>\n"
        f"📝 {html.escape(script_info['description'])}\n\n"
        "Hasil analisis akan dikirim oleh modul scanner ke chat Telegram.\n"
        "Anda dapat membatalkan proses melalui tombol di bawah."
    )
    await query.edit_message_text(
        processing_text,
        reply_markup=get_task_keyboard(task_id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    worker = threading.Thread(
        target=run_script_process,
        args=(task_id, script_info, chat_id),
        daemon=True,
        name=f"zena-{data}-{int(time.time())}",
    )
    worker.start()


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: ZENA_TELEGRAM_BOT_TOKEN belum diatur.")
        raise SystemExit(1)

    print("Menjalankan ZENA Crypto Control Center...")
    print(f"Folder skrip: {ROOT}")
    print(f"Akses dibatasi untuk Telegram user ID: {ALLOWED_USER_ID}")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", start_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Bot siap. Buka Telegram lalu kirim /start.")
    app.run_polling()


if __name__ == "__main__":
    main()
