# Train Station Announcer

A Python script that automatically fetches live train departure data from the [kalkulatorkolejowy.pl](https://kalkulatorkolejowy.pl) API and announces upcoming departures over speakers using Microsoft Edge TTS — just like a real station public address system.

## How it works

1. Every few minutes the script fetches departure data for a configured station from the API.
2. Fetched trains are filtered, deduplicated, and sorted by actual departure time.
3. A departure announcement is spoken **3 minutes before** each train's actual departure time.
4. If a train is delayed, a **delay reminder** is spoken every 5 minutes (up to 30 minutes before departure, and again up to 60 minutes before if still delayed).
5. Every poll cycle, a **departure board** is printed to the console showing all upcoming trains.

All announcement text is synthesized using [edge-tts](https://github.com/rany2/edge-tts) (Microsoft Neural TTS), which requires an internet connection but no API key.

## Requirements

Python 3.10 or newer, plus one pip package:

```
pip install edge-tts
```

For audio playback, one of the following is recommended:

| Platform | Recommended player |
|----------|--------------------|
| Windows  | [ffmpeg](https://ffmpeg.org/download.html) (add `ffplay` to PATH) — optional, PowerShell fallback is built-in |
| Linux    | `sudo apt install ffmpeg` or `sudo apt install mpg123` |
| macOS    | `afplay` is built-in; ffmpeg optional for volume control |

> **Note:** Without ffmpeg, the `volume` setting in `settings.ini` has no effect on Windows.

## Files

```
announcer/
├── pipertts.py       # main script
├── settings.ini      # configuration
├── jingle.wav        # or jingle.mp3 — played before each announcement
└── README.md
```

## Configuration — settings.ini

### [general]

| Key | Description | Default |
|-----|-------------|---------|
| `station` | Station ID from kalkulatorkolejowy.pl (BILKOM) | *(required)* |
| `poll_seconds` | How often to fetch data from the API (minimum: 300) | `300` |
| `lookahead_minutes` | How far ahead to load departures | `120` |

### [jingle]

| Key | Description |
|-----|-------------|
| `file` | Path to the jingle file, relative to the script. Supports `.wav` and `.mp3`. |

### [audio]

| Key | Description | Default |
|-----|-------------|---------|
| `volume` | Playback volume, 0–100. Requires ffplay to be installed. | `80` |
| `pause_after_jingle_ms` | Pause between the jingle and the spoken announcement, in milliseconds. | `200` |

### [tts]

| Key | Description | Default |
|-----|-------------|---------|
| `voice` | Edge TTS voice name. See [available voices](https://github.com/rany2/edge-tts#available-voices). | `pl-PL-ZofiaNeural` |
| `rate` | Speech rate adjustment, e.g. `+10%`, `-20%`. | `+0%` |

Available Polish voices: `pl-PL-ZofiaNeural` (female), `pl-PL-MarekNeural` (male).

### [messages]

Announcement templates. All three keys are optional — if the section is absent, built-in Polish defaults are used.

| Key | When used |
|-----|-----------|
| `departure_on_time` | Train is on time (delay ≤ 4 min) |
| `departure_delayed` | Train is delayed (delay > 4 min) |
| `delay_reminder` | Periodic delay reminder |

Available template variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `{train_code}` | Train number as returned by the API | `IC 5424` |
| `{dest}` | Destination station name | `Kraków Główny` |
| `{track}` | Track number | `7` |
| `{platform}` | Platform number/letter | `III` |
| `{delay_min}` | Delay in minutes, rounded to 5 | `10` |
| `{scheduled_time}` | Planned departure time (HH:MM) | `14:34` |
| `{departure_time}` | Actual departure time incl. delay (HH:MM) | `14:44` |

Example:

```ini
[messages]
departure_on_time =
    Attention. Train {train_code} to {dest}.
    Departing from track {track}, platform {platform} at {scheduled_time}.

departure_delayed =
    Attention. Train {train_code} to {dest}.
    Departing from track {track}, platform {platform}.
    This train is delayed by {delay_min} minutes. Expected departure: {departure_time}.

delay_reminder =
    Delay notice. Train {train_code} to {dest} is {delay_min} minutes late.
```

### [pronunciation]

A simple find-and-replace map applied to the announcement text **before** it is sent to TTS. Useful for fixing foreign station names, train operator abbreviations, or track/platform numbers.

```ini
[pronunciation]
Praha hl.n. = Praha Hlavni Nadrazi
IC = Intercity
toru 1 = toru pierwszego
toru 2 = toru drugiego
peronie I = peronie pierwszym
peronie II = peronie drugim
```

**Rules:**
- Replacements are applied longest-first, so `peronie II` is always replaced before `peronie I`.
- Matching is token-aware: `IC` will not match inside `EIC` or other longer tokens.
- Key casing is preserved exactly as written.

> **Tip:** edge-tts has no SSML phoneme support for Polish, so unusual foreign words or abbreviations may need phonetic spelling in this section (e.g. `München Hbf = Munchen Hauptbankhof`).

## Running

```
python tts.py
```

Press **Ctrl+C** to stop.

## Console output

On each poll the script prints a departure board:

```
[14:32:05] Departure board (8 trains):
  Departure  Train        Destination                    Track  Platform  Delay
  ---------- ------------ ------------------------------ ------ --------- -----
  14:34      IC 5424      Racibórz                       11     IV        +5 min
  14:37      IC 55        Kraków Główny                  5      II        +35 min
  14:44      IC 417       München Hbf                    7      III       -
  ...
```

Announcements are logged as they are queued:

```
[14:31:12] Announcement: Uwaga. Pociąg spółki IC 5424 do stacji Racibórz. Odjedzie z toru 11, przy peronie IV.
[14:31:12] Delay: Informacja o opóźnieniu. Pociąg IC 55 do stacji Kraków Główny jest opóźniony o 35 minut.
```

## Authors

matpl11, Bartlomiej Malarz and others specified in the Git repository. All rights reserved according to the license.
