"""Command line interface: list a day, fetch events, fetch everything."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

from .client import Recording, V380Camera, V380Error

DEFAULT_TEMPLATE = "{day}/{kind}_{day}_{time}_{id}.mp4"


def load_config(path: str | None) -> dict:
    candidates = [path, os.environ.get("V380_CONFIG"), "v380.config.json",
                  str(pathlib.Path.home() / ".config" / "v380" / "config.json")]
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).is_file():
            return json.loads(pathlib.Path(candidate).read_text())
    return {}


def make_camera(config: dict, args) -> V380Camera:
    device_id = args.device or config.get("device_id")
    password = args.password or os.environ.get("V380_PASSWORD") or config.get("password")
    if not device_id or not password:
        raise SystemExit("need a device id and a password: --device/--password, "
                         "V380_PASSWORD, or a config file (see config.example.json)")
    username = args.username or config.get("username")

    relay = args.relay or config.get("relay")
    if not relay:
        platform = int(config.get("platform", 10001))
        try:
            relays = V380Camera.discover_relays(int(device_id), platform=platform)
        except V380Error as exc:
            raise SystemExit(f"relay discovery failed: {exc}")
        if not relays:
            raise SystemExit("relay discovery returned no servers")
        relay = relays[0]

    return V380Camera(int(device_id), password, username=username, relay=relay)


def login(camera: V380Camera, config: dict, args) -> None:
    mode = args.login or config.get("login", "account")
    if mode == "device":
        camera.login_device(version=int(config.get("device_version", 31)),
                            encoding=config.get("password_encoding", "pkcs7"))
    else:
        camera.login_account(config.get("app_username") or str(camera.device_id),
                             config.get("app_password"))
    print(f"logged in (ticket={camera.ticket}, relay={camera.relay})", file=sys.stderr)


def remux(source: str, target: str, fps: float, reencode: bool) -> bool:
    """Wrap the raw H.265 stream in MP4. Copying the codec is instant and lossless."""
    codec = ["-c:v", "libx265", "-preset", "ultrafast", "-crf", "27"] if reencode else ["-c:v", "copy"]
    command = ["ffmpeg", "-y", "-hide_banner", "-v", "error", "-fflags", "+genpts",
               "-r", str(fps), "-i", source, *codec, "-tag:v", "hvc1", target]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit("ffmpeg not found — install it, or use --keep-h265")
    if result.returncode != 0:
        print(f"ffmpeg failed: {result.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return os.path.exists(target)


def handle(camera: V380Camera, recording: Recording, args, outdir: pathlib.Path) -> bool:
    name = args.template.format(day=recording.start_dt.strftime("%Y-%m-%d"),
                               time=recording.start_dt.strftime("%H-%M-%S"),
                               kind=recording.kind_name.upper(),
                               id=recording.segment_id)
    target = outdir / name
    if target.exists() and target.stat().st_size > 10000 and not args.overwrite:
        print(f"= {target.name}")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = str(target) + ".h265"
    print(f"> {recording.kind_name:10s} {recording.start_dt:%Y-%m-%d %H:%M:%S} "
          f"({recording.duration}s) -> {target.name}", flush=True)
    try:
        stats = camera.download(recording, raw, max_seconds=args.max_seconds)
    except V380Error as exc:
        print(f"  failed: {exc}", file=sys.stderr)
        if os.path.exists(raw):
            os.remove(raw)
        return False
    if stats["bytes"] < 20000:
        print(f"  too little data ({stats['bytes']} B)", file=sys.stderr)
        os.replace(raw, raw + ".incomplete")
        return False
    print(f"  {stats['frames']} frames, {stats['bytes'] / 1048576:.1f} MB, "
          f"{stats['width']}x{stats['height']}", flush=True)
    if args.keep_h265:
        os.replace(raw, str(target) + ".h265")
        return True
    if remux(raw, str(target), args.fps, args.reencode):
        os.remove(raw)
        return True
    os.replace(raw, raw + ".incomplete")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="v380sd", description="Download SD-card recordings from a V380 camera, no phone needed.")
    parser.add_argument("-c", "--config", help="path to a JSON config file")
    parser.add_argument("--device", help="camera device id (e.g. 12345678)")
    parser.add_argument("--password", help="device password (prefer V380_PASSWORD)")
    parser.add_argument("--username", help="login username (default: the device id)")
    parser.add_argument("--relay", help="relay IP (default: ask the dispatch service)")
    parser.add_argument("--login", choices=["account", "device"], help="authentication path")
    parser.add_argument("--day", default=dt.date.today().isoformat(),
                        help="day to read, YYYY-MM-DD (default today)")
    parser.add_argument("--days", type=int, default=1, help="how many days back to walk")
    parser.add_argument("--list", action="store_true", help="only print the index")
    parser.add_argument("--events", action="store_true",
                        help="only motion/alarm recordings (kinds 2 and 3)")
    parser.add_argument("--out", default="recordings", help="output directory")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE,
                        help=f"output name template (default {DEFAULT_TEMPLATE})")
    parser.add_argument("--fps", type=float, default=12.5, help="frame rate for the MP4 wrapper")
    parser.add_argument("--max-seconds", type=float, default=900.0,
                        help="wall-clock cap per clip")
    parser.add_argument("--keep-h265", action="store_true", help="keep the raw stream, skip ffmpeg")
    parser.add_argument("--reencode", action="store_true",
                        help="re-encode instead of copying the codec (slow)")
    parser.add_argument("--overwrite", action="store_true", help="re-download existing files")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    outdir = pathlib.Path(args.out)
    camera = make_camera(config, args)
    try:
        login(camera, config, args)
    except V380Error as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 2

    first = dt.date.fromisoformat(args.day)
    days = [first - dt.timedelta(days=offset) for offset in range(max(1, args.days))]
    total = saved = 0
    for day in days:
        try:
            recordings = camera.list_day(day)
        except V380Error as exc:
            print(f"{day}: listing failed: {exc}", file=sys.stderr)
            continue
        if args.events:
            recordings = [rec for rec in recordings if rec.is_event]
        kinds = {}
        for rec in recordings:
            kinds[rec.kind_name] = kinds.get(rec.kind_name, 0) + 1
        print(f"{day}: {len(recordings)} recordings {kinds or ''}", flush=True)
        if args.list:
            for rec in recordings:
                print(f"  {rec.start_dt:%H:%M:%S} -> {rec.start_dt + dt.timedelta(seconds=rec.duration):%H:%M:%S}"
                      f"  {rec.kind_name:10s} id={rec.segment_id}")
            continue
        for rec in recordings:
            total += 1
            if handle(camera, rec, args, outdir):
                saved += 1
    if not args.list:
        print(f"\nsaved {saved} of {total} -> {outdir.resolve()}")
    return 0 if saved or args.list else 1


if __name__ == "__main__":
    raise SystemExit(main())
