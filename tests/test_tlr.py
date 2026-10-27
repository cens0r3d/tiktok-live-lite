#!/usr/bin/env python3
"""Tests for tlr.py — stdlib unittest only, no network needed.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tlr  # noqa: E402

SAMPLE_PULL = {
    "options": {"qualities": [{"sdk_key": "hd", "level": 3, "name": "720p"}]},
    "stream_data": json.dumps(
        {
            "common": {"session_id": "x"},
            "data": {
                "hd": {
                    "main": {
                        "flv": "https://cdn.example/main_hd.flv",
                        "hls": "",
                        "sdk_params": json.dumps(
                            {"resolution": "1280x720", "vbitrate": 1800000}
                        ),
                    },
                    "backup": {
                        "flv": "https://cdn2.example/backup_hd.flv",
                        "sdk_params": json.dumps(
                            {"resolution": "1280x720", "vbitrate": 1810000}
                        ),
                    },
                },
                "sd": {
                    "main": {
                        "flv": "https://cdn.example/main_sd.flv",
                        "sdk_params": json.dumps(
                            {"resolution": "640x360", "vbitrate": 800000}
                        ),
                    }
                },
                "ao": {
                    "main": {
                        "flv": "https://cdn.example/audio.flv?only_audio=1",
                        "sdk_params": json.dumps({"vbitrate": 0}),
                    }
                },
            },
        }
    ),
}

SIGN_ROUTE = {"signed_path": "/api-live/user/room/?uniqueId=someone&aid=1988"}


def sign_then(payload):
    """Route table for the two-step signed flow: sign -> signed url -> payload."""
    return {
        "/tiktok/room/api/sign": ("json", SIGN_ROUTE),
        "/api-live/user/room/": ("json", payload),
    }


SIGNED_PAYLOAD = {
    "data": {
        "user": {"uniqueId": "someone", "roomId": "1234567890123456789", "status": 2},
        "liveRoom": {"title": "Test live", "status": 2, "streamData": {"pull_data": SAMPLE_PULL}},
    }
}


class FakeHttp:
    """Scripted stand-in for tlr.Http."""

    def __init__(self, routes, opener=None):
        self.routes = routes          # url-substring -> (kind, payload)
        self.calls: list[str] = []
        self.opener = opener

    def _resolve(self, url):
        for needle, value in self.routes.items():
            if needle in url:
                return value
        raise tlr.Retryable(f"no route for {url}")

    def get_text(self, url, timeout=15.0, tries=3, headers=None):
        self.calls.append(url)
        kind, payload = self._resolve(url)
        if kind == "raise":
            raise payload
        if kind == "html":
            return payload
        return json.dumps(payload)

    def get_json(self, url, timeout=15.0, tries=3):
        self.calls.append(url)
        kind, payload = self._resolve(url)
        if kind == "raise":
            raise payload
        if kind == "text":
            # what a WAF/challenge page looks like: not JSON at all
            raise tlr.Retryable("non-JSON response")
        return payload


# --------------------------------------------------------------------------- #
# parsing / quality selection
# --------------------------------------------------------------------------- #


class TestStreamParsing(unittest.TestCase):
    def test_video_variants_first_and_audio_excluded(self):
        streams = tlr._streams_from_pull_data(SAMPLE_PULL)
        self.assertTrue(streams)
        self.assertTrue(all(s.is_video for s in streams[:2]))
        self.assertNotIn("only_audio=1", streams[0].url)
        # highest bitrate first (backup 1810k > main 1800k)
        self.assertEqual(streams[0].bitrate, 1810000)
        self.assertTrue(any(not s.is_video for s in streams))  # audio kept but last

    def test_pick_best_and_worst(self):
        streams = tlr._streams_from_pull_data(SAMPLE_PULL)
        cfg = tlr.Config()
        best = cfg.pick(streams)
        self.assertGreaterEqual(best.bitrate, 1800000)
        worst = tlr.Config(quality="worst").pick(streams)
        self.assertLess(worst.bitrate, best.bitrate)

    def test_pick_by_resolution_label(self):
        streams = tlr._streams_from_pull_data(SAMPLE_PULL)
        self.assertEqual(tlr.Config(quality="360").pick(streams).bitrate, 800000)

    def test_broken_stream_data_falls_back_to_legacy_urls(self):
        pull = {
            "stream_data": "{ this is not json",
            "flv_pull_url": {"HD1": "https://cdn.example/legacy_hd.flv"},
            "hls_pull_url": "https://cdn.example/legacy.m3u8",
        }
        streams = tlr._streams_from_pull_data(pull)
        self.assertEqual(len(streams), 2)
        self.assertIn("legacy_hd.flv", streams[0].url)

    def test_missing_stream_data_is_empty_not_crash(self):
        self.assertEqual(tlr._streams_from_pull_data({}), [])


# --------------------------------------------------------------------------- #
# http guards (the JSONDecodeError bug class)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, body: bytes):
        self.body = body

    def open(self, req, timeout=None):
        return _FakeResponse(self.body)


class TestHttpGuards(unittest.TestCase):
    def test_html_instead_of_json_is_retryable(self):
        http = tlr.Http()
        http.opener = _FakeOpener(b"<html>Please wait</html>")
        with self.assertRaises(tlr.Retryable):
            http.get_json("https://x.test/api", tries=1)

    def test_garbage_json_is_retryable(self):
        http = tlr.Http()
        http.opener = _FakeOpener(b'{"data": '
                                  )  # truncated body
        with self.assertRaises(tlr.Retryable):
            http.get_json("https://x.test/api", tries=1)

    def test_empty_body_is_retryable(self):
        http = tlr.Http()
        http.opener = _FakeOpener(b"")
        with self.assertRaises(tlr.Retryable):
            http.get_json("https://x.test/api", tries=1)

    def test_cookie_header_built(self):
        http = tlr.Http(cookies={"sessionid": "abc", "tt_chain_token": "def"})
        self.assertEqual(http.headers["Cookie"], "sessionid=abc; tt_chain_token=def")


# --------------------------------------------------------------------------- #
# resolver
# --------------------------------------------------------------------------- #


class TestResolver(unittest.TestCase):
    def test_offline_status_is_offline(self):
        payload = {
            "data": {
                "user": {"uniqueId": "someone", "roomId": "42", "status": 4},
                "liveRoom": {"status": 4},
            }
        }
        http = FakeHttp(sign_then(payload))
        with self.assertRaises(tlr.Offline):
            tlr.Resolver(http).resolve("someone", None, None)

    def test_unknown_user_is_not_found(self):
        payload = {"data": None, "message": "user_not_found", "statusCode": 19881007}
        http = FakeHttp(sign_then(payload))
        with self.assertRaises(tlr.NotFound):
            tlr.Resolver(http).resolve("ghost", None, None)

    def test_signed_ok(self):
        http = FakeHttp(sign_then(SIGNED_PAYLOAD))
        live = tlr.Resolver(http).resolve("someone", None, None)
        self.assertEqual(live.room_id, "1234567890123456789")
        self.assertEqual(live.user, "someone")
        self.assertTrue(live.streams)

    def test_signed_blocked_falls_back_to_page(self):
        html = (
            '<script>{"roomId":"777","status":2}</script>'
            '"https://cdn.example/from_page.flv?sign=1"'
        )
        http = FakeHttp({
            "/tiktok/room/api/sign": ("json", SIGN_ROUTE),
            "/api-live/user/room/": ("json", {"data": None, "message": "blocked"}),
            "/live": ("html", html),
        })
        live = tlr.Resolver(http).resolve("someone", None, None)
        self.assertEqual(live.source, "page")
        self.assertEqual(live.room_id, "777")
        self.assertIn("from_page.flv", live.streams[0].url)

    def test_private_account_is_reported_as_private_not_offline(self):
        # what TikTok returns for a private account: live, but every live field
        # redacted (roomId "", no streamData, followers 0, secret true)
        payload = {
            "data": {
                "user": {"uniqueId": "private_guy", "roomId": "", "status": 2, "secret": True},
                "stats": {"followerCount": 0},
                "liveRoom": {"status": 2, "title": ""},
            }
        }
        http = FakeHttp(sign_then(payload))
        with self.assertRaises(tlr.PrivateAccount) as ctx:
            tlr.Resolver(http).resolve("private_guy", None, None)
        self.assertIn("private account", str(ctx.exception))
        self.assertIn("cookies", str(ctx.exception))

    def test_live_without_room_and_without_secret_is_retryable(self):
        payload = {
            "data": {
                "user": {"uniqueId": "x", "roomId": "", "status": 2, "secret": False},
                "liveRoom": {"status": 2},
            }
        }
        http = FakeHttp({
            "/tiktok/room/api/sign": ("json", SIGN_ROUTE),
            "/api-live/user/room/": ("json", payload),
            "/live": ("html", "<html>nothing here</html>"),
        })
        with self.assertRaises(tlr.Retryable):
            tlr.Resolver(http).resolve("x", None, None)

    def test_url_is_parsed_into_user(self):
        self.assertEqual(
            tlr.parse_user_from_url("https://www.tiktok.com/@mike/live?lang=de"), "mike"
        )
        self.assertIsNone(tlr.parse_user_from_url("https://example.com/x"))

    def test_no_target_is_an_error(self):
        with self.assertRaises(tlr.TlrError):
            tlr.Resolver(FakeHttp({})).resolve(None, None, None)


# --------------------------------------------------------------------------- #
# recording: stall / reconnect / finalise
# --------------------------------------------------------------------------- #


def make_ts(path: Path, seconds: int = 6) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
            "-c:a", "aac", "-shortest", "-f", "mpegts", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


class FakeCDN:
    """Serves a TS file, optionally truncating + hanging on the first request."""

    def __init__(self, payload: bytes, truncate_first: float = 0.55):
        self.payload = payload
        self.truncate_first = truncate_first
        self.hits = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits += 1
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(len(outer.payload)))
                self.end_headers()
                if outer.hits == 1:
                    # send half, then go silent: models a stalled CDN socket
                    self.wfile.write(outer.payload[: int(len(outer.payload) * outer.truncate_first)])
                    self.wfile.flush()
                    time.sleep(30)
                else:
                    self.wfile.write(outer.payload)

            do_HEAD = do_GET

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/stream.ts"


class TestRecording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="tlr-test-"))
        cls.ts = make_ts(cls.tmp / "sample.ts", 6)

    def _cfg(self, out: Path, **kw) -> tlr.Config:
        base = dict(
            ffmpeg="ffmpeg",
            output=out,
            user="fake",
            stall_timeout=3,
            live_check=10 ** 9,   # keep the live probe out of the unit test
            max_reconnects=3,
        )
        base.update(kw)
        return tlr.Config(**base)

    def test_stall_detected_and_reconnected(self):
        payload = self.ts.read_bytes()
        with FakeCDN(payload) as cdn, tempfile.TemporaryDirectory() as d:
            out = Path(d)
            stream = tlr.Stream(url=cdn.url, label="hd", bitrate=1, resolution="320x240", is_video=True)
            live = tlr.Live(user="fake", room_id="1", streams=[stream])
            rec = tlr.Recording(self._cfg(out), live, stream, out)
            result = rec.run()

            self.assertIsNotNone(result, "recording lost after a stall")
            self.assertGreaterEqual(cdn.hits, 2, "watchdog never reconnected")
            self.assertEqual(result.suffix, ".mp4")
            self.assertGreater(result.stat().st_size, 1000)
            self.assertTrue(tlr._has_stream("ffprobe", result, audio_only=False))
            # parts are cleaned up after a successful remux
            self.assertEqual(list(out.glob("*_part*.ts")), [])

    def test_keep_parts_preserves_segments(self):
        payload = self.ts.read_bytes()
        with FakeCDN(payload) as cdn, tempfile.TemporaryDirectory() as d:
            out = Path(d)
            stream = tlr.Stream(url=cdn.url, label="hd", bitrate=1, resolution="", is_video=True)
            live = tlr.Live(user="fake", room_id="1", streams=[stream])
            rec = tlr.Recording(self._cfg(out, keep_parts=True), live, stream, out)
            rec.run()
            self.assertTrue(list(out.glob("*_part*.ts")))

    def test_nothing_served_yields_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            stream = tlr.Stream(url="http://127.0.0.1:9/none.flv", label="hd",
                                bitrate=1, resolution="", is_video=True)
            live = tlr.Live(user="fake", room_id="1", streams=[stream])
            rec = tlr.Recording(self._cfg(out, stall_timeout=1, max_reconnects=1), live, stream, out)
            self.assertIsNone(rec.run())
            self.assertEqual(list(out.glob("*.mp4")), [])

    def test_concat_produces_playable_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            a = make_ts(out / "a.ts", 3)
            b = make_ts(out / "b.ts", 3)
            merged = out / "merged.ts"
            self.assertTrue(tlr.concat_ts("ffmpeg", [a, b], merged))
            dur = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(merged)],
                capture_output=True, text=True,
            ).stdout.strip()
            self.assertGreater(float(dur), 4.0)

    def test_remux_refuses_bad_input(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            bad = out / "bad.ts"
            bad.write_bytes(b"\x00" * 100)
            self.assertFalse(tlr.remux("ffmpeg", bad, out / "bad.mp4", None, False))


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


class TestCli(unittest.TestCase):
    def test_defaults(self):
        cfg = tlr.cfg_from_args(["-user", "@someone"])
        self.assertEqual(cfg.user, "someone")
        self.assertEqual(cfg.mode, "manual")
        self.assertEqual(cfg.quality, "best")

    def test_upstream_flag_aliases(self):
        cfg = tlr.cfg_from_args(
            ["-user", "a", "-automatic_interval", "2", "-ffmpeg-path", "/usr/bin/ffmpeg",
             "-mode", "watch"]
        )
        self.assertEqual(cfg.interval, 2)
        self.assertEqual(cfg.ffmpeg, "/usr/bin/ffmpeg")
        self.assertEqual(cfg.mode, "watch")

    def test_bad_mode_rejected(self):
        with self.assertRaises(SystemExit):
            tlr.cfg_from_args(["-user", "a", "-mode", "followers"])

    def test_missing_ffmpeg_is_reported(self):
        with self.assertRaises(tlr.TlrError):
            tlr.check_ffmpeg("definitely-not-ffmpeg")

    def test_cookie_string(self):
        jar = tlr.parse_cookie_string("sessionid=abc; tt_chain_token=d e f;  broken")
        self.assertEqual(jar, {"sessionid": "abc", "tt_chain_token": "d e f"})
        cfg = tlr.cfg_from_args(["-user", "a", "-cookie", "sessionid=xyz; tt_chain_token=q"])
        self.assertEqual(cfg.http.headers["Cookie"], "sessionid=xyz; tt_chain_token=q")
        self.assertEqual(cfg.cookie_header, "sessionid=xyz; tt_chain_token=q")

    def test_cookie_json_and_netscape(self):
        with tempfile.TemporaryDirectory() as d:
            j = Path(d) / "c.json"
            j.write_text(json.dumps({"sessionid": "abc"}))
            self.assertEqual(tlr.load_cookies(j)["sessionid"], "abc")
            n = Path(d) / "c.txt"
            n.write_text("# Netscape HTTP Cookie File\n.tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\txyz\n")
            self.assertEqual(tlr.load_cookies(n)["sessionid"], "xyz")


if __name__ == "__main__":
    unittest.main(verbosity=2)
