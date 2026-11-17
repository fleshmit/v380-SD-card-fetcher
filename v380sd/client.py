"""V380 cloud client: login, SD-card listing, clip download.

Two authentication paths are implemented because firmware differs:

* ``login_device``  — cmd 1167, username = device ID (or "admin"), password = the
  camera's own *device password*. This is what OpenV380 uses and it is the
  cleanest path when it works.
* ``login_account`` — the JSON login the official app performs against the
  message server (port 8089). It needs the app account credentials that the
  app derives per device (``username`` + ``password``) — on some cameras the
  password is the device password, double AES-ECB encrypted with
  ``randomKey`` and the static key ``macrovideo+*#!^@``.

Packet layouts, crypto keys and frame formats are documented in docs/PROTOCOL.md.
Everything here was verified against real captures; do not "tidy" the offsets.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import random
import secrets
import socket
import string
import struct
import time
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

PORT = 8800
LOGIN_HOST, LOGIN_PORT = "47.91.90.154", 8089  # message server used by the app
STATIC_KEY = b"macrovideo+*#!^@"                # hard-coded in every app build

# Frame types
VIDEO_TYPES = (0, 1, 40, 41, 50, 51, 52, 53)
KEYFRAME_TYPES = (0, 40, 50, 52)

# Packet types on the playback socket
PKT_DATA = 0x7F
PKT_ACK_REQUEST = 0x6F
PKT_SEGMENT_END = 0xEF
PKT_IGNORE = 0x1F
PKT_KEEPALIVE = 2000


class V380Error(RuntimeError):
    """Any failure talking to the camera or the cloud."""


@dataclass
class Recording:
    """One entry in the camera's SD-card recording index."""

    segment_id: int
    kind: int          # 1 = continuous, 2 = motion, 3 = alarm
    start: int         # unix seconds, camera wall clock
    end: int

    @property
    def duration(self) -> int:
        return max(0, self.end - self.start)

    @property
    def start_dt(self) -> _dt.datetime:
        return _dt.datetime.fromtimestamp(self.start, _dt.timezone.utc).replace(tzinfo=None)

    @property
    def is_event(self) -> bool:
        return self.kind in (2, 3)

    @property
    def kind_name(self) -> str:
        return {1: "continuous", 2: "motion", 3: "alarm"}.get(self.kind, str(self.kind))


def _clock(buf: bytearray, base: int, when: _dt.datetime) -> None:
    """u16 year followed by five single bytes: month, day, hour, minute, second."""
    struct.pack_into("<H", buf, base, when.year)
    buf[base + 2] = when.month
    buf[base + 3] = when.day
    buf[base + 4] = when.hour
    buf[base + 5] = when.minute
    buf[base + 6] = when.second


def _start_code(buf: bytes) -> Optional[int]:
    """Index of an Annex-B start code, searching the first 16 bytes."""
    for i in range(min(16, max(0, len(buf) - 3))):
        if buf[i] == 0 and buf[i + 1] == 0 and buf[i + 2] == 1:
            return i - 1 if (i > 0 and buf[i - 1] == 0) else i
    return None


