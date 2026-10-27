# tlr — TikTok Live Recorder (lean rewrite)

Ein schlanker Ersatz für [Michele0303/TikTok-Live-Recorder](https://github.com/Michele0303/TikTok-Live-Recorder).
Eine Datei, nur Standardbibliothek + `ffmpeg`-Binary — dafür ohne die Bug-Klassen, die das Original seit Jahren begleiten.

```bash
python3 tlr.py -user irgendeinaccount
```

---

## Warum der Rewrite

Das Original lädt den FLV-Stream als Byte-Strom durch Python in eine Datei und konvertiert danach. Das koppelt jede Netzwerkstörung an die Aufnahme, hat keinen Read-Timeout, keine Stall-Erkennung, zerlegt die Datei bei Ctrl-C und greift gelegentlich die Audio-only-Variante.

Die häufigsten offenen Issues und was hier anders ist:

| Problem im Original | Ursache | Lösung hier |
|---|---|---|
| `JSONDecodeError` / „expecting value line 1" | `.json()` auf jede Antwort, auch auf WAF-Seiten | jede Antwort läuft durch `Http.get_json`, Nicht-JSON = retrybarer Zustand |
| Room-ID wird nicht gefunden | nur ein Signing-Dienst | drei Quellen nacheinander: signed API → webcast → Live-Seite |
| „alle User sind online" | `check_alive` wird geglaubt | Stream-URL wird auf echte Bytes geprüft, bevor aufgenommen wird |
| Recorder hängt, stoppt nicht | Download ohne Timeout, Live-Check erst nach Verbindungsabbau | `-rw_timeout` + Größen-Watchdog + Live-Check alle 30 s |
| Bitrate ~1000 kbps, Qualitätsverlust | `-bitrate` erzwingt Re-Encode | `-c copy` in MPEG-TS, Original-Bitrate bleibt |
| Datei ist audio-only | Qualitätsauswahl nimmt `ao`-Stream mit | Audio-Varianten werden aussortiert |
| Absturz ohne MP4, Endlosschleife bei Ctrl-C | Konvertierung im Fehlerpfad, multiprocessing | Ctrl-C = sauberer Stopp, TS bleibt liegen wenn Remux scheitert |

## Voraussetzungen

* Python 3.9+ (keine Pakete nötig)
* `ffmpeg` im PATH (getestet mit 7.1)

```bash
sudo apt install ffmpeg        # Debian/Ubuntu
brew install ffmpeg            # macOS
pkg install ffmpeg             # Termux
```

Kopieren, fertig:

```bash
git clone git@github.com:cens0r3d/tiktok-live-lite.git
cd tiktok-live-lite
python3 tlr.py --check -user aljazeeraenglish
```

## Benutzung

```bash
# jetzt aufnehmen, bis der Stream endet
python3 tlr.py -user account

# nur schauen: ist live, welche Qualitäten liefert der Stream?
python3 tlr.py --check -user account

# dauerhaft lauschen, alle 3 Minuten prüfen
python3 tlr.py -user account -mode watch -interval 3

# mehrere Accounts (je Prozess einer) in ein Zielverzeichnis
python3 tlr.py -user a -output /data/live &
python3 tlr.py -user b -output /data/live &

# 15 Minuten mitschneiden, kleinste Qualität, nur Ton
python3 tlr.py -user account -duration 900 -quality worst
python3 tlr.py -user account --audio-only

# per URL oder Room-ID
python3 tlr.py -url https://www.tiktok.com/@account/live
python3 tlr.py -room_id 7699631452921154317
```

### Optionen

| Flag | Bedeutung |
|---|---|
| `-user`, `--user` | Username (ohne `@`), mehrere mit Komma |
| `-url` | TikTok-Live-URL |
| `-room_id` | Room-ID direkt |
| `-mode manual\|watch` | `manual` = jetzt aufnehmen, `watch` = dauerhaft lauschen (Default: `manual`) |
| `-interval` | Minuten zwischen Checks im Watch-Modus (Default: 5, Alias `-automatic_interval`) |
| `-output` | Zielverzeichnis (Default: aktuelles Verzeichnis) |
| `-duration` | maximale Aufnahmedauer in Sekunden |
| `-quality` | `best` (Default), `worst`, oder Label/Auflösung wie `sd`, `720p` |
| `-proxy` | HTTP-Proxy, z. B. `http://127.0.0.1:8080` |
| `-cookies` | `cookies.json` (Dict) oder Netscape `cookies.txt` |
| `-ffmpeg-path` | eigener ffmpeg-Pfad |
| `-bitrate` | Re-Encode mit dieser Bitrate — kostet Qualität, nur wenn nötig |
| `--audio-only` | nur Tonspur, Ausgabe `.m4a` |
| `--check` | nur Status und Qualitäten ausgeben, nichts aufnehmen |
| `--stall-timeout` | Sekunden ohne Datenwachstum bis Reconnect (Default: 25) |
| `--live-check` | Sekunden zwischen Live-Checks während der Aufnahme (Default: 30) |
| `--max-reconnects` | maximale Reconnects pro Aufnahme (Default: 20) |
| `--keep-parts` | rohe `.ts`-Segmente behalten |
| `-q` | weniger Ausgabe |

### Exit-Codes

| Code | Bedeutung |
|---|---|
| 0 | Aufnahme gespeichert (oder `--check`: live) |
| 1 | Auflösung/Setup fehlgeschlagen (User unbekannt, kein ffmpeg, Netz tot) |
| 2 | User ist nicht live |
| 3 | live erkannt, aber kein Stream-Datenfluss |
| 130 | per Ctrl-C beendet |

## Wie es funktioniert

```
User ──► signierte API ──► room_id + status + stream-URLs
              │ (blockiert / kein JSON)
              ├─► webcast room/info (wenn room_id bekannt)
              └─► Live-Seite (HTML-Scrape)
                        │
                        ▼
              URL-Probe: kommen echte Bytes?
                        │
                        ▼
      ffmpeg -c copy → MPEG-TS  ◄── Watchdog (Größe, Timeout, Live-Check)
                        │                  │ Stall/Abbruch
                        │                  └─► neues Segment, neue URL
                        ▼
        concat + Remux → MP4 (faststart)  |  Fallback: .ts bleibt liegen
```

Ein Aufnahme-Durchlauf kann aus mehreren Segmenten bestehen (`_part1.ts`, `_part2.ts`, …). Sie werden am Ende zusammengefügt und zu MP4 gemuxt; erst wenn der Remux nachweislich eine Videospur enthält, werden die Segmente gelöscht. Schlägt der Remux fehl, bleibt die Aufnahme als TS erhalten — es geht nichts verloren.

Dateiname: `TK_<user>_<YYYY.MM.DD_HH-MM-SS>.mp4`

### Cookies

Werden nur gebraucht, wenn TikTok die IP blockt (WAF) oder der Stream privat ist. Entweder `cookies.json` neben das Skript legen:

```json
{ "sessionid": "...", "tt_chain_token": "...", "tt_csrf_token": "..." }
```

oder `-cookies pfad/cookies.txt` (Netscape-Format aus der Browser-Extension). Die Cookies gehen an die HTTP-Aufrufe **und** als Header an ffmpeg.

### Dauerbetrieb

`tmux`/`screen` oder systemd:

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
sudo systemctl enable --now tlr@irgendeinaccount
journalctl -u tlr@irgendeinaccount -f
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

25 Tests, kein Netzwerk nötig: Parsing/Qualitätsauswahl, HTTP-Guards (HTML statt JSON, kaputtes JSON, leerer Body), Resolver-Pfade (offline, unbekannt, WAF-Fallback), CLI, Cookies — und ein lokaler Fake-CDN, der nach 55 % der Daten hängt, um Stall-Erkennung und Reconnect zu beweisen.

## Grenzen

* `-mode followers` (alle gefolgten Accounts) gibt es hier nicht — dafür bräuchte es Cookies, Sec-UID-Paginierung und einen Prozess pro Account. Für mehrere Accounts: je ein Prozess.
* Kein Telegram-Upload. Stattdessen Datei am Ende per `-output`-Verzeichnis weiterverarbeiten (z. B. `inotifywait` + `rclone`).
* Kein Auto-Update, keine Selbstinstallation von Abhängigkeiten.

## Rechtliches

Nicht mit TikTok verbunden. Aufnehmen nur, was du aufnehmen darfst. Nutzung auf eigenes Risiko. Lizenz: MIT (wie das Original, von dem die Idee und die CLI-Konventionen stammen).
