import asyncio
import configparser
import json
import math
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen

import edge_tts

BASE_DIR = Path(__file__).parent
SETTINGS_PATH = BASE_DIR / "settings.ini"

API_TEMPLATE = "https://kalkulatorkolejowy.pl/bilkom/api/departures/normal/{station_id}"

# Timing constants (can be overridden in settings.ini, but not below these values)
MIN_POLL_SECONDS = 5 * 60          # absolute minimum poll interval: 5 minutes
ANNOUNCE_BEFORE_SEC = 3 * 60       # announce departure 3 min before scheduled time
DELAY_REPEAT_SEC = 5 * 60          # repeat delay info every 5 min
SCHEDULER_TICK_SEC = 10


@dataclass
class Messages:
    """Announcement templates loaded from the [messages] section of settings.ini."""
    departure_on_time: str
    departure_delayed: str
    delay_reminder: str

    def format_departure(self, train_code: str, dest: str, track: str,
                         platform: str, delay_min: int,
                         scheduled_time: str, departure_time: str) -> str:
        if delay_min > 4:
            return self.departure_delayed.format(
                train_code=train_code,
                dest=dest,
                track=track,
                platform=platform,
                delay_min=delay_min,
                scheduled_time=scheduled_time,
                departure_time=departure_time,
            )
        return self.departure_on_time.format(
            train_code=train_code,
            dest=dest,
            track=track,
            platform=platform,
            delay_min=delay_min,
            scheduled_time=scheduled_time,
            departure_time=departure_time,
        )

    def format_delay_reminder(self, train_code: str, dest: str, delay_min: int,
                              scheduled_time: str, departure_time: str) -> str:
        return self.delay_reminder.format(
            train_code=train_code,
            dest=dest,
            delay_min=delay_min,
            scheduled_time=scheduled_time,
            departure_time=departure_time,
        )


@dataclass
class Settings:
    station_id: int
    poll_seconds: int
    lookahead_seconds: int
    jingle_path: Path
    volume: int
    pause_after_jingle_ms: int
    tts_voice: str
    tts_rate: str
    messages: Messages
    pronunciation: dict[str, str] = field(default_factory=dict)


@dataclass
class TrainEvent:
    key: str
    train_code: str
    dest: str
    track: str
    platform: str
    delay_min: int          # already rounded to nearest 5 minutes
    planned_ts: int
    calculated_ts: int


def round_to_5(minutes: int) -> int:
    """Round delay to the nearest 5 minutes."""
    return int(5 * math.floor((minutes + 2.5) / 5))


def load_settings() -> Settings:
    if not SETTINGS_PATH.exists():
        raise FileNotFoundError(f"settings.ini not found: {SETTINGS_PATH}")

    cfg = configparser.RawConfigParser()
    cfg.optionxform = str  # preserve original key casing (default is lower())
    cfg.read(SETTINGS_PATH, encoding="utf-8")

    station_id = cfg.getint("general", "station")
    poll_seconds_cfg = cfg.getint("general", "poll_seconds", fallback=300)
    lookahead_minutes = cfg.getint("general", "lookahead_minutes", fallback=120)

    # Enforce minimum poll interval of 5 minutes
    poll_seconds = max(MIN_POLL_SECONDS, poll_seconds_cfg)
    if poll_seconds != poll_seconds_cfg:
        print(f"[INFO] poll_seconds={poll_seconds_cfg} is below the minimum of {MIN_POLL_SECONDS}s — using {poll_seconds}s")

    jingle_file = cfg.get("jingle", "file")
    tts_voice = cfg.get("tts", "voice", fallback="pl-PL-ZofiaNeural")
    tts_rate = cfg.get("tts", "rate", fallback="+0%")
    volume = cfg.getint("audio", "volume", fallback=100)
    pause_ms = cfg.getint("audio", "pause_after_jingle_ms", fallback=0)

    if not (0 <= volume <= 100):
        raise ValueError("audio.volume must be in the range 0..100")
    if lookahead_minutes <= 0:
        raise ValueError("general.lookahead_minutes must be > 0")

    # Default announcement templates (used when [messages] section is absent)
    DEFAULT_ON_TIME = (
        "Uwaga. Pociąg spółki {train_code} do stacji {dest}. "
        "Odjedzie z toru {track}, przy peronie {platform}."
    )
    DEFAULT_DELAYED = (
        "Uwaga. Pociąg spółki {train_code} do stacji {dest}. "
        "Odjedzie z toru {track}, przy peronie {platform}. "
        "Pociąg jest opóźniony o {delay_min} minut."
    )
    DEFAULT_REMINDER = (
        "Informacja o opóźnieniu. Pociąg {train_code} do stacji {dest} "
        "jest opóźniony o {delay_min} minut."
    )

    messages = Messages(
        departure_on_time=cfg.get("messages", "departure_on_time", fallback=DEFAULT_ON_TIME),
        departure_delayed=cfg.get("messages", "departure_delayed", fallback=DEFAULT_DELAYED),
        delay_reminder=cfg.get("messages", "delay_reminder", fallback=DEFAULT_REMINDER),
    )

    pron = {}
    if cfg.has_section("pronunciation"):
        pron = dict(cfg.items("pronunciation"))

    s = Settings(
        station_id=station_id,
        poll_seconds=poll_seconds,
        lookahead_seconds=lookahead_minutes * 60,
        jingle_path=BASE_DIR / jingle_file,
        volume=volume,
        pause_after_jingle_ms=pause_ms,
        tts_voice=tts_voice,
        tts_rate=tts_rate,
        messages=messages,
        pronunciation=pron,
    )

    if not s.jingle_path.exists():
        raise FileNotFoundError(f"File not found: {s.jingle_path}")

    return s


