import os
import sys
import json
import time
import subprocess
import importlib

BOT_API_LIMIT = 50 * 1024 * 1024  # HTTP Bot API upload limit
HOME = os.path.expanduser("~")
CONFIG = os.path.join(HOME, ".tg_sender.json")
SESSION = os.path.join(HOME, ".tg_sender_session")


def ensure(import_name, pip_name=None, required=True):
    """Silently install a library if missing, then import it."""
    pip_name = pip_name or import_name
    try:
        return importlib.import_module(import_name)
    except ImportError:
        pass

    print("Loading, please wait...", end="", flush=True)
    base = [sys.executable, "-m", "pip", "install", "--quiet",
            "--disable-pip-version-check", pip_name]
    for extra in ([], ["--break-system-packages"], ["--user"]):
        res = subprocess.run(base + extra, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        if res.returncode == 0:
            break
    importlib.invalidate_caches()
    print("\r" + " " * 60 + "\r", end="", flush=True)

    try:
        return importlib.import_module(import_name)
    except ImportError:
        if required:
            print(f"Failed to install {pip_name}. Please check your internet connection.")
            sys.exit(1)
        return None


requests = ensure("requests")


def detect_chat_id(token):
    """Detect the chat ID automatically. Waits until a message reaches the bot."""
    api = f"https://api.telegram.org/bot{token}"
    me = requests.get(f"{api}/getMe", timeout=30).json()
    if not me.get("ok"):
        print("Invalid token:", me.get("description"))
        sys.exit(1)
    requests.get(f"{api}/deleteWebhook", timeout=30)

    announced = False
    while True:
        r = requests.get(f"{api}/getUpdates", timeout=30).json()
        for u in reversed(r.get("result", [])):
            msg = u.get("message") or u.get("channel_post")
            if msg:
                return msg["chat"]["id"]
        if not announced:
            print(f"Open @{me['result']['username']} in Telegram and send /start (waiting)...")
            announced = True
        time.sleep(2)


def report(size, started):
    secs = max(time.time() - started, 0.001)
    print(f"Sent successfully! Average speed: {size * 8 / secs / 1_000_000:.1f} Mbps")


def send_small(token, chat_id, path):
    """Up to 50MB: sendDocument keeps the original quality (no compression)."""
    size = os.path.getsize(path)
    session = requests.Session()
    started = time.time()
    with open(path, "rb") as f:
        r = session.post(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data={"chat_id": chat_id, "disable_content_type_detection": "true"},
            files={"document": (os.path.basename(path), f)},
            timeout=None,
        )
    res = r.json()
    if res.get("ok"):
        report(size, started)
    else:
        print("Failed:", res.get("description"))


def load_api_credentials():
    """API ID / Hash are asked only once, then saved."""
    if os.path.isfile(CONFIG):
        try:
            with open(CONFIG) as f:
                d = json.load(f)
            return int(d["api_id"]), d["api_hash"]
        except Exception:
            pass
    print("\nFile is larger than 50MB. One-time setup needed.")
    print("Get API ID and API Hash at: https://my.telegram.org -> API development tools")
    api_id = int(input("API ID: ").strip())
    api_hash = input("API Hash: ").strip()
    with open(CONFIG, "w") as f:
        json.dump({"api_id": api_id, "api_hash": api_hash}, f)
    return api_id, api_hash


def send_large(token, chat_id, path):
    """Above 50MB: send via MTProto (Telethon), up to 2GB."""
    ensure("cryptg", required=False)  # much faster encryption when available
    telethon = ensure("telethon")
    TelegramClient, events = telethon.TelegramClient, telethon.events

    api_id, api_hash = load_api_credentials()
    size = os.path.getsize(path)
    client = TelegramClient(SESSION, api_id, api_hash)

    def progress(done, total):
        print(f"\r{done * 100 // total}%", end="", flush=True)

    async def upload(target):
        started = time.time()
        await client.send_file(target, path, force_document=True,
                               part_size_kb=512, progress_callback=progress)
        print()
        report(size, started)

    async def run():
        await client.start(bot_token=token)
        try:
            await upload(chat_id)
            await client.disconnect()
            return
        except ValueError:
            pass  # chat not cached in this session yet, wait for one incoming message

        print("Send any message to your bot once to continue (waiting)...")

        @client.on(events.NewMessage(incoming=True))
        async def handler(event):
            await upload(event.chat_id)
            await client.disconnect()

        await client.run_until_disconnected()

    client.loop.run_until_complete(run())


def main():
    path = input("Image / Video path: ").strip().strip('"').strip("'")
    if not os.path.isfile(path):
        print("File not found:", path)
        return
    token = input("Telegram Bot Token: ").strip()

    chat_id = detect_chat_id(token)
    print("Sending...")
    if os.path.getsize(path) <= BOT_API_LIMIT:
        send_small(token, chat_id, path)
    else:
        send_large(token, chat_id, path)


if __name__ == "__main__":
    main()
