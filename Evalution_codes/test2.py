import os
import sys
import subprocess
import importlib

BOT_API_LIMIT = 50 * 1024 * 1024  # Telegram Bot API (HTTP) limit


def ensure(import_name, pip_name=None):
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
    print("\r" + " " * 60 + "\r", end="", flush=True)  # clear the loading line

    try:
        return importlib.import_module(import_name)
    except ImportError:
        print(f"Failed to install {pip_name}. Please check your internet connection.")
        sys.exit(1)


requests = ensure("requests")


def get_chat_id(token):
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    if not r.get("ok"):
        print("Invalid token:", r.get("description"))
        sys.exit(1)
    for u in reversed(r["result"]):
        msg = u.get("message") or u.get("channel_post")
        if msg:
            return msg["chat"]["id"]
    print("Please send /start to your bot in Telegram first.")
    sys.exit(1)


def send_small(token, chat_id, path):
    """Up to 50MB: sendDocument keeps the original quality (no compression)."""
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with open(path, "rb") as f:
        r = requests.post(
            url,
            data={"chat_id": chat_id, "disable_content_type_detection": "true"},
            files={"document": (os.path.basename(path), f)},
            timeout=None,
        )
    res = r.json()
    print("Sent successfully!" if res.get("ok") else f"Failed: {res.get('description')}")


def send_large(token, path):
    """Above 50MB: send via MTProto (Telethon), up to 2GB."""
    telethon = ensure("telethon")
    TelegramClient, events = telethon.TelegramClient, telethon.events

    print("\nFile is larger than 50MB, so API ID and API Hash are required.")
    print("Get them at: https://my.telegram.org -> API development tools")
    api_id = int(input("API ID: ").strip())
    api_hash = input("API Hash: ").strip()

    client = TelegramClient("bot_session", api_id, api_hash)

    async def run():
        await client.start(bot_token=token)
        print("Now send any message to your bot in Telegram...")

        @client.on(events.NewMessage(incoming=True))
        async def handler(event):
            await client.send_file(
                event.chat_id,
                path,
                force_document=True,  # no compression
                progress_callback=lambda c, t: print(f"\r{c * 100 // t}%", end="", flush=True),
            )
            print("\nSent successfully!")
            await client.disconnect()

        await client.run_until_disconnected()

    client.loop.run_until_complete(run())


def main():
    path = input("Image / Video path: ").strip().strip('"').strip("'")
    if not os.path.isfile(path):
        print("File not found:", path)
        return
    token = input("Telegram Bot Token: ").strip()

    if os.path.getsize(path) <= BOT_API_LIMIT:
        chat_id = input("Chat ID (press Enter if unknown): ").strip() or get_chat_id(token)
        print("Sending...")
        send_small(token, chat_id, path)
    else:
        send_large(token, path)


if __name__ == "__main__":
    main()
