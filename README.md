# v380-SD-card-fetcher

**Pull recordings off a V380 / V380 Pro camera's SD card over the internet — no phone, no vendor app, no card removal, no NVR.**

Most open-source V380 tooling gives you the *live* stream. This one gives you the
**SD-card recording index and the actual files**: continuous footage, motion clips and
alarm clips, straight onto your NAS or server, on a schedule.

```console
$ v380sd --day 2026-11-17 --list
logged in (ticket=5555, relay=130.61.214.86)
2026-11-17: 73 recordings {'continuous': 73}
  06:01:15 -> 06:11:15  continuous id=1794891675
  06:11:09 -> 06:21:09  continuous id=1794892269
  ...

$ v380sd --days 3 --events --out /mnt/media/camera-data/ALARM
2026-11-17: 0 recordings
2026-11-16: 0 recordings
2026-11-15: 12 recordings {'alarm': 12}
> alarm      2026-11-15 15:24:10 (120s) -> ALARM_2026-11-15_15-24-10_1794752650.mp4
  1496 frames, 11.5 MB, 2304x2592
```

---

## Disclaimer

**This project was written by an AI agent** (Hermes, driven by a human operator who owns the
camera), working from public reverse-engineering notes and from packet captures of the vendor
app. Treat the code accordingly:

* It is **not** an official V380, V380 Pro or Macrovideo product, and it is not affiliated
  with, endorsed by, or supported by any camera manufacturer. "V380" is used only to
  describe the devices this tool interoperates with.
* It has **nothing to do with Home Assistant** or any home-automation platform. It is a
  standalone Python CLI — no HA integration, no MQTT, no add-on, no HACS component. Any
  similarity in vocabulary is incidental.
* AI-generated code can be confidently wrong. Every offset in `docs/PROTOCOL.md` was checked
  against real traffic, but **your firmware may differ**. Verify on your own hardware before
  relying on it.
* Provided **as is, without warranty of any kind**. You are responsible for what you run it
  against and for the footage you store.

Use it only on cameras **you own**, with **your own** credentials.

---

## Why this exists

A V380 camera with a 4G modem and no NVR is a black box: the footage sits on a microSD
card inside the camera, and the only official way to get it is tapping through the phone
app. The app talks to the camera over a cloud relay, so the traffic is reachable — the
protocol is just undocumented.

This project implements that protocol. Given a device ID and the device password it will:

1. ask the V380 dispatch service which relay serves your camera,
2. log in and obtain a session ticket,
3. list the SD-card recording index for any day the card still holds,
4. download individual recordings, decrypt them and write playable MP4 files.

## Features

* **No phone, no card removal, no vendor app** — works over the internet from anywhere.
* **SD-card index** for any calendar day, with per-recording kind (`continuous`, `motion`, `alarm`).
* **Download** a single clip, a whole day, or just the events.
* **Decryption** of recorded media (AES, newer firmware).
* **Resumable/idempotent** — files already on disk are skipped, so a cron job can
  simply re-scan the last few days and fill in whatever was missed.
* **Raw H.265 kept or MP4 muxed** (lossless `-c copy` by default; re-encoding is opt-in).

## Requirements

* Python **3.9+**
* `pycryptodome` (AES for the login and the media)
* `ffmpeg` on `PATH` (only for MP4 muxing — `--keep-h265` skips it)
* The camera's **device ID** (the number shown under the camera name in the app) and its
  **device password**

```console
pip install -r requirements.txt
```

## Configuration

Copy `config.example.json` and fill it in, or use flags / environment variables.
**Never commit your credentials.**

```json
{
  "device_id": 12345678,
  "password": "REPLACE_WITH_DEVICE_PASSWORD",
  "login": "account",
  "app_username": "AbCd1234",
  "device_version": 31,
  "password_encoding": "pkcs7",
  "platform": 10001
}
```

| key | meaning |
|---|---|
| `device_id` | camera device id, the number in the app |
| `password` | the camera's **device password** (camera settings → device password) |
| `login` | `"account"` (default, port 8089 JSON login) or `"device"` (cmd 1167) |
| `app_username` | only for `account` login: the per-device account name the app uses |
| `device_version` / `password_encoding` | only for `device` login; try `31`/`pkcs7`, then `21`/`zero48` |
| `platform` | `10001` normal camera, `20001` multi-lens / panoramic |
| `relay` | optional fixed relay IP; otherwise discovered automatically |

