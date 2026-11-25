"""
All-in-one file transfer (any file type, files or folders)

  1) Wi-Fi send     : fast direct transfer on same network (no internet)
  2) Wi-Fi receive  : run this on the receiving device
  3) Telegram send  : anywhere in the world (up to 2GB)

Secrets (bot token, API hash) are never hardcoded. They are asked at run time.
Only API ID / API Hash are saved locally in ~/.tg_sender.json (Telegram >50MB).
"""
import getpass
import importlib
import json
import os
import socket
import struct
import subprocess
import sys
import time

PORT = 5001
CHUNK = 1024 * 1024                  # 1 MB
SAVE_DIR = "received"
BOT_API_LIMIT = 50 * 1024 * 1024     # Bot API upload limit
HOME = os.path.expanduser("~")
CONFIG = os.path.join(HOME, ".tg_sender.json")
SESSION = os.path.join(HOME, ".tg_sender_session")


# ---------------------------------------------------------------- helpers
def ensure(import_name, pip_name=None, required=True):
    """Install a library silently if missing, then import it."""
    pip_name = pip_name or import_name
    try:
        return importlib.import_module(import_name)
    except ImportError:
        pass
    print(f"Installing {pip_name}...", end="", flush=True)
    base = [sys.executable, "-m", "pip", "install", "--quiet",
            "--disable-pip-version-check", pip_name]
    for extra in ([], ["--break-system-packages"], ["--user"]):
        res = subprocess.run(base + extra, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        if res.returncode == 0:
            break
    importlib.invalidate_caches()
    print("\r" + " " * 50 + "\r", end="", flush=True)
    try:
        return importlib.import_module(import_name)
    except ImportError:
        if required:
            print(f"Could not install {pip_name}. Check internet connection.")
            sys.exit(1)
        return None


def collect(path):
    """Return list of files for a file or a folder path."""
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        out = []
        for root, _, names in os.walk(path):
            out += [os.path.join(root, n) for n in names]
        return out
    return []


def ask_path():
    p = input("File or folder path: ").strip().strip('"').strip("'")
    files = collect(p)
    if not files:
        print("Nothing found at:", p)
    return files


def show(name, done, total, start):
    elapsed = max(time.time() - start, 0.001)
    mbs = done / elapsed / 1024 / 1024
    pct = done * 100 // total if total else 100
    print(f"\r{name[:28]:28} {pct:3d}%  {mbs:6.1f} MB/s ({mbs * 8:5.0f} Mbps)",
          end="", flush=True)


def my_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))       # no data is actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- Wi-Fi mode
def recv_exact(conn, n):
    buf = bytearray()
    while len(buf) < n:
        part = conn.recv(n - len(buf))
        if not part:
            raise ConnectionError("Connection closed")
        buf += part
    return bytes(buf)


def wifi_receive():
    os.makedirs(SAVE_DIR, exist_ok=True)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(1)
    print(f"Ready. Give this IP to the sender: {my_ip()}   (Ctrl+C to stop)")
    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        print(f"\nConnected: {addr[0]}")
        try:
            while True:
                (nlen,) = struct.unpack("!H", recv_exact(conn, 2))
                if nlen == 0:
                    break
                name = os.path.basename(recv_exact(conn, nlen).decode())
                (size,) = struct.unpack("!Q", recv_exact(conn, 8))
                got, start = 0, time.time()
                with open(os.path.join(SAVE_DIR, name), "wb") as f:
                    while got < size:
                        data = conn.recv(min(CHUNK, size - got))
                        if not data:
                            raise ConnectionError("Connection lost")
                        f.write(data)
                        got += len(data)
                        show(name, got, size, start)
                print()
            print(f"Done. Saved in ./{SAVE_DIR}")
        except Exception as e:
            print(f"\nError: {e}")
        finally:
            conn.close()


def wifi_send():
    files = ask_path()
    if not files:
        return False
    ip = input("Receiver IP: ").strip()
    try:
        sock = socket.create_connection((ip, PORT), timeout=10)
    except OSError as e:
        print("Could not connect:", e)
        return False
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    sock.settimeout(None)
    for path in files:
        name = os.path.basename(path)
        size = os.path.getsize(path)
        nb = name.encode()
        sock.sendall(struct.pack("!H", len(nb)) + nb + struct.pack("!Q", size))
        sent, start = 0, time.time()
        with open(path, "rb") as f:
            while True:
                data = f.read(CHUNK)
                if not data:
                    break
                sock.sendall(data)
                sent += len(data)
                show(name, sent, size, start)
        print()
    sock.sendall(struct.pack("!H", 0))
    sock.close()
    print("All files sent.")
    return True


# ---------------------------------------------------------------- Telegram mode
import hashlib
from concurrent.futures import ThreadPoolExecutor


def cfg_load():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def cfg_save(d):
    with open(CONFIG, "w") as f:
        json.dump(d, f)
    try:
        os.chmod(CONFIG, 0o600)          # owner-only access
    except OSError:
        pass


