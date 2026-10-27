#!/usr/bin/env python3
"""
tlr — a lean TikTok LIVE recorder.

Why this exists (the short version):
the reference implementation streams FLV bytes through Python into a file and
then re-muxes. That couples every network hiccup to the recording, has no read
timeout, no stall detection, garbles the file on Ctrl-C and picks audio-only
variants. This rewrite moves the download into ffmpeg (`-c copy` into MPEG-TS),
keeps the network/JSON work in one guarded place and adds a watchdog.

Design rules:
  1. one file, stdlib only, ffmpeg as the only external binary
  2. every network answer is parsed defensively — a broken/blocked response is
     a retryable condition, never a traceback (no more JSONDecodeError spam)
  3. room + stream URLs come from one signed endpoint, with three fallbacks
  4. record with `-c copy` into .ts -> original bitrate, playable while writing
  5. watchdog: stall detection, reconnect into a new segment, periodic live
     check -> clean stop. Never wait forever on a dead socket.
  6. finalise to MP4, and never delete the raw parts unless the MP4 is valid

Usage:
    python3 tlr.py -user someaccount
    python3 tlr.py -user someaccount -mode watch -interval 3
    python3 tlr.py -url https://www.tiktok.com/@someone/live -duration 600
    python3 tlr.py -user someone --check        # status + available qualities
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "1.0.0"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

BASE = "https://www.tiktok.com"
SIGN_API = "https://tikrec.com/tiktok/room/api/sign"
WEBCAST = "https://webcast.tiktok.com"

# room status: 2 = live, 4 = offline. Some regions/rooms report 3 while the
# stream is paused, which still counts as live: we keep recording and let the
# watchdog reconnect when data flows again.
STATUS_LIVE = (2, 3)

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
    "Origin": BASE,
}


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #


class Log:
    quiet = False

    @staticmethod
    def _emit(tag: str, msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} [{tag}] {msg}", flush=True)

    @classmethod
    def info(cls, msg: str) -> None:
        cls._emit("+", msg)

    @classmethod
    def warn(cls, msg: str) -> None:
        cls._emit("!", msg)

    @classmethod
    def error(cls, msg: str) -> None:
        cls._emit("x", msg)

    @classmethod
    def debug(cls, msg: str) -> None:
        if not cls.quiet:
            cls._emit(".", msg)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class TlrError(Exception):
    """Fatal, user-facing error."""


class Retryable(Exception):
    """Something that may work on the next attempt."""


class Offline(Retryable):
    """User exists but is not live right now."""


class Blocked(Retryable):
    """TikTok/WAF or the signing service refused us."""


class NotFound(TlrError):
    """Username/room does not exist."""


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #


class Http:
    """Small urllib wrapper: browser-ish headers, timeouts, retries, cookies."""

    def __init__(self, proxy: str | None = None, cookies: dict[str, str] | None = None):
        handlers: list[Any] = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self.opener = urllib.request.build_opener(*handlers)
        self.cookies = cookies or {}
        self.headers = dict(DEFAULT_HEADERS)
        if self.cookies:
            self.headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _request(self, url: str, timeout: float, headers: dict[str, str] | None = None):
        hdrs = dict(self.headers)
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, headers=hdrs)
        return self.opener.open(req, timeout=timeout)

    def get_text(
        self,
        url: str,
        timeout: float = 15.0,
        tries: int = 3,
        headers: dict[str, str] | None = None,
    ) -> str:
        """GET and return decoded body. Raises Blocked/Retryable after retries."""
        last: Exception | None = None
        for attempt in range(1, tries + 1):
            try:
                with self._request(url, timeout, headers) as r:
                    raw = r.read()
                text = raw.decode("utf-8", "replace")
                if not text.strip():
                    raise Retryable("empty response body")
                # TikTok answers WAF challenges with a redirect page or a
                # "Please wait" interstitial instead of JSON/HTML
                if "Please wait" in text[:2000] or "verify" in text[:200].lower():
                    raise Blocked("WAF challenge page")
                return text
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (403, 429):
                    last = Blocked(f"HTTP {e.code}")
            except Blocked as e:
                last = e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
            except Retryable as e:
                last = e
            if attempt < tries:
                delay = 0.6 * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
                Log.debug(f"retry {attempt}/{tries - 1} in {delay:.1f}s ({last})")
                time.sleep(delay)
        if isinstance(last, Blocked):
            raise last
        raise Retryable(str(last) if last else "request failed")

    def get_json(self, url: str, timeout: float = 15.0, tries: int = 3) -> dict:
        """GET + parse JSON. A non-JSON body is a retryable condition, not a crash."""
        text = self.get_text(url, timeout=timeout, tries=tries)
        try:
            data = json.loads(text)
        except ValueError:
            snippet = text[:120].replace("\n", " ")
            raise Retryable(f"non-JSON response ({snippet!r})") from None
        if not isinstance(data, dict):
            raise Retryable("unexpected JSON shape")
        return data


# --------------------------------------------------------------------------- #
# stream model
# --------------------------------------------------------------------------- #


@dataclass
class Stream:
    url: str
    label: str          # hd / sd / origin / ao
    bitrate: int        # bits/s, 0 when unknown
    resolution: str
    is_video: bool

    def describe(self) -> str:
        if not self.is_video:
            return f"audio-only ({self.bitrate // 1000} kbps)"
        res = self.resolution or "?"
        return f"{self.label} {res} ~{self.bitrate // 1000} kbps"


@dataclass
class Live:
    user: str
    room_id: str
    title: str = ""
    streams: list[Stream] = field(default_factory=list)
    source: str = "signed"


def _first_int(*vals: Any) -> int:
    for v in vals:
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return 0


def _parse_stream_object(obj: dict) -> list[Stream]:
    """Turn the pull_data blob into an ordered list of usable streams."""
    out: list[Stream] = []
    data = obj.get("data")
    if not isinstance(data, dict):
        return out
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        for which in ("main", "backup"):
            part = entry.get(which)
            if not isinstance(part, dict):
                continue
            url = part.get("flv") or part.get("hls") or part.get("m3u8") or ""
            if not url:
                continue
            params: dict = {}
            raw_params = part.get("sdk_params")
            if isinstance(raw_params, str):
                try:
                    params = json.loads(raw_params)
                except ValueError:
                    params = {}
            elif isinstance(raw_params, dict):
                params = raw_params
            resolution = str(params.get("resolution") or "")
            bitrate = _first_int(params.get("vbitrate"), params.get("v_rtbitrate"))
            is_video = bool(resolution) and "only_audio" not in url
            label = str(key)
            out.append(
                Stream(
                    url=url,
                    label=label,
                    bitrate=bitrate,
                    resolution=resolution,
                    is_video=is_video,
                )
            )
    # best quality first; the backup CDN stays reachable as a fallback
    out.sort(key=lambda s: (s.is_video, s.bitrate), reverse=True)
    return out


def _streams_from_pull_data(pull: dict) -> list[Stream]:
    streams: list[Stream] = []
    raw = pull.get("stream_data")
    obj: dict | None = None
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
    elif isinstance(raw, dict):
        obj = raw
    if isinstance(obj, dict):
        streams = _parse_stream_object(obj)
    if streams:
        return streams

    # legacy shape: flv_pull_url map + single hls/rtmp keys
    legacy = pull.get("flv_pull_url")
    if isinstance(legacy, dict):
        for key in ("FULL_HD1", "HD1", "SD2", "SD1", "SD"):
            url = legacy.get(key)
            if url:
                streams.append(
                    Stream(url=url, label=key, bitrate=0, resolution="", is_video=True)
                )
    hls = pull.get("hls_pull_url") or pull.get("hls_pull_url_map")
    if isinstance(hls, str) and hls:
        streams.append(Stream(url=hls, label="hls", bitrate=0, resolution="", is_video=True))
    elif isinstance(hls, dict):
        for key, url in hls.items():
            streams.append(
                Stream(url=url, label=f"hls:{key}", bitrate=0, resolution="", is_video=True)
            )
    return streams


# --------------------------------------------------------------------------- #
# resolving: user -> room -> stream urls
# --------------------------------------------------------------------------- #


class Resolver:
    def __init__(self, http: Http):
        self.http = http

    # -- source 1: signed api (works without cookies, returns both room + urls)
    def _from_signed(self, user: str) -> Live:
        try:
            sign = self.http.get_json(
                f"{SIGN_API}?unique_id={urllib.parse.quote(user)}", tries=2
            )
        except Retryable as e:
            raise Blocked(f"signing service: {e}") from None

        path = sign.get("signed_path")
        if not path:
            raise Blocked("signing service returned no signed_path")

        payload = self.http.get_json(BASE + path, tries=2)
        data = payload.get("data")
        if not isinstance(data, dict):
            msg = str(payload.get("message") or "")
            if "not_found" in msg or payload.get("statusCode") == 19881007:
                raise NotFound(f"@{user} does not exist")
            raise Retryable(f"signed api: {msg or 'no data'}")

        u = data.get("user") if isinstance(data.get("user"), dict) else {}
        room = data.get("liveRoom") if isinstance(data.get("liveRoom"), dict) else {}
        room_id = str(u.get("roomId") or "") or str(room.get("id") or "")
        status = _first_int(room.get("status"), u.get("status"))

        if status and status not in STATUS_LIVE:
            raise Offline(f"@{user} is not live (status {status})")
        if not room_id:
            raise Offline(f"@{user} is not live (no room)")

        stream_data = room.get("streamData") if isinstance(room.get("streamData"), dict) else {}
        pull = stream_data.get("pull_data") if isinstance(stream_data.get("pull_data"), dict) else {}
        streams = _streams_from_pull_data(pull) if pull else []
        if not streams:
            # room is live but the payload has no urls -> ask the webcast room
            streams = self._streams_from_room_info(room_id)

        return Live(
            user=str(u.get("uniqueId") or user),
            room_id=room_id,
            title=str(room.get("title") or ""),
            streams=streams,
            source="signed",
        )

    # -- source 2: webcast room info (needs a room_id)
    def _streams_from_room_info(self, room_id: str) -> list[Stream]:
        payload = self.http.get_json(
            f"{WEBCAST}/webcast/room/info/?aid=1988&room_id={room_id}", tries=2
        )
        code = _first_int(payload.get("status_code"))
        if code == 4003110:
            raise Blocked("webcast blocked this IP (4003110)")
        if code != 0:
            raise Retryable(f"webcast status_code {code}")
        room = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if str(room.get("status")) not in ("2", "3", ""):
            raise Offline("room is closed")
        return _streams_from_pull_data(room.get("stream_url") or {})

    def resolve_room(self, room_id: str) -> Live:
        payload = self.http.get_json(
            f"{WEBCAST}/webcast/room/info/?aid=1988&room_id={room_id}", tries=2
        )
        if _first_int(payload.get("status_code")) == 4003110:
            raise Blocked("webcast blocked this IP (4003110)")
        room = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        owner = room.get("owner") if isinstance(room.get("owner"), dict) else {}
        if not room:
            raise NotFound(f"room {room_id} not found")
        return Live(
            user=str(owner.get("display_id") or room_id),
            room_id=str(room_id),
            title=str(room.get("title") or ""),
            streams=_streams_from_pull_data(room.get("stream_url") or {}),
            source="webcast",
        )

    # -- source 3: live page html (last resort, no signing needed)
    def _from_page(self, user: str) -> Live:
        html = self.http.get_text(f"{BASE}/@{urllib.parse.quote(user)}/live", tries=2)
        room_id = ""
        m = re.search(r'"roomId":"(\d+)"', html) or re.search(r'roomId\\?":\\?"(\d{15,})', html)
        if m:
            room_id = m.group(1)
        urls = re.findall(r'https?://[^\s"\'<>\\]+\.flv[^\s"\'<>\\]*', html)
        streams = [
            Stream(url=u.replace("\\u002F", "/"), label="page", bitrate=0, resolution="", is_video="only_audio" not in u)
            for u in dict.fromkeys(urls)
        ]
        live_flag = '"status":2' in html or '"statusCode":2' in html
        if not room_id and not streams:
            raise Offline(f"@{user} is not live (no room in page)")
        if not live_flag and not streams:
            raise Offline(f"@{user} is not live (page status)")
        return Live(user=user, room_id=room_id, streams=streams, source="page")

    def resolve(self, user: str | None, room_id: str | None, url: str | None) -> Live:
        """Resolve any of user/room_id/url into a Live with usable stream URLs."""
        if url:
            user = parse_user_from_url(url) or user
        errors: list[str] = []

        if room_id:
            try:
                live = self.resolve_room(room_id)
                if live.streams:
                    return live
                errors.append("webcast: no stream urls")
            except Retryable as e:
                errors.append(f"webcast: {e}")
        if user:
            for attempt, fn in enumerate((self._from_signed, self._from_page), start=1):
                if attempt > 1:
                    time.sleep(1.0)
                try:
                    live = fn(user)
                    if live.streams:
                        return live
                    errors.append(f"{live.source}: no stream urls")
                except NotFound:
                    raise
                except Offline as e:
                    # trustworthy "not live" answers end the search for this round
                    raise
                except Retryable as e:
                    errors.append(f"{live_source_name(fn)}: {e}")
        if not user and not room_id:
            raise TlrError("need one of -user, -url or -room_id")
        raise Retryable("; ".join(errors) or "resolution failed")


def live_source_name(fn) -> str:
    return "signed" if fn.__name__ == "_from_signed" else "page"


def parse_user_from_url(url: str) -> str | None:
    m = re.search(r"tiktok\.com/@([^/?#]+)", url)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# stream validation
# --------------------------------------------------------------------------- #


def probe_stream(http: Http, stream: Stream, timeout: float = 10.0) -> bool:
    """Open the stream and require real bytes. Fixes 'everyone looks live' reports:
    a stale room can hand out URLs that no longer carry data."""
    req_headers = dict(DEFAULT_HEADERS)
    req_headers["Range"] = "bytes=0-65535"
    req = urllib.request.Request(stream.url, headers=req_headers)
    try:
        with http.opener.open(req, timeout=timeout) as r:
            chunk = r.read(4096)
    except Exception as e:  # noqa: BLE001 - any failure means "not usable"
        Log.debug(f"probe failed: {e}")
        return False
    return len(chunk) > 512


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #


class Recording:
    """One recording session (may consist of several ffmpeg segments)."""

    def __init__(self, cfg: "Config", live: Live, stream: Stream, out_dir: Path):
        self.cfg = cfg
        self.live = live
        self.stream = stream
        self.out_dir = out_dir
        self.stem = f"TK_{live.user}_{time.strftime('%Y.%m.%d_%H-%M-%S')}"
        self.parts: list[Path] = []
        self.stop = threading.Event()
        self.hard_stop = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.started = time.time()
        self.written = 0

    # -- process handling ---------------------------------------------------
    def _ffmpeg_cmd(self, target: Path) -> list[str]:
        cmd = [
            self.cfg.ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostats",
            "-y",
            "-rw_timeout", str(int(self.cfg.stall_timeout * 1_000_000)),
            "-user_agent", UA,
            "-headers", "Referer: https://www.tiktok.com/\r\n",
        ]
        if self.cfg.cookie_header:
            cmd[-1] += f"Cookie: {self.cfg.cookie_header}\r\n"
        cmd += ["-i", self.stream.url]
        if self.cfg.audio_only:
            cmd += ["-vn", "-c:a", "copy"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-f", "mpegts", str(target)]
        return cmd

    def _start(self, index: int) -> Path:
        target = self.out_dir / f"{self.stem}_part{index}.ts"
        cmd = self._ffmpeg_cmd(target)
        Log.debug("ffmpeg " + " ".join(cmd[:6]) + " ...")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return target

    def _stop_ffmpeg(self, hard: bool = False) -> None:
        p = self.proc
        if p is None or p.poll() is not None:
            return
        try:
            if hard:
                p.kill()
            else:
                p.stdin.write(b"q")  # graceful: lets ffmpeg finish the muxer
                p.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self._close_pipes(p)

    @staticmethod
    def _close_pipes(p: subprocess.Popen) -> None:
        for pipe in (p.stdin, p.stdout, p.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    # -- one segment --------------------------------------------------------
    def _segment(self, index: int) -> tuple[bool, str]:
        """Run one ffmpeg segment. Returns (wants_retry, reason)."""
        target = self._start(index)
        size = 0
        last_change = time.time()
        next_live_check = time.time() + self.cfg.live_check

        try:
            while True:
                if self.hard_stop.is_set():
                    self._stop_ffmpeg(hard=True)
                    return False, "aborted"
                if self.stop.is_set():
                    self._stop_ffmpeg()
                    return False, "stopped by user"
                if self.cfg.duration and time.time() - self.started >= self.cfg.duration:
                    Log.info(f"duration limit reached ({self.cfg.duration}s)")
                    self._stop_ffmpeg()
                    return False, "duration"

                rc = self.proc.poll() if self.proc else 0
                if rc is not None:
                    err = ""
                    try:
                        err = (self.proc.stderr.read() or b"").decode("utf-8", "replace").strip()
                    except Exception:  # noqa: BLE001
                        pass
                    if target.exists():
                        size = max(size, target.stat().st_size)
                    if size > 0:
                        self.parts.append(target)
                        self.written += size
                    if rc == 0 and size > 0:
                        return False, "stream ended"
                    Log.warn(f"ffmpeg exited ({rc}) {('after ' + human(size)) if size else 'without data'}"
                             + (f": {err.splitlines()[-1][:160]}" if err else ""))
                    self._close_pipes(self.proc)
                    return True, "ffmpeg exited"

                if target.exists():
                    cur = target.stat().st_size
                    if cur != size:
                        size = cur
                        last_change = time.time()
                if time.time() - last_change > self.cfg.stall_timeout:
                    Log.warn(f"no data for {self.cfg.stall_timeout}s — reconnecting")
                    self._stop_ffmpeg(hard=True)
                    if target.exists() and target.stat().st_size > 0:
                        self.parts.append(target)
                        self.written += target.stat().st_size
                    return True, "stalled"

                if time.time() >= next_live_check:
                    next_live_check = time.time() + self.cfg.live_check
                    if not self._still_live():
                        Log.info("stream ended (room is offline)")
                        self._stop_ffmpeg()
                        return False, "offline"

                time.sleep(0.5)
        finally:
            if self.proc and self.proc.poll() is None:
                self._stop_ffmpeg()
            # a graceful stop still produced data -> keep the part
            if target.exists():
                sz = target.stat().st_size
                if sz > 0 and target not in self.parts:
                    self.parts.append(target)
                    self.written += sz
                elif sz == 0:
                    target.unlink(missing_ok=True)
            if self.proc is not None:
                self._close_pipes(self.proc)

    def _still_live(self) -> bool:
        try:
            live = Resolver(self.cfg.http).resolve(
                user=self.live.user, room_id=None, url=None
            )
            if live.room_id and live.room_id != self.live.room_id:
                Log.info("new room id — a new live session started")
                return False
            return bool(live.streams)
        except Offline:
            return False
        except (Retryable, TlrError):
            # cannot confirm -> do not kill a healthy recording
            return True

    # -- whole recording ----------------------------------------------------
    def run(self) -> Path | None:
        Log.info(f"@{self.live.user} live — recording {self.stream.describe()}")
        Log.info("[Ctrl-C once to stop cleanly, twice to abort]")
        index = 1
        failures = 0
        while not self.stop.is_set() and not self.hard_stop.is_set():
            if self.cfg.duration and time.time() - self.started >= self.cfg.duration:
                break
            retry, reason = self._segment(index)
            if not retry:
                break
            failures += 1
            if failures > self.cfg.max_reconnects:
                Log.error(f"giving up after {failures} failed reconnects")
                break
            try:
                fresh = Resolver(self.cfg.http).resolve(
                    user=self.live.user, room_id=None, url=None
                )
                if fresh.streams:
                    self.stream = self.cfg.pick(fresh.streams)
                    self.live = fresh
            except Offline:
                Log.info("user went offline")
                break
            except (Retryable, TlrError) as e:
                Log.warn(f"re-resolve failed: {e}")
            index += 1
            time.sleep(min(2 * failures, 10))
        return self._finalise()

    # -- output -------------------------------------------------------------
    def _finalise(self) -> Path | None:
        parts = [p for p in self.parts if p.exists() and p.stat().st_size > 0]
        if not parts:
            Log.error("nothing recorded")
            return None
        Log.info(f"recorded {human(sum(p.stat().st_size for p in parts))} "
                 f"in {int(time.time() - self.started)}s")

        if len(parts) > 1:
            merged = self.out_dir / f"{self.stem}_merged.ts"
            if concat_ts(self.cfg.ffmpeg, parts, merged):
                parts = [merged]
                Log.info(f"joined {len(parts)} segment(s)")

        suffix = ".m4a" if self.cfg.audio_only else ".mp4"
        final = self.out_dir / f"{self.stem}{suffix}"
        if remux(self.cfg.ffmpeg, parts[0], final, self.cfg.bitrate, self.cfg.audio_only):
            Log.info(f"saved {final} ({human(final.stat().st_size)})")
            if not self.cfg.keep_parts:
                for p in self.parts + [self.out_dir / f"{self.stem}_merged.ts"]:
                    p.unlink(missing_ok=True)
            return final

        # never lose a recording because the container failed
        Log.warn("remux failed — keeping the raw .ts file(s) so nothing is lost")
        for p in parts:
            Log.warn(f"  {p}")
        return parts[0]


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GiB"


def _run_ffmpeg(cmd: list[str]) -> bool:
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=3600)
    except (OSError, subprocess.SubprocessError) as e:
        Log.warn(f"ffmpeg: {e}")
        return False
    if res.returncode != 0:
        err = (res.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        Log.warn("ffmpeg: " + (err[-1][:200] if err else f"exit {res.returncode}"))
        return False
    return True


def concat_ts(ffmpeg: str, parts: list[Path], target: Path) -> bool:
    listfile = target.with_suffix(".txt")
    listfile.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    ok = _run_ffmpeg([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listfile),
        "-c", "copy", "-f", "mpegts", str(target),
    ])
    listfile.unlink(missing_ok=True)
    if ok and target.exists() and target.stat().st_size > 0:
        return True
    # fall back to plain byte concatenation — valid for MPEG-TS
    try:
        with open(target, "wb") as out:
            for p in parts:
                with open(p, "rb") as fh:
                    shutil.copyfileobj(fh, out)
        return target.stat().st_size > 0
    except OSError:
        return False


def remux(ffmpeg: str, src: Path, target: Path, bitrate: str | None, audio_only: bool) -> bool:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if audio_only:
        cmd += ["-vn", "-c:a", "aac", "-b:a", "128k"]
    elif bitrate:
        # explicit re-encode only when the user asks for it
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-b:v", bitrate, "-c:a", "copy"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", str(target)]
    ok = _run_ffmpeg(cmd)
    if not ok or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        return False
    # verify the container is readable and has a video (or audio) stream
    return _has_stream(ffmpeg.replace("ffmpeg", "ffprobe"), target, audio_only)


def _has_stream(ffprobe: str, path: Path, audio_only: bool) -> bool:
    probe = shutil.which(ffprobe) or ffprobe
    try:
        res = subprocess.run(
            [probe, "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)],
            capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return True  # no ffprobe available: trust ffmpeg's exit code
    kinds = (res.stdout or b"").decode().split()
    return ("audio" in kinds) if audio_only else ("video" in kinds)


# --------------------------------------------------------------------------- #
# config + cli
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    user: str | None = None
    room_id: str | None = None
    url: str | None = None
    mode: str = "manual"
    interval: float = 5.0            # minutes between live checks (watch mode)
    output: Path = Path(".")
    duration: int | None = None
    quality: str = "best"
    proxy: str | None = None
    cookies: Path | None = None
    ffmpeg: str = "ffmpeg"
    bitrate: str | None = None
    audio_only: bool = False
    check_only: bool = False
    stall_timeout: int = 25
    live_check: int = 30
    max_reconnects: int = 20
    keep_parts: bool = False
    quiet: bool = False
    http: Http = field(default_factory=Http)
    cookie_header: str = ""

    def pick(self, streams: list[Stream]) -> Stream:
        pool = [s for s in streams if s.is_video] or streams
        if not pool:
            raise TlrError("no usable stream url")
        q = (self.quality or "best").lower()
        if q in ("best", "max", "highest"):
            return pool[0]
        if q in ("worst", "min", "lowest"):
            return pool[-1]
        for s in pool:
            if q in s.label.lower() or q in s.resolution.lower():
                return s
        Log.warn(f"quality {q!r} not offered, using {pool[0].describe()}")
        return pool[0]


def load_cookies(path: Path | None) -> dict[str, str]:
    if not path:
        # the reference tool keeps cookies.json next to the script
        auto = Path(__file__).with_name("cookies.json")
        path = auto if auto.exists() else None
    if not path or not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if v not in (None, "")}
        if isinstance(data, list):
            return {str(c["name"]): str(c["value"]) for c in data if c.get("name")}
    except ValueError:
        pass
    # netscape cookies.txt
    jar: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) >= 7:
            jar[fields[5]] = fields[6]
    return jar


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tlr",
        description="Lean TikTok LIVE recorder (ffmpeg copy, watchdog, auto-reconnect).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Beispiele:\n"
            "  tlr.py -user account\n"
            "  tlr.py -user a,b,c -output /data/live\n"
            "  tlr.py -user account -mode watch -interval 3\n"
            "  tlr.py -user account --check\n"
            "  tlr.py -user account -quality sd -duration 900\n"
        ),
    )
    p.add_argument("-user", "--user", help="Username(s), Komma-getrennt")
    p.add_argument("-url", "--url", help="TikTok-Live-URL")
    p.add_argument("-room_id", "--room-id", dest="room_id", help="Room-ID")
    p.add_argument("-mode", "--mode", choices=("manual", "watch"), default="manual",
                   help="manual = jetzt aufnehmen, watch = dauerhaft lauschen (default: manual)")
    p.add_argument("-interval", "--interval", "-automatic_interval", dest="interval", type=float,
                   default=5.0, help="Minuten zwischen Live-Checks im watch-Modus (default: 5)")
    p.add_argument("-output", "--output", type=Path, default=Path("."), help="Zielverzeichnis")
    p.add_argument("-duration", "--duration", type=int, help="Max. Aufnahmedauer in Sekunden")
    p.add_argument("-quality", "--quality", default="best",
                   help="best|worst|<label>|<auflösung>, z.B. 'sd' oder '720p' (default: best)")
    p.add_argument("-proxy", "--proxy", help="HTTP-Proxy, z.B. http://127.0.0.1:8080")
    p.add_argument("-cookies", "--cookies", type=Path, help="cookies.json oder cookies.txt")
    p.add_argument("-ffmpeg-path", "--ffmpeg-path", dest="ffmpeg", default="ffmpeg")
    p.add_argument("-bitrate", "--bitrate", help="Re-Encode mit dieser Bitrate (z.B. 1M) — kostet Qualität")
    p.add_argument("--audio-only", action="store_true", help="Nur Audio, als .m4a")
    p.add_argument("--check", action="store_true", help="Nur Status/Qualitäten zeigen, nichts aufnehmen")
    p.add_argument("--stall-timeout", type=int, default=25, help="Sekunden ohne Daten bis Reconnect (default: 25)")
    p.add_argument("--live-check", type=int, default=30, help="Sekunden zwischen Live-Checks während der Aufnahme")
    p.add_argument("--max-reconnects", type=int, default=20, help="Max. Reconnects pro Aufnahme")
    p.add_argument("--keep-parts", action="store_true", help="Rohe .ts-Segmente behalten")
    p.add_argument("-q", "--quiet", action="store_true", help="Weniger Ausgabe")
    p.add_argument("-V", "--version", action="version", version=f"tlr {VERSION}")
    return p


def cfg_from_args(argv: list[str] | None = None) -> Config:
    a = build_parser().parse_args(argv)
    cookie_map = load_cookies(a.cookies)
    http = Http(proxy=a.proxy, cookies=cookie_map)
    out = a.output.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    return Config(
        user=(a.user.lstrip("@").strip() if a.user else None),
        room_id=a.room_id,
        url=a.url,
        mode=a.mode,
        interval=a.interval,
        output=out,
        duration=a.duration,
        quality=a.quality,
        proxy=a.proxy,
        cookies=a.cookies,
        ffmpeg=a.ffmpeg,
        bitrate=a.bitrate,
        audio_only=a.audio_only,
        check_only=a.check,
        stall_timeout=a.stall_timeout,
        live_check=a.live_check,
        max_reconnects=a.max_reconnects,
        keep_parts=a.keep_parts,
        quiet=a.quiet,
        http=http,
        cookie_header="; ".join(f"{k}={v}" for k, v in cookie_map.items()),
    )


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #


class Session:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.resolver = Resolver(cfg.http)
        self.current: Recording | None = None
        self.abort = False

    def record_once(self, live: Live) -> Path | None:
        stream = self.cfg.pick(live.streams)
        if not probe_stream(self.cfg.http, stream):
            usable = [s for s in live.streams if probe_stream(self.cfg.http, s)]
            if not usable:
                raise Retryable("no stream url delivered data")
            stream = self.cfg.pick(usable)
        if live.title:
            Log.info(f'title: "{live.title}"')
        rec = Recording(self.cfg, live, stream, self.cfg.output)
        self.current = rec
        try:
            return rec.run()
        finally:
            self.current = None

    def run(self) -> int:
        cfg = self.cfg
        if cfg.check_only:
            return self._check()
        if cfg.mode == "manual":
            try:
                live = self.resolver.resolve(cfg.user, cfg.room_id, cfg.url)
            except Offline as e:
                Log.error(str(e))
                return 2
            except (Retryable, TlrError) as e:
                Log.error(f"cannot resolve: {e}")
                return 1
            try:
                return 0 if self.record_once(live) else 3
            except Retryable as e:
                Log.error(str(e))
                return 3
        return self._watch()

    def _check(self) -> int:
        cfg = self.cfg
        try:
            live = self.resolver.resolve(cfg.user, cfg.room_id, cfg.url)
        except Offline as e:
            Log.info(str(e))
            return 2
        except (Retryable, TlrError) as e:
            Log.error(f"cannot resolve: {e}")
            return 1
        Log.info(f"@{live.user} is live — room {live.room_id} ({live.source})")
        if live.title:
            Log.info(f'title: "{live.title}"')
        for s in live.streams:
            ok = probe_stream(cfg.http, s)
            Log.info(f"  {s.describe():38s} {'ok' if ok else 'no data'}  {s.label}")
        return 0

    def _watch(self) -> int:
        cfg = self.cfg
        users = [u.strip().lstrip("@") for u in (cfg.user or "").split(",") if u.strip()]
        if cfg.url and not users:
            u = parse_user_from_url(cfg.url)
            users = [u] if u else []
        if not users:
            Log.error("watch mode needs -user (or a -url with a username)")
            return 1
        if len(users) > 1:
            Log.error("watch mode records one user per process; start one process per user")
            return 1
        user = users[0]
        Log.info(f"watching @{user} every {cfg.interval:g} min — Ctrl-C to stop")
        while not self.abort:
            try:
                live = self.resolver.resolve(user, None, None)
                Log.info(f"@{user} is live")
                path = self.record_once(live)
                if path:
                    Log.info(f"done: {path}")
                if self.abort:
                    break
                Log.info("waiting for the next live …")
                time.sleep(min(cfg.interval * 60, 15))  # short pause after a session
                continue
            except Offline as e:
                Log.debug(str(e))
            except (Retryable, TlrError) as e:
                Log.warn(f"check failed: {e}")
            deadline = time.time() + cfg.interval * 60
            while time.time() < deadline and not self.abort:
                time.sleep(min(5, max(0.2, deadline - time.time())))
        return 0


def install_signal_handlers(session: Session) -> None:
    state = {"hits": 0}

    def handler(signum, frame):  # noqa: ARG001
        state["hits"] += 1
        rec = session.current
        if state["hits"] == 1 and rec is not None:
            Log.warn("stopping cleanly … (Ctrl-C again to abort)")
            rec.stop.set()
        else:
            Log.warn("aborting")
            session.abort = True
            if rec is not None:
                rec.hard_stop.set()
                rec.stop.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def check_ffmpeg(binary: str) -> None:
    path = shutil.which(binary)
    if not path:
        raise TlrError(
            f"ffmpeg not found ({binary}). Install it: apt install ffmpeg / brew install ffmpeg"
        )


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = cfg_from_args(argv)
    except SystemExit as e:  # argparse
        return int(e.code or 0)
    Log.quiet = cfg.quiet
    try:
        check_ffmpeg(cfg.ffmpeg)
    except TlrError as e:
        Log.error(str(e))
        return 1
    session = Session(cfg)
    install_signal_handlers(session)
    try:
        return session.run()
    except KeyboardInterrupt:
        return 130
    except TlrError as e:
        Log.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