Environment fallbacks: `V380_PASSWORD`, `V380_CONFIG` (path to the config file).

## Usage

```console
# what is on the card today?
v380sd --list

# everything from the last three days (the card usually only keeps ~3)
v380sd --days 3 --out /mnt/media/camera-data/CONTINUOUS

# only alarms and motion, into a date-per-folder tree
v380sd --days 3 --events --out /mnt/media/camera-data/ALARM \
       --template "{day}/{kind}_{day}_{time}_{id}.mp4"

# a single day, keep the raw bitstream
v380sd --day 2026-11-15 --keep-h265 --out ./raw

# one specific clip: pass its id as the day is listed, then fetch with --list first
```

Useful flags: `--day`, `--days`, `--list`, `--events`, `--out`, `--template`, `--fps`
(default 12.5), `--max-seconds` (per-clip cap), `--keep-h265`, `--reencode`, `--overwrite`.

Exit codes: `0` saved at least one file (or `--list`), `1` nothing saved, `2` login failed.

## Automating it

The intended pattern is a **daily cron**: re-scan the last few days and download only the
events. It is idempotent, so a missed run costs nothing.

```cron
# every day at 07:00 — alarms from the last 3 days into the archive
0 7 * * * cd /opt/v380 && ./venv/bin/v380sd --days 3 --events \
          --out /mnt/media/camera-data/ALARM \
          --template "{day}/{kind}_{day}_{time}_{id}.mp4" >> /var/log/v380.log 2>&1
```

`examples/` contains a systemd unit and a wrapper script you can adapt.

## How it works

The camera speaks a compact binary protocol (big-endian-free, all little-endian) to a
**cloud relay**, addressed as `<device-id>.nvdvr.net:8800`. Three command pairs matter:

| command | direction | purpose |
|---|---|---|
| `361`/`362` → `461`/`462` | control | list the SD-card index for a day |
| `363` → `463` | control | authorise playback of one recording |
| `364` → data packets | control → stream | start / acknowledge / stop the transfer |

Recorded frames are H.265, and on newer firmware the first 64 bytes of every 80-byte
block are AES-ECB encrypted with a key derived from the session ticket.

**Full byte layouts, offsets, keys and the frame format are in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).** If you are here to write your own client, start
there — including the three mistakes that cost the author days.

## Limitations (read before filing a bug)

* **The relay must be reachable.** The camera does not have to be on your LAN, but it does
  have to be online; a sleeping 4G camera answers nothing.
* **The card only keeps a few days** (typically 2–3) — poll at least daily or data is gone.
* **Not instant.** This polls the SD card. The camera also pushes alerts through a separate
  notification channel (the app's `msgsznode` service), which this project does **not**
  implement yet — for second-level alerts you still need the app.
* **No audio muxing.** Recorded audio frames use codecs (PCM16 / IMA-ADPCM) this tool skips;
  clips are video-only.
* **Firmware varies.** The protocol offsets that work here were verified against one camera
  family. If your camera answers `-1003`, `2002` or nothing at all, open an issue with the
  firmware string and a packet capture.
* **Login paths differ by firmware.** `account` login needs the app's per-device username;
  `device` login (cmd 1167) needs the device password and a working packet layout.

## Legal

Use this only on cameras **you own**. It authenticates with your own credentials, reads
your own card and downloads your own footage. Do not use it to access other people's
cameras — that is a crime in most jurisdictions and it is not what this is for.

## Credits

Standing on shoulders:

* **[cimplesid/v380-mac](https://github.com/cimplesid/v380-mac)** (OpenV380, MIT) — a
  clean-room Swift implementation of the V380 protocol, and the source of the cloud
  packet layouts used here. Without it this project would not exist.
* **[PyanSofyan/V380Decoder](https://github.com/PyanSofyan/V380Decoder)** — dispatch
  endpoint and signing scheme.
* **[j-bloggs/v380-4g-stream](https://github.com/j-bloggs/v380-4g-stream)** — useful notes
  on relay login and AES handling for 4G units.
* The various `V380Decoder` ports that mapped the frame layout.

Offsets in `docs/PROTOCOL.md` were additionally cross-checked **byte for byte** against
real app traffic, which is how the `ts == segment id` rule and the `0x3A/0x3E/0x42`
field positions were found (the published client code uses an older layout).

## License

MIT — see [LICENSE](LICENSE). Third-party credits and their licence terms are in
[NOTICE](NOTICE).