class V380Camera:
    """Talk to one camera over the V380 cloud relay."""

    def __init__(self, device_id: int, password: str, *, username: Optional[str] = None,
                 relay: Optional[str] = None, port: int = PORT, timeout: float = 12.0):
        self.device_id = int(device_id)
        self.password = password
        self.username = username or str(device_id)
        self.domain = f"{self.device_id}.nvdvr.net"
        self.port = int(port)
        self.timeout = timeout
        self.relay = relay
        self.ticket: Optional[int] = None
        self.device_version: int = 0
        self._aes: Optional[AES.AES] = None

    # ── transport helpers ────────────────────────────────────────────────
    def _recv(self, sock: socket.socket, count: int, timeout: Optional[float] = None) -> bytes:
        sock.settimeout(timeout or self.timeout)
        out = b""
        while len(out) < count:
            chunk = sock.recv(count - len(out))
            if not chunk:
                raise V380Error("connection closed by peer")
            out += chunk
        return out

    def _connect(self) -> socket.socket:
        if not self.relay:
            raise V380Error("relay address unknown — call login_*() first")
        try:
            return socket.create_connection((self.relay, self.port), timeout=self.timeout)
        except OSError as exc:
            raise V380Error(f"cannot reach relay {self.relay}:{self.port}: {exc}") from exc

    # ── authentication ───────────────────────────────────────────────────
    def _media_key(self) -> bytes:
        key = bytearray(16)
        struct.pack_into("<I", key, 0, (self.ticket or 0) & 0xFFFFFFFF)
        struct.pack_into("<Q", key, 4, 0x618123462C14795C)
        struct.pack_into("<I", key, 12, 0x82800DF0)
        return bytes(key)

    def login_device(self, version: int = 31, encoding: str = "pkcs7") -> None:
        """cmd 1167 — log in with the camera's own device password."""
        if not self.relay:
            raise V380Error("set relay= (see V380Camera.discover_relays) before login")

        random_key = "".join(secrets.choice(string.ascii_letters + string.digits)
                             for _ in range(16)).encode()
        if encoding == "zero48":
            block = (self.password.encode() + bytes(48))[:48]
            inner = AES.new(STATIC_KEY, AES.MODE_ECB).encrypt(block)
            payload = random_key + AES.new(random_key, AES.MODE_ECB).encrypt(inner)
        else:
            inner = AES.new(STATIC_KEY, AES.MODE_ECB).encrypt(pad(self.password.encode(), 16))
            payload = random_key + AES.new(random_key, AES.MODE_ECB).encrypt(inner)[:64]

        cmd = bytearray(520)
        struct.pack_into("<I", cmd, 0, 1167)
        struct.pack_into("<I", cmd, 4, 1022)
        cmd[8] = version
        struct.pack_into("<I", cmd, 9, 1)
        struct.pack_into("<I", cmd, 13, self.device_id)
        cmd[17:17 + len(self.domain)] = self.domain.encode()
        struct.pack_into("<I", cmd, 67, self.port)
        cmd[71:71 + len(self.username)] = self.username.encode()
        cmd[103:103 + len(payload)] = payload

        sock = self._connect()
        try:
            sock.sendall(bytes(cmd))
            resp = self._recv(sock, 256)
        finally:
            sock.close()

        cmd0, result = struct.unpack("<II", resp[:8])
        if cmd0 != 1168:
            raise V380Error(f"unexpected login reply command {cmd0}")
        if result != 1001:
            raise V380Error({
                1011: "wrong username",
                1012: "wrong password",
                1018: "wrong device id",
                1002: "login refused (wrong credentials, or the relay is wrong for this device)",
            }.get(result, f"login failed with code {result}"))

        self.device_version = resp[12]
        self.ticket = struct.unpack("<I", resp[13:17])[0]
        self._aes = AES.new(self._media_key(), AES.MODE_ECB)

    def login_account(self, app_username: str, app_password: Optional[str] = None,
                      host: str = LOGIN_HOST, login_port: int = LOGIN_PORT) -> None:
        """JSON login on port 8089 — the path the official app uses.

        ``app_username`` is the per-device account name the app stores; when
        ``app_password`` is omitted the camera password is used.
        """
        app_password = app_password or self.password
        random_key = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        inner = AES.new(STATIC_KEY, AES.MODE_ECB).encrypt(pad(app_password.encode(), 16))
        outer = AES.new(random_key.encode(), AES.MODE_ECB).encrypt(inner)
        request = {
            "id": random.randint(100, 99999998),
            "method": "login",
            "params": {
                "version": 31, "phoneType": 1012, "deviceId": self.device_id,
                "domain": self.domain, "port": self.port, "accountId": 11,
                "username": app_username,
                "password": base64.b64encode(outer).decode(),
                "randomKey": random_key, "connectType": 0, "securityLevel": 1,
                "agora": 0, "ectx": int(time.time()), "p2pIdx": 0,
            },
        }
        body = json.dumps(request, separators=(",", ":")).encode()
        frame = struct.pack("<I", 0xFE000300) + struct.pack("<I", len(body)) + body  # 00 03 00 fe + length

        sock = socket.create_connection((host, login_port), timeout=self.timeout)
        try:
            sock.sendall(frame)
            header = self._recv(sock, 8)
            _, length = struct.unpack("<II", header)
            payload = self._recv(sock, length)
        finally:
            sock.close()

        data = json.loads(payload).get("v380") or {}
        handle = data.get("handle")
        if handle is None:
            raise V380Error(f"account login rejected: {json.loads(payload).get('result')}")
        self.ticket = int(handle)
        if not self.relay:
            self.relay = host
        self._aes = AES.new(self._media_key(), AES.MODE_ECB)

    # ── relay discovery ──────────────────────────────────────────────────
    @staticmethod
    def discover_relays(device_id: int, *, platform: int = 10001,
                        timeout: float = 12.0) -> list[str]:
        """Ask the V380 dispatch service which relays serve this device.

        ``platform`` 10001 for a normal camera, 20001 for multi-lens / panoramic.
        """
        import hashlib
        import urllib.request

        stamp = int(time.time())
        sign = hashlib.sha1(
            f"dev_id={device_id}&platform={platform}&timestamp={stamp}hsdata2022".encode()
        ).hexdigest()
        body = json.dumps({"dev_id": device_id, "platform": platform,
                           "timestamp": stamp, "sign": sign}).encode()
        request = urllib.request.Request(
            "http://dispa1.av380.net:8001/api/v1/get_stream_server",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read())
        if payload.get("code") != 2000:
            raise V380Error(f"dispatch refused with code {payload.get('code')}")
        return [server["ip"] for server in (payload.get("data") or []) if server.get("ip")]

    # ── SD-card listing (cmd 361 / 362 -> 461 / 462) ─────────────────────
    def list_day(self, day: _dt.date) -> list[Recording]:
        """Every recording the camera holds for one calendar day (camera local time)."""
        if self.ticket is None:
            raise V380Error("not logged in")

        cmd = bytearray(256)
        struct.pack_into("<I", cmd, 0, 361)
        struct.pack_into("<I", cmd, 4, 1012)
        cmd[8:8 + len(self.domain)] = self.domain.encode()
        struct.pack_into("<I", cmd, 58, self.port)      # cloud layout: 58 / 62 / 66
        struct.pack_into("<I", cmd, 62, self.device_id)
        struct.pack_into("<I", cmd, 66, self.ticket)
        for base, moment in ((72, _dt.datetime.combine(day, _dt.time(0, 0, 0))),
                             (92, _dt.datetime.combine(day, _dt.time(23, 59, 59)))):
            for index, value in enumerate((moment.year, moment.month, moment.day,
                                           moment.hour, moment.minute, moment.second)):
                struct.pack_into("<H", cmd, base + 2 * index, value)

        sock = self._connect()
        try:
            sock.sendall(bytes(cmd))
            head = self._recv(sock, 32)
            while struct.unpack("<I", head[:4])[0] == PKT_KEEPALIVE:
                head = self._recv(sock, 32)
            command, result = struct.unpack("<Ii", head[:8])
            if command != 461 and result != 1:
                raise V380Error(f"recording search refused ({command}/{result})")

            sock.sendall(struct.pack("<I", 362) + bytes(28))
            found: list[Recording] = []
            while True:
                try:
                    block = self._recv(sock, 512)
                except (socket.timeout, V380Error):
                    break
                command = struct.unpack("<I", block[:4])[0]
                if command == 460:
                    break
                if command == PKT_KEEPALIVE:
                    continue
                if command != 462:
                    continue
                flag = struct.unpack("<H", block[4:6])[0]
                count = min(struct.unpack("<H", block[6:8])[0], (512 - 8) // 13)
                for index in range(count):
                    offset = 8 + 13 * index
                    segment = struct.unpack("<I", block[offset:offset + 4])[0]
                    if not 0 < segment < 0x7FFFFFFF:
                        continue
                    found.append(Recording(
                        segment_id=segment,
                        kind=block[offset + 4],
                        start=struct.unpack("<I", block[offset + 5:offset + 9])[0],
                        end=struct.unpack("<I", block[offset + 9:offset + 13])[0],
                    ))
                if flag == 12:
                    break
        finally:
            sock.close()
        return sorted(found, key=lambda rec: rec.start)

    # ── clip download (cmd 363 / 364) ────────────────────────────────────
    def download(self, recording: Recording, output_path: str, *,
                 max_seconds: float = 900.0, progress=None) -> dict:
        """Fetch one recording and write a raw H.265 elementary stream to ``output_path``."""
        if self.ticket is None or self._aes is None:
            raise V380Error("not logged in")

        request = bytearray(256)
        struct.pack_into("<I", request, 0, 363)
        struct.pack_into("<I", request, 4, 1012)
        request[8:8 + len(self.domain)] = self.domain.encode()
        struct.pack_into("<I", request, 0x3A, self.port)
        struct.pack_into("<I", request, 0x3E, self.device_id)
        struct.pack_into("<I", request, 0x42, self.ticket)
        # The app puts the SEGMENT ID here, not the current time. Sending the
        # wall clock makes the camera answer 2002 ("already downloaded").
        struct.pack_into("<I", request, 0x46, recording.segment_id & 0xFFFFFFFF)
        _clock(request, 0x4A, recording.start_dt)
        request[0x51] = 1

        sock = self._connect()
        try:
            sock.sendall(bytes(request))
            reply = self._recv(sock, 64)
            while struct.unpack("<I", reply[:4])[0] == PKT_KEEPALIVE:
                reply = self._recv(sock, 64)
            command, result = struct.unpack("<Ii", reply[:8])
            if command != 463 or result != 1000:
                raise V380Error(f"playback refused ({command}/{result})")
            height = struct.unpack("<H", reply[14:16])[0]
            width = struct.unpack("<H", reply[16:18])[0]

            start = bytearray(32)
            struct.pack_into("<I", start, 0, 364)
            start[4] = 1                       # 1 = start
            struct.pack_into("<H", start, 6, 200)
            struct.pack_into("<I", start, 12, recording.segment_id & 0xFFFFFFFF)
            _clock(start, 16, recording.start_dt)
            sock.sendall(bytes(start))

            packets = frames = written = encrypted = 0
            buffer = bytearray()
            started = time.time()
            with open(output_path, "wb") as sink:
                while time.time() - started < max_seconds:
                    try:
                        header = self._recv(sock, 8, timeout=20)
                    except (socket.timeout, V380Error):
                        break
                    kind, note = header[0], header[1]
                    size = struct.unpack("<H", header[4:6])[0]
                    payload = b""
                    if size:
                        try:
                            payload = self._recv(sock, size, timeout=20)
                        except (socket.timeout, V380Error):
                            break

                    if kind == PKT_ACK_REQUEST:
                        ack = bytearray(32)
                        struct.pack_into("<I", ack, 0, 364)
                        ack[4] = 2                 # 2 = acknowledge
                        ack[5] = note
                        struct.pack_into("<H", ack, 6, 200)
                        struct.pack_into("<I", ack, 8, packets)
                        struct.pack_into("<I", ack, 12, recording.segment_id & 0xFFFFFFFF)
                        struct.pack_into("<H", ack, 23, (packets + 199) & 0xFFFF)
                        try:
                            sock.sendall(bytes(ack))
                        except OSError:
                            break
                        continue
                    if kind == PKT_IGNORE:
                        continue
                    if kind == PKT_SEGMENT_END:
                        break
                    if kind != PKT_DATA:
                        continue

                    packets += 1
                    buffer += payload
                    cursor = 0
                    while len(buffer) - cursor >= 22:
                        frame_type = struct.unpack("<H", buffer[cursor + 4:cursor + 6])[0]
                        frame_size = struct.unpack("<I", buffer[cursor + 18:cursor + 22])[0]
                        if frame_size > 0x80000:
                            cursor = len(buffer)
                            break
                        if len(buffer) - cursor < 22 + frame_size:
                            break
                        body = bytes(buffer[cursor + 22:cursor + 22 + frame_size])
                        cursor += 22 + frame_size
                        if frame_type not in VIDEO_TYPES:
                            continue
                        if _start_code(body) is None:
                            body = self._decrypt_video(body)
                            encrypted += 1
                        offset = _start_code(body)
                        if offset is None:
                            continue
                        if offset:
                            body = body[offset:]
                        sink.write(body)
                        frames += 1
                        written += len(body)
                    if cursor:
                        del buffer[:cursor]
                    if progress and packets % 500 == 0:
                        progress(packets, written)
        finally:
            stop = bytearray(32)
            struct.pack_into("<I", stop, 0, 364)
            stop[4] = 3                            # 3 = stop
            try:
                sock.sendall(bytes(stop))
            except OSError:
                pass
            sock.close()

        return {"packets": packets, "frames": frames, "bytes": written,
                "width": width, "height": height, "encrypted_frames": encrypted}

    def _decrypt_video(self, payload: bytes) -> bytes:
        """Recorded media is AES-ECB encrypted in the first 64 of every 80 bytes."""
        buf = bytearray(payload)
        offset = 0
        while offset + 64 <= len(buf):
            buf[offset:offset + 64] = self._aes.decrypt(bytes(buf[offset:offset + 64]))
            offset += 80
        return bytes(buf)

    # ── convenience ──────────────────────────────────────────────────────
    def iter_events(self, days: Iterable[_dt.date]) -> Iterator[Recording]:
        """Yield motion/alarm recordings across several days."""
        for day in days:
            for recording in self.list_day(day):
                if recording.is_event:
                    yield recording
