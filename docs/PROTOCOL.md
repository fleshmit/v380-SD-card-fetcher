# V380 SD-card protocol

Everything below was verified against real traffic from a V380 Pro camera (device id in
the 8-digit range, firmware reporting `deviceVersion` 31, H.265, 2304×2592 dual-lens).
All integers are **little-endian**. Where a layout differs between the LAN protocol and
the **cloud relay**, this document describes the cloud relay — that is what you need when
the camera is behind 4G or on another network.

> **Read this first:** the three offsets people get wrong are
> 1. `ts` in a `363` request is the **segment id**, not the wall clock;
> 2. the date is `u16 year` plus **five single bytes**, not six `u16`s;
> 3. in the cloud layout, port/device/ticket sit at `0x3A/0x3E/0x42`, **not** at the
>    `58/62/66` used by the listing command.

## 0. Addressing

Every packet carries the camera's routing hostname, `"<device-id>.nvdvr.net"`, as an
ASCII string. The relay is one of the V380 cloud servers; ask the dispatch service:

```
POST http://dispa1.av380.net:8001/api/v1/get_stream_server
Content-Type: application/json

{"dev_id": <device-id>, "platform": 10001, "timestamp": <unix>,
 "sign": sha1("dev_id=<id>&platform=<p>&timestamp=<ts>hsdata2022")}
```

`platform` is `10001` for a normal camera, `20001` for multi-lens / panoramic units.
A successful answer is `{"code": 2000, "data": [{"ip": "1.2.3.4", "domain": "1.2.3.4"}]}`.
Any of the returned IPs works; they all route to your device by hostname. The media port
is **8800**.

## 1. Authentication

Two independent paths exist. Which one works depends on the firmware and on what the
vendor app happens to use for that device.

### 1a. JSON login on port 8089 (the app's path)

The app talks to a message server (`LOGIN_HOST:8089` in your region) with a framed JSON
request — framing is the magic `00 03 00 fe` followed by the payload length as `u32`:

```json
{"id": <random>, "method": "login", "params": {
  "version": 31, "phoneType": 1012, "deviceId": <id>,
  "domain": "<id>.nvdvr.net", "port": 8800, "accountId": 11,
  "username": "REPLACE_WITH_APP_USERNAME", "password": "REPLACE_WITH_BASE64_BLOB", "randomKey": "REPLACE_WITH_16_RANDOM_CHARS",
  "connectType": 0, "securityLevel": 1, "agora": 0, "ectx": <unix>, "p2pIdx": 0}}
```

`password` is the device password encrypted twice:

```
inner = AES-ECB-PKCS7(device_password, "macrovideo+*#!^@")     # 16-byte static key
outer = AES-ECB(inner, randomKey)                              # randomKey sent in clear
```

The reply is `{"v380": {"handle": ..., "session": ..., "version": ...}, "result": {...}}`.
**`handle` is the ticket** used by every later command; `session` is not.

### 1b. Device login, cmd 1167 (520 bytes)

```
offset  field
0       1167            u32
4       1022            u32
8       version         u8   (21 or 31 — probe both)
9       1               u32
13      device id       u32
17      domain          ASCII, 50 bytes
67      port            u32  (8800)
71      username        ASCII, 32 bytes   ("admin" or the device id)
103     password        encrypted blob
```

Two password encodings are in use and the app auto-probes them:

```
zero48: AES-ECB((password + 48 zero bytes)[:48], static) then AES-ECB(result, randomKey)
pkcs7 : AES-ECB(PKCS7(password), static)               then AES-ECB(result, randomKey)[:64]
both are prefixed with the 16-byte randomKey (alphanumeric, sent in clear)
```

Reply:

```
0   1168   u32
4   result u32   1001 = OK, 1011 wrong username, 1012 wrong password,
                 1018 wrong device id, 1002 generic refusal / offline
12  deviceVersion  u8
13  ticket         u32     <-- session ticket for all later commands
17  session        u32
```

## 2. SD-card index — 361 / 362

Request, **256 bytes**:

```
offset  field
0       361        u32
4       1012       u32   (phoneType)
8       domain     ASCII, 50 bytes
58      port       u32   (8800)
62      device id  u32
66      ticket     u32
70      channel    u8    (0)
71      types      u8    (0 = all; bitmask of 1/2/3 where useful)
72      day start  u16 year, u8 month, u8 day, u8 hour, u8 minute, u8 second
82      (padding to 92)
92      day end    same six-field layout
```

Reply is 32 bytes (skip any `u32 == 2000` keep-alives first):

```
0  461      u32
4  result   u32   (1 = ok)
8  count    u16   <-- UNRELIABLE: often 0 even with 67 recordings. Ignore it.
```

Then send a bare `362` (32 bytes, only the `u32` command) and read **512-byte blocks**:

```
0  462 or 460   u32     460 = end of index
4  flag         u16     flag == 12 means this is the last block
6  count        u16     number of 13-byte entries (max 38 per block)
8  entries...   13 bytes each:
      id     u32    segment id
      kind   u8     1 = continuous, 2 = motion, 3 = alarm
      start  u32    unix seconds, camera wall clock
      end    u32
```