def token_key(token):
    """Only a hash of the token is used as key; the token itself is never saved."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def tg_get_chat_id(requests, token):
    """Instant on repeat runs (chat id cached). First run: finds it quickly."""
    cfg = cfg_load()
    key = token_key(token)
    cached = cfg.get("chats", {}).get(key)
    if cached:
        print("Chat found (saved). Sending right away...")
        return cached

    api = f"https://api.telegram.org/bot{token}"
    print("Checking token...", flush=True)
    try:
        me = requests.get(f"{api}/getMe", timeout=15).json()
    except requests.RequestException as e:
        print("Cannot reach Telegram (check internet / VPN):", e)
        sys.exit(1)
    if not me.get("ok"):
        print("Invalid token:", me.get("description"))
        sys.exit(1)
    try:
        requests.get(f"{api}/deleteWebhook", timeout=15)
    except requests.RequestException:
        pass

    chat_id = None
    print("Looking for your chat...", flush=True)
    r = requests.get(f"{api}/getUpdates", timeout=15).json()
    chat_id = _chat_from_updates(r)

    if chat_id is None:
        print(f"No chat yet. Open @{me['result']['username']} in Telegram and send /start")
        typed = input("...or type your Chat ID now (Enter = wait for /start): ").strip()
        if typed.lstrip("-").isdigit():
            chat_id = int(typed)
        while chat_id is None:
            # long polling: returns the moment a message arrives (no sleeping)
            r = requests.get(f"{api}/getUpdates", params={"timeout": 20}, timeout=40).json()
            chat_id = _chat_from_updates(r)

    cfg.setdefault("chats", {})[key] = chat_id
    cfg_save(cfg)
    return chat_id


def _chat_from_updates(r):
    for u in reversed(r.get("result", [])):
        msg = u.get("message") or u.get("channel_post")
        if msg:
            return msg["chat"]["id"]
    return None


def tg_report(size, started):
    secs = max(time.time() - started, 0.001)
    print(f"Sent. Average speed: {size / secs / 1024 / 1024:.1f} MB/s "
          f"({size * 8 / secs / 1_000_000:.0f} Mbps)")


def tg_send_small(requests, token, chat_id, path, quiet=False):
    """Up to 50MB via Bot API, original quality, with live progress."""
    name = os.path.basename(path)
    size = os.path.getsize(path)
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    tb = ensure("requests_toolbelt", required=False)
    started = time.time()
    try:
        if tb:
            from requests_toolbelt.multipart.encoder import (
                MultipartEncoder, MultipartEncoderMonitor)
            with open(path, "rb") as f:
                enc = MultipartEncoder(fields={
                    "chat_id": str(chat_id),
                    "disable_content_type_detection": "true",
                    "document": (name, f),
                })
                mon = MultipartEncoderMonitor(
                    enc,
                    lambda m: None if quiet else show(name, m.bytes_read, enc.len, started))
                r = requests.post(url, data=mon,
                                  headers={"Content-Type": mon.content_type},
                                  timeout=None)
            if not quiet:
                print()
        else:
            with open(path, "rb") as f:
                r = requests.post(
                    url,
                    data={"chat_id": chat_id, "disable_content_type_detection": "true"},
                    files={"document": (name, f)}, timeout=None)
        res = r.json()
    except Exception as e:
        print(f"{name}: failed ({e})")
        return
    if res.get("ok"):
        if quiet:
            print(f"{name}: ", end="")
        tg_report(size, started)
    else:
        print(f"{name}: failed - {res.get('description')}")


def tg_credentials():
    cfg = cfg_load()
    if "api_id" in cfg and "api_hash" in cfg:
        return int(cfg["api_id"]), cfg["api_hash"]
    print("\nFiles over 50MB need one-time setup.")
    print("Get API ID / Hash: https://my.telegram.org -> API development tools")
    api_id = int(input("API ID: ").strip())
    api_hash = getpass.getpass("API Hash (hidden): ").strip()
    cfg["api_id"], cfg["api_hash"] = api_id, api_hash
    cfg_save(cfg)
    return api_id, api_hash


def tg_send_large(token, chat_id, paths):
    ensure("cryptg", required=False)     # much faster encryption when available
    telethon = ensure("telethon")
    TelegramClient, events = telethon.TelegramClient, telethon.events
    api_id, api_hash = tg_credentials()
    client = TelegramClient(SESSION, api_id, api_hash, connection_retries=5)

    def make_progress():
        start = time.time()

        def cb(done, total):
            show("uploading", done, total, start)
        return cb

    async def upload_all(target):
        for p in paths:
            started = time.time()
            await client.send_file(target, p, force_document=True,
                                   part_size_kb=512,
                                   progress_callback=make_progress())
            print()
            tg_report(os.path.getsize(p), started)

    async def run():
        await client.start(bot_token=token)
        try:
            await upload_all(chat_id)
            await client.disconnect()
            return
        except ValueError:
            pass                         # first time only: chat not cached in session
        print("One-time step: send any message to your bot (waiting)...")

        @client.on(events.NewMessage(incoming=True))
        async def handler(event):
            await upload_all(event.chat_id)
            await client.disconnect()

        await client.run_until_disconnected()

    client.loop.run_until_complete(run())


def telegram_send():
    requests = ensure("requests")
    files = ask_path()
    if not files:
        return
    token = getpass.getpass("Telegram Bot Token (hidden): ").strip()
    chat_id = tg_get_chat_id(requests, token)
    small = [p for p in files if os.path.getsize(p) <= BOT_API_LIMIT]
    large = [p for p in files if os.path.getsize(p) > BOT_API_LIMIT]
    print(f"Sending {len(files)} file(s)...")
    if len(small) == 1:
        tg_send_small(requests, token, chat_id, small[0])
    elif small:
        # several small files upload in parallel for more total speed
        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(lambda p: tg_send_small(requests, token, chat_id, p, quiet=True),
                        small))
    if large:
        tg_send_large(token, chat_id, large)


# ---------------------------------------------------------------- menu
def main():
    print("1) Wi-Fi send     (fast, same network)")
    print("2) Wi-Fi receive")
    print("3) Telegram send  (anywhere, needs internet)")
    choice = input("Choose 1/2/3: ").strip()
    try:
        if choice == "1":
            ok = wifi_send()
            if not ok and input("Send via Telegram instead? (y/n): ").strip().lower() == "y":
                telegram_send()
        elif choice == "2":
            wifi_receive()
        elif choice == "3":
            telegram_send()
        else:
            print("Invalid choice")
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