def fetch_json(url: str, timeout: int = 10) -> Any:
    req = Request(url, headers={"User-Agent": "TrainAnnouncer/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8")
    return json.loads(data)


def clean_field(x: Any) -> str:
    if x is None:
        return ""
    s = str(x)
    s = re.sub(r"<[^>]*>", "", s)
    s = s.replace("\u00a0", " ")
    return s.strip()


def parse_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        return int(str(x).strip())
    except Exception:
        return default


def normalize_event(row: Dict[str, Any]) -> Optional[TrainEvent]:
    train_code = clean_field(row.get("trainCode"))
    dest = clean_field(row.get("arrivalStation"))
    track = clean_field(row.get("track"))
    platform = clean_field(row.get("platform"))
    raw_delay = parse_int(row.get("delay"), 0)

    # Round delay to nearest 5 minutes
    delay_min = round_to_5(max(0, raw_delay))

    planned_ts = parse_int(row.get("timestamp"), 0)
    calculated_ts = parse_int(row.get("calculatedTime"), 0)

    if calculated_ts <= 0 and planned_ts > 0:
        calculated_ts = planned_ts + max(0, raw_delay) * 60

    if not train_code or calculated_ts <= 0:
        return None

    # Filter out "podroz" tracks and HTML-contaminated platforms (through-carriages / redirects)
    if track.lower() == "podroz" or "<" in platform:
        return None

    extra = clean_field(row.get("extraLink"))
    key = extra if extra else f"{train_code}|{planned_ts}"

    return TrainEvent(
        key=key,
        train_code=train_code,
        dest=dest or "unknown station",
        track=track or "unknown",
        platform=platform or "unknown",
        delay_min=delay_min,
        planned_ts=planned_ts,
        calculated_ts=calculated_ts,
    )


def play_wav(path: Path, volume: int) -> None:
    path = path.resolve()

    if shutil.which("ffplay"):
        subprocess.run(
            ["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet",
             "-volume", str(volume), str(path)],
            check=False,
        )
        return

    if sys.platform.startswith("linux"):
        for player in ("aplay", "paplay"):
            if shutil.which(player):
                subprocess.run([player, "-q", str(path)], check=False)
                return
        print(f"[INFO] ffplay/aplay/paplay not found. WAV file: {path}")
        return

    if sys.platform == "darwin":
        if shutil.which("afplay"):
            subprocess.run(["afplay", str(path)], check=False)
            return
        print(f"[INFO] afplay not found. WAV file: {path}")
        return

    if sys.platform.startswith("win"):
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            f"$p='{str(path)}'; "
            "$sp=New-Object System.Media.SoundPlayer($p); "
            "$sp.PlaySync();"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
        return

    print(f"[INFO] Unsupported platform. WAV file: {path}")


class AudioWorker(threading.Thread):
    def __init__(self, settings: Settings):
        super().__init__(daemon=True)
        self.settings = settings
        self.q: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()
        self.q.put("")

    def say(self, text: str):
        self.q.put(text)

    def _synthesize(self, text: str, out_path: Path) -> None:
        """Synthesize speech via edge-tts (synchronous wrapper around async API)."""
        async def _run():
            communicate = edge_tts.Communicate(
                text,
                voice=self.settings.tts_voice,
                rate=self.settings.tts_rate,
            )
            await communicate.save(str(out_path))

        asyncio.run(_run())

    def run(self):
        while not self._stop.is_set():
            text = self.q.get()
            if self._stop.is_set() or not text:
                continue

            # 1) jingle (WAV or MP3 — detected by file extension)
            jingle = self.settings.jingle_path
            if jingle.suffix.lower() == ".mp3":
                play_mp3(jingle, self.settings.volume)
            else:
                play_wav(jingle, self.settings.volume)

            # 2) pause after jingle
            if self.settings.pause_after_jingle_ms > 0:
                time.sleep(self.settings.pause_after_jingle_ms / 1000.0)

            # 3) TTS → MP3 → play
            with tempfile.TemporaryDirectory() as tmp:
                tts_mp3 = Path(tmp) / "tts.mp3"
                try:
                    self._synthesize(text, tts_mp3)
                    play_mp3(tts_mp3, self.settings.volume)
                except Exception as e:
                    print(f"[WARN] TTS error: {e}")


def play_mp3(path: Path, volume: int) -> None:
    """Play an MP3 file. ffplay supports volume control; fallback via mpg123/afplay/PowerShell."""
    path = path.resolve()

    if shutil.which("ffplay"):
        subprocess.run(
            ["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet",
             "-volume", str(volume), str(path)],
            check=False,
        )
        return

    if sys.platform.startswith("linux"):
        if shutil.which("mpg123"):
            subprocess.run(["mpg123", "-q", str(path)], check=False)
            return
        if shutil.which("mpg321"):
            subprocess.run(["mpg321", "-q", str(path)], check=False)
            return
        print(f"[INFO] ffplay/mpg123 not found. MP3 file: {path}")
        return

    if sys.platform == "darwin":
        if shutil.which("afplay"):
            subprocess.run(["afplay", str(path)], check=False)
            return
        print(f"[INFO] afplay not found. MP3 file: {path}")
        return

    if sys.platform.startswith("win"):
        ps = (
            "Add-Type -AssemblyName presentationCore; "
            f"$mp = New-Object System.Windows.Media.MediaPlayer; "
            f"$mp.Open([uri]'{str(path)}'); "
            "$mp.Play(); Start-Sleep -Seconds 10; $mp.Stop();"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
        return

    print(f"[INFO] Unsupported platform. MP3 file: {path}")


def apply_pronunciation(text: str, mapping: dict) -> str:
    if not mapping:
        return text
    items = sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True)
    for src, dst in items:
        src = src.strip()
        dst = dst.strip()
        if not src:
            continue
        # Match whole tokens only — boundary defined as non-alphanumeric character or string edge
        pattern = r"(?<![A-Za-z0-9])" + re.escape(src) + r"(?![A-Za-z0-9])"
        text = re.sub(pattern, dst, text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def main():
    settings = load_settings()
    url = API_TEMPLATE.format(station_id=settings.station_id)

    print("Automatic train announcer (edge-tts)")
    print(f"• Station ID : {settings.station_id}")
    print(f"• API        : {url}")
    print(f"• Poll       : {settings.poll_seconds}s ({settings.poll_seconds//60} min), "
          f"Lookahead: {settings.lookahead_seconds//60} min")
    print(f"• TTS voice  : {settings.tts_voice}  rate={settings.tts_rate}")
    print("Press Ctrl+C to quit.\n")

    speaker = AudioWorker(settings)
    speaker.start()

    latest_events: Dict[str, TrainEvent] = {}
    announced_departure: Dict[str, bool] = {}
    last_delay_announce_ts: Dict[str, int] = {}

    last_fetch: int = 0

    try:
        while True:
            now = int(time.time())

            # ── 1. Fetch data ─────────────────────────────────────────────────
            if now - last_fetch >= settings.poll_seconds:
                last_fetch = now
                try:
                    data = fetch_json(url)
                    events: List[TrainEvent] = []
                    if isinstance(data, list):
                        for row in data:
                            if isinstance(row, dict):
                                ev = normalize_event(row)
                                if ev and 0 <= ev.calculated_ts - now <= settings.lookahead_seconds:
                                    events.append(ev)

                    events.sort(key=lambda e: e.calculated_ts)
                    latest_events = {e.key: e for e in events}

                    # clean up stale keys
                    for k in list(announced_departure.keys()):
                        if k not in latest_events:
                            announced_departure.pop(k, None)
                            last_delay_announce_ts.pop(k, None)

                    print(f"\n[{time.strftime('%H:%M:%S')}] Departure board ({len(events)} trains):")
                    print(f"  {'Departure':<10} {'Train':<12} {'Destination':<30} {'Track':<6} {'Platform':<9} Delay")
                    print(f"  {'----------':<10} {'------------':<12} {'------------------------------':<30} {'------':<6} {'---------':<9} -----")
                    for e in events:
                        dep_time = time.strftime('%H:%M', time.localtime(e.calculated_ts))
                        delay_str = f"+{e.delay_min} min" if e.delay_min > 0 else "-"
                        dest_short = e.dest[:30]
                        print(f"  {dep_time:<10} {e.train_code:<12} {dest_short:<30} {e.track:<6} {e.platform:<9} {delay_str}")
                    print()
                except Exception as e:
                    print(f"[WARN] Failed to fetch/parse API: {e}")

            # ── 2. Logika zapowiedzi ──────────────────────────────────────────
            for ev in list(latest_events.values()):
                if ev.calculated_ts <= now:
                    continue

                scheduled_time = time.strftime('%H:%M', time.localtime(ev.planned_ts))
                departure_time = time.strftime('%H:%M', time.localtime(ev.calculated_ts))

                # Zapowiedź 3 min przed odjazdem
                if not announced_departure.get(ev.key, False):
                    if now >= ev.calculated_ts - ANNOUNCE_BEFORE_SEC:
                        msg = settings.messages.format_departure(
                            train_code=ev.train_code,
                            dest=ev.dest,
                            track=ev.track,
                            platform=ev.platform,
                            delay_min=ev.delay_min,
                            scheduled_time=scheduled_time,
                            departure_time=departure_time,
                        )
                        print(f"[{time.strftime('%H:%M:%S')}] Zapowiedź: {msg}")
                        msg = apply_pronunciation(msg, settings.pronunciation)
                        speaker.say(msg)
                        announced_departure[ev.key] = True
                        if ev.delay_min > 0:
                            last_delay_announce_ts[ev.key] = now

                # Cykliczne info o opóźnieniu
                if ev.delay_min > 0:
                    last_ts = last_delay_announce_ts.get(ev.key, 0)
                    time_left = ev.calculated_ts - now
                    if last_ts == 0:
                        if time_left <= 30 * 60:
                            msg = settings.messages.format_delay_reminder(
                                train_code=ev.train_code,
                                dest=ev.dest,
                                delay_min=ev.delay_min,
                                scheduled_time=scheduled_time,
                                departure_time=departure_time,
                            )
                            print(f"[{time.strftime('%H:%M:%S')}] Opóźnienie: {msg}")
                            msg = apply_pronunciation(msg, settings.pronunciation)
                            speaker.say(msg)
                            last_delay_announce_ts[ev.key] = now
                    elif now - last_ts >= DELAY_REPEAT_SEC and time_left <= 60 * 60:
                        msg = settings.messages.format_delay_reminder(
                            train_code=ev.train_code,
                            dest=ev.dest,
                            delay_min=ev.delay_min,
                            scheduled_time=scheduled_time,
                            departure_time=departure_time,
                        )
                        print(f"[{time.strftime('%H:%M:%S')}] Opóźnienie (przypomnienie): {msg}")
                        msg = apply_pronunciation(msg, settings.pronunciation)
                        speaker.say(msg)
                        last_delay_announce_ts[ev.key] = now

            time.sleep(SCHEDULER_TICK_SEC)

    except KeyboardInterrupt:
        print("\nKoniec.")
    finally:
        speaker.stop()


if __name__ == "__main__":
    main()