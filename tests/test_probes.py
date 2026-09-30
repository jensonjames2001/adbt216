"""Tests for the Phase 0 probe scaffolding (probes/common.py) and the pure
helpers of probe_telegram.py and probe_waze.py. No network."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from probes import common  # noqa: E402
from probes import probe_telegram as tg  # noqa: E402
from probes import probe_waze as wz  # noqa: E402
from rgalerts.config import Box  # noqa: E402

PY = sys.executable
FAKE_TOKEN = "123456789:AAFakeTokenValueForTestsOnly-abc"
FAKE_KEY = "FAKEKEYFAKEKEYFAKEKEYFAKEKEY1234"


# --------------------------------------------------------------------------
# common: redaction
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url, must_not_contain, must_contain", [
    (f"https://api.tomtom.com/traffic/map/4/tile/incidents/10/511/340.pbf?key={FAKE_KEY}&tags=%5Bid%5D",
     FAKE_KEY, "key=***&tags=%5Bid%5D"),
    (f"https://x.example/?apikey={FAKE_KEY}", FAKE_KEY, "apikey=***"),
    (f"https://x.example/?API_KEY={FAKE_KEY}&bbox=1,2,3,4", FAKE_KEY, "bbox=1,2,3,4"),
    (f"https://x.example/?subscription-key={FAKE_KEY}", FAKE_KEY, "subscription-key=***"),
    (f"https://x.example/?token={FAKE_KEY}", FAKE_KEY, "token=***"),
    (f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe", FAKE_TOKEN, "https://api.telegram.org/bot***/getMe"),
    (f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage?chat_id=5", FAKE_TOKEN, "/bot***/sendMessage?chat_id=5"),
    (f"https://user:{FAKE_KEY}@x.example/p", FAKE_KEY, "https://***@x.example/p"),
])
def test_redacted_url(url, must_not_contain, must_contain):
    out = common.redacted_url(url)
    assert must_not_contain not in out
    assert must_contain in out


def test_redacted_url_keeps_plain_params_and_encoding():
    url = "https://api.data.nationalhighways.co.uk/roads/v2.0/closures?closureType=unplanned&startDateTime=2026-09-30T10%3A00%3A00&pageCursor=2"
    assert common.redacted_url(url) == url


def test_redacted_url_scrubs_known_env_secret_anywhere(monkeypatch):
    monkeypatch.setenv("TOMTOM_API_KEY", FAKE_KEY)
    out = common.redacted_url(f"https://x.example/{FAKE_KEY}/tile.pbf")
    assert FAKE_KEY not in out and "***" in out


def test_redacted_headers_masks_secrets_and_urls():
    h = httpx.Headers({
        "Ocp-Apim-Subscription-Key": FAKE_KEY,
        "Authorization": "Bearer " + FAKE_KEY,
        "X-Api-Key": FAKE_KEY,
        "X-Custom-Token": FAKE_KEY,
        "Cookie": "a=b",
        "Content-Type": "application/json",
        "x-next": f"https://api.example/closures?subscription-key={FAKE_KEY}&PageCursor=2",
        "ETag": 'W/"2fdbd61f30456"',
    })
    out = common.redacted_headers(h)
    dumped = json.dumps(out)
    assert FAKE_KEY not in dumped
    assert out["ocp-apim-subscription-key"] == "***"
    assert out["authorization"] == "***"
    assert out["x-api-key"] == "***"
    assert out["x-custom-token"] == "***"
    assert out["cookie"] == "***"
    assert out["content-type"] == "application/json"
    assert out["etag"] == 'W/"2fdbd61f30456"'
    assert out["x-next"] == "https://api.example/closures?subscription-key=***&PageCursor=2"
    assert common.redacted_headers(None) == {}
    assert common.redacted_headers({"Content-Length": 5}) == {"Content-Length": "5"}


def test_known_secrets_ignores_short_values(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "ab")
    monkeypatch.setenv("NH_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    secrets = common.known_secrets()
    assert FAKE_KEY in secrets
    assert "ab" not in secrets
    assert "-1001234567890" not in secrets


# --------------------------------------------------------------------------
# common: save_response
# --------------------------------------------------------------------------

def _response(status, url, content, content_type, method="GET", req_headers=None):
    req = httpx.Request(method, url, headers=req_headers or {})
    return httpx.Response(status, content=content, headers={"content-type": content_type}, request=req)


def test_save_response_json_never_writes_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TOMTOM_API_KEY", FAKE_KEY)
    url = f"https://api.tomtom.com/traffic/services/5/incidentDetails?key={FAKE_KEY}&bbox=-0.7,51.36,-0.25,51.6"
    resp = _response(200, url, b'{"incidents": [{"id": "a"}]}', "application/json;charset=utf-8",
                     req_headers={"X-Api-Key": FAKE_KEY})
    meta_path = common.save_response(tmp_path, "details", resp, elapsed_s=0.123)
    assert meta_path == tmp_path / "details.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["url"] == "https://api.tomtom.com/traffic/services/5/incidentDetails?key=***&bbox=-0.7,51.36,-0.25,51.6"
    assert meta["status"] == 200 and meta["method"] == "GET" and meta["elapsed_s"] == 0.123
    assert meta["request_headers"]["x-api-key"] == "***"
    assert meta["body_file"] == "details.body.json"
    body = json.loads((tmp_path / "details.body.json").read_text(encoding="utf-8"))
    assert body == {"incidents": [{"id": "a"}]}
    for f in tmp_path.iterdir():
        assert FAKE_KEY not in f.read_bytes().decode("utf-8", errors="replace"), f.name


def test_save_response_text_and_pbf_and_truncation(tmp_path):
    html = _response(403, "https://www.waze.com/live-map/api/georss?top=1", b"<html>" + b"x" * 1000, "text/html")
    common.save_response(tmp_path, "blocked", html, max_body_bytes=500)
    meta = json.loads((tmp_path / "blocked.meta.json").read_text(encoding="utf-8"))
    assert meta["body_file"] == "blocked.body.txt" and meta["body_truncated"] is True
    assert len((tmp_path / "blocked.body.txt").read_bytes()) == 500

    pbf = _response(200, "https://api.tomtom.com/t/1/2/3.pbf?key=k", b"\x1a\x03abc", "application/x-protobuf")
    common.save_response(tmp_path, "tile", pbf)
    assert (tmp_path / "tile.body.pbf").read_bytes() == b"\x1a\x03abc"

    nobody = _response(304, "https://api.tomtom.com/t/1/2/3.pbf?key=k", b"", "")
    common.save_response(tmp_path, "unchanged", nobody)
    meta = json.loads((tmp_path / "unchanged.meta.json").read_text(encoding="utf-8"))
    assert meta["body_file"] is None and meta["body_bytes"] == 0

    private = _response(200, "https://api.telegram.org/botX:Y/getUpdates", b'{"ok":true,"result":[{"from":{"first_name":"Bob"}}]}',
                        "application/json")
    common.save_response(tmp_path, "upd", private, body=False)
    assert not (tmp_path / "upd.body.json").exists()
    assert "Bob" not in (tmp_path / "upd.meta.json").read_text(encoding="utf-8")


def test_save_response_without_request_object(tmp_path):
    resp = httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})
    meta = json.loads(common.save_response(tmp_path, "x", resp).read_text(encoding="utf-8"))
    assert meta["url"] is None and meta["elapsed_s"] is None


# --------------------------------------------------------------------------
# common: Findings, RequestBudget, time, client
# --------------------------------------------------------------------------

def test_findings_writes_markdown_and_summary(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NH_API_KEY", FAKE_KEY)
    f = common.Findings("Test probe")
    f.section("Numbers")
    f.kv("tiles", 4)
    f.kv("ratio", 0.5)
    f.kv("nothing", None)
    f.add("secret leaked? " + FAKE_KEY)
    f.table([[1, "a|b"], [2, "c"]], header=["n", "name"])
    f.table([], header=["only", "header"])
    md = f.write(tmp_path)
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# Test probe")
    assert "## Numbers" in text and "- tiles: 4" in text and "- ratio: 0.5" in text and "- nothing: unknown" in text
    assert "| n | name |" in text and "a\\|b" in text and "(none)" in text
    assert FAKE_KEY not in text and "***" in text
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["values"] == {"tiles": 4, "ratio": 0.5, "nothing": None}
    out = capsys.readouterr().out
    assert "- tiles: 4" in out and FAKE_KEY not in out


def test_request_budget_refuses_to_exceed_cap(capsys):
    b = common.RequestBudget(3)
    b.check(3, "tiles")
    assert "Planned requests: 3" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        b.check(4)
    assert "cap is 3" in str(e.value)
    assert b.count("tiles") == 1
    assert b.count("tiles") == 2
    assert b.count("details") == 1
    with pytest.raises(SystemExit) as e:
        b.count("tiles")
    assert "exceed" in str(e.value)
    assert b.totals() == {"tiles": 2, "details": 1, "total": 3}
    with pytest.raises(ValueError):
        common.RequestBudget(0)


def test_london_and_now_utc():
    assert common.london(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)) == "13:00"   # BST
    assert common.london(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)) == "12:00"   # GMT
    assert common.london(datetime(2026, 7, 1, 12, 0)) == "13:00"                        # naive = UTC
    assert common.now_utc().tzinfo is timezone.utc
    assert common.run_dir_name(datetime(2026, 9, 30, 18, 5, 7, tzinfo=timezone.utc)) == "20260930T180507Z"


def test_http_client_settings():
    with common.http_client() as c:
        assert c.headers["user-agent"] == "rgalerts-probe/0.1 (+https://recoverygiantuk.com)"
        assert c.timeout == httpx.Timeout(10.0)
        assert c.follow_redirects is False


def test_explain_exception_mentions_timeout():
    assert "10 s" in common.explain_exception(httpx.ConnectTimeout("x"))
    assert "closed the connection" in common.explain_exception(httpx.RemoteProtocolError("x"))


def test_setup_creates_outdir_and_loads_config(tmp_path, monkeypatch):
    out = tmp_path / "run"
    cfg, outdir, parser = common.setup("waze", "desc", argv=["--out", str(out)], max_requests_default=4)
    assert outdir == out and out.is_dir()
    assert cfg["box_obj"] == Box(-0.70, 51.36, -0.25, 51.60)
    parser.add_argument("--grid", type=int, default=1)
    args = parser.parse_args(["--out", str(out), "--grid", "2"])
    assert args.max_requests == 4 and args.grid == 2


def test_setup_help_does_not_create_outdir(tmp_path):
    out = tmp_path / "nope"
    common.setup("waze", "desc", argv=["--out", str(out), "--help"])
    assert not out.exists()


# --------------------------------------------------------------------------
# probe_telegram helpers
# --------------------------------------------------------------------------

def test_telegram_sample_text_matches_brief():
    text = tg.sample_alert_text(datetime(2026, 9, 30, 14, 8, tzinfo=common.LONDON))
    assert text.split("\n") == [
        "This was a test message",
        "🚨 Reported breakdown: M25 clockwise",
        "J13 (Staines) to J14 (Heathrow T4)",
        "Hard shoulder · first reported 14:05 (3 min ago)",
        "6.2 mi from Feltham in a straight line",
        "TomTom · 2 reports",
    ]
    assert "job" not in text.lower()
    edited = tg.replace_last_line(text, "TomTom · Waze (3 👍) · lane closed on signs")
    assert edited.endswith("TomTom · Waze (3 👍) · lane closed on signs") and edited.count("\n") == 5
    assert tg.sample_buttons() == {"inline_keyboard": [[
        {"text": "Google Maps", "url": "https://www.google.com/maps/search/?api=1&query=51.4356,-0.5115"},
        {"text": "Waze", "url": "https://waze.com/ul?ll=51.4356,-0.5115&navigate=yes"},
    ]]}


def test_telegram_redaction_keeps_no_personal_data():
    updates = [
        {"update_id": 1, "message": {"message_id": 9, "date": 1700000000, "text": "hello there",
                                     "from": {"id": 42, "first_name": "Bob", "username": "bob_h"},
                                     "chat": {"id": 42, "type": "private", "first_name": "Bob", "username": "bob_h"}}},
        {"update_id": 2, "message": {"date": 1700000100, "caption": "pic",
                                     "chat": {"id": -1001, "type": "supergroup", "title": "RG dispatch"}}},
        {"update_id": 3, "my_chat_member": {"date": 1700000200, "chat": {"id": -1001, "type": "supergroup", "title": "RG dispatch"},
                                            "from": {"id": 7, "first_name": "Ann"}}},
        {"update_id": 4, "callback_query": {"id": "x", "from": {"id": 42, "first_name": "Bob"},
                                            "message": {"chat": {"id": 42, "type": "private", "first_name": "Bob"}}}},
        {"update_id": 5, "poll": {"id": "p"}},
        "garbage",
    ]
    red = tg.redact_updates(updates)
    dumped = json.dumps(red)
    for bad in ("Bob", "bob_h", "Ann", "hello", "first_name", "username", "from"):
        assert bad not in dumped, bad
    assert red[0] == {"update_id": 1, "kind": "message", "date": 1700000000, "text_len": 11, "chat": {"id": 42, "type": "private"}}
    assert red[1]["chat"] == {"id": -1001, "type": "supergroup", "title": "RG dispatch"} and red[1]["text_len"] == 3
    assert red[2]["kind"] == "my_chat_member" and red[2]["chat"]["id"] == -1001
    assert red[3]["kind"] == "callback_query" and red[3]["chat"] == {"id": 42, "type": "private"}
    assert red[4] == {"update_id": 5, "kind": "poll", "text_len": 0}
    chats = tg.distinct_chats(updates)
    assert chats == [{"id": 42, "type": "private"}, {"id": -1001, "type": "supergroup", "title": "RG dispatch"}]
    sent = tg.redact_sent_message({"ok": True, "result": {"message_id": 5, "date": 1, "text": "abc", "reply_markup": {},
                                                          "chat": {"id": 42, "type": "private", "first_name": "Bob"},
                                                          "from": {"id": 1, "is_bot": True, "first_name": "rgbot"}}})
    assert sent == {"ok": True, "message_id": 5, "date": 1, "edit_date": None, "chat": {"id": 42, "type": "private"},
                    "text_len": 3, "has_reply_markup": True}
    assert tg.retry_after_seconds({"parameters": {"retry_after": 7}}) == 7.0
    assert tg.retry_after_seconds({"ok": False}) == 5.0
    assert common.redacted_url(tg.api_url(FAKE_TOKEN, "getMe")) == "https://api.telegram.org/bot***/getMe"


# --------------------------------------------------------------------------
# probe_waze helpers
# --------------------------------------------------------------------------

BOX = Box(-0.70, 51.36, -0.25, 51.60)


def test_waze_grid_cells():
    one = wz.grid_cells(BOX, 1)
    assert one == [{"name": "box", "top": 51.6, "bottom": 51.36, "left": -0.7, "right": -0.25}]
    assert wz.cell_params(one[0]) == {"top": "51.6000", "bottom": "51.3600", "left": "-0.7000", "right": "-0.2500",
                                      "env": "row", "types": "alerts"}
    two = wz.grid_cells(BOX, 2)
    assert len(two) == 4 and two[0]["name"] == "r0c0" and two[3]["name"] == "r1c1"
    assert two[0]["top"] == 51.6 and two[3]["bottom"] == 51.36 and two[0]["left"] == -0.7 and two[3]["right"] == -0.25
    assert two[0]["bottom"] == two[2]["top"] == 51.48
    assert len(wz.grid_cells(BOX, 3)) == 9
    with pytest.raises(ValueError):
        wz.grid_cells(BOX, 4)
    with pytest.raises(ValueError):
        wz.grid_cells(BOX, 0)


def test_waze_strip_personal():
    raw = {"uuid": "u1", "type": "HAZARD", "subtype": "HAZARD_ON_SHOULDER_CAR_STOPPED",
           "reportBy": "Bob", "reportByUser": "bob", "reportRating": 3, "reporter": {"id": 1},
           "user": "x", "userName": "y", "username": "z", "avatar": "a", "imageUrl": "i", "imageId": "j",
           "thumbsUpBy": ["p"], "comments": [{"user": "q", "text": "t"}], "nThumbsUp": 2,
           "reportByMunicipalityUser": False, "location": {"x": -0.5, "y": 51.4}, "street": "M25"}
    clean = wz.strip_personal(raw)
    assert clean == {"uuid": "u1", "type": "HAZARD", "subtype": "HAZARD_ON_SHOULDER_CAR_STOPPED", "nThumbsUp": 2,
                     "reportByMunicipalityUser": False, "location": {"x": -0.5, "y": 51.4}, "street": "M25"}
    assert wz.strip_personal({"alerts": [raw], "users": [{"id": 1}]}) == {"alerts": [clean]}


def test_waze_classify():
    assert wz.classify(200, b'{"alerts": []}') == ("ok", {"alerts": []})
    assert wz.classify(403, b"<html>403 Forbidden</html>")[0] == "blocked"
    assert wz.classify(429, b'{"error": "slow down"}')[0] == "blocked"
    assert wz.classify(200, b"<html>challenge</html>")[0] == "blocked"
    assert wz.classify(302, b"")[0] == "blocked"
    assert wz.classify(500, b'{"e": 1}')[0] == "error"
    assert wz.classify(200, b"[1, 2]")[0] == "error"


def test_waze_summarise():
    now_ms = 1_800_000_000_000
    mk = lambda i, sub, typ="HAZARD", street="M25", rt=3, rel=7, age_min=4.0, lat=51.45, lon=-0.45: {  # noqa: E731
        "uuid": f"u{i}", "type": typ, "subtype": sub, "street": street, "roadType": rt, "reliability": rel,
        "pubMillis": now_ms - int(age_min * 60000), "location": {"x": lon, "y": lat}, "nThumbsUp": 0,
        "city": "Feltham", "magvar": 10, "confidence": 1, "reportDescription": "",
    }
    alerts = [
        mk(1, "HAZARD_ON_SHOULDER_CAR_STOPPED"),
        mk(2, "HAZARD_ON_ROAD_CAR_STOPPED", street="M4", age_min=12.0),
        mk(3, "HAZARD_ON_SHOULDER_CAR_STOPPED", street=None, lat=52.0, age_min=70.0),   # outside the box
        mk(4, "ACCIDENT_MINOR", typ="ACCIDENT", age_min=25.0),
        mk(5, "JAM_HEAVY_TRAFFIC", typ="JAM", age_min=7.0),
    ]
    s = wz.summarise(alerts, BOX, now_ms, raw_fields=set(wz.field_names(alerts[0])) | {"reportBy"})
    assert s["total"] == 5
    assert s["by_type"] == {"HAZARD": 3, "ACCIDENT": 1, "JAM": 1}
    assert s["car_stopped"] == 3 and s["accidents"] == 1
    assert s["car_stopped_by_subtype"] == {"HAZARD_ON_SHOULDER_CAR_STOPPED": 2, "HAZARD_ON_ROAD_CAR_STOPPED": 1}
    assert s["car_stopped_streets"] == ["(none)", "M25", "M4"]
    assert s["car_stopped_in_box"] == 2
    assert s["car_stopped_road_type"] == {"3": 3} and s["car_stopped_reliability"] == {"7": 3}
    assert s["age_buckets"] == {"0-5 min": 1, "5-10 min": 1, "10-20 min": 1, "20-60 min": 1, "60+ min": 1}
    assert s["age_min"] == 4.0 and s["age_max_min"] == 70.0 and s["age_median_min"] == 12.0
    assert s["expected_fields_missing"] == []
    assert s["personal_fields_dropped"] == ["reportBy"]
    assert "location.x" in s["fields_seen"] and "reportBy" not in s["fields_seen"]
    empty = wz.summarise([], BOX, now_ms)
    assert empty["total"] == 0 and empty["age_min"] is None


# --------------------------------------------------------------------------
# The scripts as the owner runs them
# --------------------------------------------------------------------------

def _run(script, *args, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([PY, str(REPO / "probes" / script), *args], cwd=REPO, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


@pytest.mark.parametrize("script", ["probe_waze.py", "probe_telegram.py"])
def test_probe_help_exits_zero(script):
    r = _run(script, "--help")
    assert r.returncode == 0, r.stderr
    assert "--max-requests" in r.stdout and "--out" in r.stdout


def test_probe_module_form_works():
    r = subprocess.run([PY, "-m", "probes.probe_waze", "--help"], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_probe_telegram_without_token_exits_3(tmp_path):
    out = tmp_path / "run"
    r = _run("probe_telegram.py", "--out", str(out), env_extra={"TELEGRAM_BOT_TOKEN": ""})
    assert r.returncode == 3
    assert "Set TELEGRAM_BOT_TOKEN in .env (see .env.example)" in r.stderr
    assert not out.exists()          # nothing was written, so the folder was removed


def test_probe_telegram_send_without_chat_id_exits_3(tmp_path):
    out = tmp_path / "run"
    r = _run("probe_telegram.py", "--send", "--out", str(out),
             env_extra={"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": ""})
    assert r.returncode == 3
    assert "--chat-id" in r.stderr and FAKE_TOKEN not in r.stdout + r.stderr


def test_probe_waze_refuses_grid_over_3(tmp_path):
    r = _run("probe_waze.py", "--grid", "4", "--out", str(tmp_path / "run"))
    assert r.returncode == 1 and "--grid must be between 1 and 3" in r.stderr