`kind` is what distinguishes an alarm clip from continuous footage. Alarms are stored as
**extra, overlapping** clips of a fixed length (2/3/4 minutes) around the detection; the
same footage also exists in the continuous entries. Cameras with only human/AI detection
enabled frequently emit `kind == 3` and never `kind == 2`.

## 3. Playback — 363 / 364

### Authorise (256 bytes)

```
offset  field
0       363        u32
4       1012       u32
8       domain     ASCII, 50 bytes
0x3A    port       u32   (8800)      <-- note: 0x3A, not 58
0x3E    device id  u32
0x42    ticket     u32
0x46    ts         u32   <-- THE SEGMENT ID. Not the time. See below.
0x4A    start      u16 year, u8 month, u8 day, u8 hour, u8 minute, u8 second
0x51    1          u8
```

**Why `ts` is the segment id:** sending `int(time.time())` here makes the camera answer
`result = 2002` ("already downloaded"), no matter which recording you asked for. Captured
traffic shows the app sending a value equal to the segment id in both the `363` and the
`364`. With the segment id in place the same request returns `1000` and the stream flows.

Reply (64 bytes; skip `u32 == 2000` keep-alives):

```
0  463      u32
4  result   u32   (1000 = granted)
8  ts       u32
14 height   u16
16 width    u16
```

### Start the transfer (32 bytes)

```
0   364          u32
4   1            u8    (1 = start, 2 = acknowledge, 3 = stop)
5   note         u8
6   200          u16   (packets requested)
8   0            u32
12  segment id   u32
16  start        u16 year, u8 month, u8 day, u8 hour, u8 minute, u8 second
```

### Reading the stream

Each packet is an 8-byte header plus payload:

```
0  type   u8   0x7F = data, 0x6F = please acknowledge, 0xEF = segment finished,
               0x1F = ignore
1  note   u8
2  packet u16
4  size   u16
6  (2 bytes unused)
```

On `0x6F` reply with a 32-byte acknowledge: same layout as the start packet but
`u8 = 2` at offset 4, a running packet counter as `u32` at offset 8, the segment id at 12
and `counter + 199` as `u16` at offset 23.

Data payloads concatenate into a frame stream; each frame has a 22-byte header:

```
0   id      u32
4   type    u16     video: 0, 1, 40, 41, 50, 51, 52, 53  (keyframes: 0, 40, 50, 52)
                    audio: 21 = PCM16, 22 = IMA-ADPCM, 24-26 = unsupported
10  ts      u64     milliseconds
18  size    u32
22  payload
```

## 4. Media decryption

When `deviceVersion > 30` the recorded video is encrypted. The key is derived from the
ticket, and the cipher is AES-ECB applied to **the first 64 bytes of every 80-byte block**
(the remaining 16 bytes are in the clear):

```
key[0:4]   = ticket,             little-endian u32
key[4:12]  = 0x618123462c14795c, little-endian u64
key[12:16] = 0x82800df0,         little-endian u32
```

After decryption, look for an Annex-B start code (`00 00 01`, optionally preceded by a
zero) in the first 16 bytes; that is the beginning of an H.265 NAL unit. If a start code is
already present, the frame was not encrypted — newer firmware only encrypts some frames,
so test before decrypting.

The result is a raw H.265 elementary stream. Wrap it in MP4 with
`ffmpeg -fflags +genpts -r 12.5 -i stream.h265 -c:v copy -tag:v hvc1 out.mp4`
(12.5 fps is what the camera produces; `-c copy` is lossless and instant).

## 5. Traps, in the order they bite

1. **`count` in `461` is a lie.** It reports 0 while the index holds 67 entries. Always
   ask for the `362` blocks.
2. **`-1003` is an answer, not a failure to reach the camera.** It means the `363` was
   parsed but not accepted — usually because the segment id was missing at `0x46`/`0x70`.
   Silence is a different problem: wrong host, wrong port, or a relay that does not
   serve this device.
3. **Do not mix the listing layout with the playback layout.** `361` uses `58/62/66` plus
   six `u16` clock fields; `363` uses `0x3A/0x3E/0x42` plus a `u16` + five bytes clock.
   Mixing them silently produces accepted-but-empty requests.
4. **`2002` means "already downloaded"** — a cloud-side state, not a malformed packet.
   Sending the current time as `ts` is the usual cause.
5. **Keep-alives (`u32 == 2000`) arrive on every socket.** Skip them everywhere you read,
   including between index blocks and inside the playback reply.
6. **The relay closes the socket ~3 s after a `0x6F` that it does not like.** A clean
   close right after the acknowledge means the authorisation, not the transport, failed.
7. **The camera stores local time as if it were UTC.** Formatting `start` with an
   aware timezone shifts the query by hours and yields an empty index.

## 6. Sources

* [cimplesid/v380-mac](https://github.com/cimplesid/v380-mac) (OpenV380, MIT) — cloud
  layouts for `361`/`363`/`364`, the keep-alive and frame handling, media key derivation.
* [PyanSofyan/V380Decoder](https://github.com/PyanSofyan/V380Decoder) — dispatch endpoint
  and signature scheme.
* Packet captures of the vendor app (PCAPdroid on Android) — used to correct the field
  positions, the `ts == segment id` rule and the clock encoding documented above.
