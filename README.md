# tlr — TikTok LIVE Recorder (lean rewrite)

A lean replacement for [Michele0303/TikTok-Live-Recorder](https://github.com/Michele0303/TikTok-Live-Recorder).
One file, standard library only plus the `ffmpeg` binary — without the classes of bugs the original has carried for years.

```bash
python3 tlr.py -user someaccount
```

---

## Why the rewrite

The original streams FLV bytes through Python into a file and re-muxes afterwards. That couples every network hiccup to the recording, has no read timeout, no stall detection, garbles the file on Ctrl-C and occasionally picks the audio-only variant.

The most common open issues and what is different here:

| Problem in the original | Cause | Fix here |
|---|---|---|
| `JSONDecodeError` / "expecting value line 1" | `.json()` on every response, including WAF pages | every response goes through `Http.get_json`, non-JSON is a retryable state |
| Room ID not found | only one signing service | three sources in sequence: signed API → webcast → live page |
| "everyone is online" | `check_alive` is trusted | the stream URL is probed for real bytes before recording starts |
| Recorder hangs and never stops | download without timeout, live check only after the connection drops | `-rw_timeout` + size watchdog + live check every 30 s |
| Bitrate ~1000 kbps, quality loss | `-bitrate` forces a re-encode | `-c copy` into MPEG-TS, original bitrate preserved |
| File is audio-only | quality selection picks up the `ao` stream | audio variants are filtered out |
| Crash without MP4, endless loop on Ctrl-C | conversion in the error path, multiprocessing | Ctrl-C stops cleanly, the TS file stays if the remux fails |

## Requirements

* Python 3.9+ (no packages needed)
* `ffmpeg` in `PATH` (tested with 7.1)

```bash
sudo apt install ffmpeg        # Debian/Ubuntu
brew install ffmpeg            # macOS
pkg install ffmpeg             # Termux
```

Copy it, done:

```bash
git clone https://github.com/cens0r3d/tiktok-live-lite.git
cd tiktok-live-lite
python3 tlr.py --check -user aljazeeraenglish
```

## Usage

```bash
# record now until the stream ends
python3 tlr.py -user account

# just look: is the account live, which qualities does the stream offer?
python3 tlr.py --check -user account

# listen permanently, check every 3 minutes
python3 tlr.py -user account -mode watch -interval 3

# several accounts (one process each) into a target directory
python3 tlr.py -user a -output /data/live &
python3 tlr.py -user b -output /data/live &

# record for 15 minutes, lowest quality, audio only
python3 tlr.py -user account -duration 900 -quality worst
python3 tlr.py -user account --audio-only

# by URL or room ID
python3 tlr.py -url https://www.tiktok.com/@account/live
python3 tlr.py -room_id 7699631452921154317
```

### Options

| Flag | Meaning |
|---|---|
| `-user`, `--user` | Username (without `@`), several separated by commas |
| `-url` | TikTok live URL |
| `-room_id` | Room ID directly |
| `-mode manual\|watch` | `manual` = record now, `watch` = listen permanently (default: `manual`) |
| `-interval` | Minutes between checks in watch mode (default: 5, alias `-automatic_interval`) |
| `-output` | Target directory (default: current directory) |
| `-duration` | Maximum recording length in seconds |
| `-quality` | `best` (default), `worst`, or a label/resolution such as `sd`, `720p` |
| `-proxy` | HTTP proxy, e.g. `http://127.0.0.1:8080` |
| `-cookies` | `cookies.json` (dict) or Netscape `cookies.txt` |
| `-cookie` | Cookies directly as a string, e.g. `"sessionid=...; tt_chain_token=..."` |
| `-ffmpeg-path` | Custom ffmpeg path |
| `-bitrate` | Re-encode at this bitrate — costs quality, only if needed |
| `--audio-only` | Audio track only, output `.m4a` |
| `--check` | Print status and qualities only, record nothing |
| `--stall-timeout` | Seconds without data growth before reconnecting (default: 25) |
| `--live-check` | Seconds between live checks while recording (default: 30) |
| `--max-reconnects` | Maximum reconnects per recording (default: 20) |
| `--keep-parts` | Keep the raw `.ts` segments |
| `-q` | Less output |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Recording saved (or `--check`: live) |
| 1 | Resolution/setup failed (unknown user, no ffmpeg, network down) |
| 2 | User is not live |
| 3 | Live detected, but no stream data flowing |
| 130 | Stopped by Ctrl-C |

## How it works

```
User ──► signed API ──► room_id + status + stream URLs
             │ (blocked / no JSON)
             ├─► webcast room/info (when the room_id is known)
             └─► live page (HTML scrape)
                       │
                       ▼
             URL probe: are real bytes coming?
                       │
                       ▼
     ffmpeg -c copy → MPEG-TS  ◄── watchdog (size, timeout, live check)
                       │                  │ stall/abort
                       │                  └─► new segment, new URL
                       ▼
       concat + remux → MP4 (faststart)  |  fallback: .ts stays on disk
```

One recording run can consist of several segments (`_part1.ts`, `_part2.ts`, …). They are concatenated at the end and remuxed to MP4; the segments are only deleted once the remux demonstrably contains a video stream. If the remux fails, the recording stays on disk as TS — nothing is lost.

Filename: `TK_<user>_<YYYY.MM.DD_HH-MM-SS>.mp4`

### Cookies (private accounts)

You need cookies in two cases: TikTok blocks the IP (WAF), or the account is **private**.

For a private account TikTok answers "live", but without a room ID, without a stream URL and with `followers=0` — that data only goes to logged-in followers. The tool now says exactly that:

```
[x] @example is live, but it is a private account: TikTok only hands out the
    room/stream URL to logged-in followers (followers=0).
```

Three ways to pass cookies:

```bash
# 1. file (dict or Netscape format)
python3 tlr.py -user x -cookies cookies.txt

# 2. directly as a string
python3 tlr.py -user x -cookie "sessionid=abc123; tt_chain_token=def456"

# 3. drop cookies.json next to tlr.py (loaded automatically)
```

Important: the cookies must come from an account **that follows the private account** — otherwise the answer stays empty. To get them: log in at tiktok.com in the browser → DevTools → Application → Cookies → copy `sessionid`, `tt_chain_token`, `tt_csrf_token`; or use an extension that exports `cookies.txt` in Netscape format.

The cookies go into every HTTP call **and** as a header to ffmpeg (`-headers`), so CDN access is authenticated too.

`--check` shows you what TikTok actually answered:

```
diag: status=2 secret=False followers=16712829 cookies=0
```

`secret=True` or `followers=0` on an account reported as live means private, not "offline".

### Daemon mode

`tmux`/`screen` or systemd:

```ini
# /etc/systemd/system/tlr@.service
[Unit]
Description=TikTok Live Recorder (%i)
After=network-online.target

[Service]
User=stoned
WorkingDirectory=/home/stoned/tiktok-live-lite
ExecStart=/usr/bin/python3 tlr.py -user %i -mode watch -output /data/live
Restart=always
RestartSec=30
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tlr@someaccount
journalctl -u tlr@someaccount -f
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

28 tests, no network required: parsing/quality selection, HTTP guards (HTML instead of JSON, broken JSON, empty body), resolver paths (offline, unknown, WAF fallback), CLI, cookies — and a local fake CDN that stalls after 55 % of the data to prove stall detection and reconnect.

## Limits

* `-mode followers` (all followed accounts) does not exist here — that would need cookies, Sec-UID pagination and one process per account. For several accounts: one process each.
* No Telegram upload. Instead, process the file from the `-output` directory afterwards (e.g. `inotifywait` + `rclone`).
* No auto-update, no self-installation of dependencies.

## Legal

Not affiliated with TikTok. Record only what you are allowed to record. Use at your own risk. License: MIT (like the original, which the idea and the CLI conventions come from).
