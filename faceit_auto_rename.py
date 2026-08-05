import http.server
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import obspython as obs

API_BASE = "https://open.faceit.com/data/v4"
CREATE_NO_WINDOW = 0x08000000

g_api_key = ""
g_nickname = ""
g_display_name = ""
g_template_faceit = "{nickname} {score}｜KDA {kda}｜Rating {rating}｜{map}｜FACEIT｜{date}"
g_template_other = "{nickname} {score}｜KDA {kda}｜{map}｜{platform}｜{date}"
g_max_wait_min = 12
g_poll_secs = 30
g_gsi_enabled = True
g_gsi_port = 3456
g_stop_delay = 20
g_auto_upload = False
g_upload_privacy = "private"

g_player_id = None
g_recording_start_ts = 0.0
g_last_renamed_match = None
g_script_dir = ""

g_gsi_server = None
g_last_phase = None
g_want_start = False
g_want_stop_at = 0.0
g_started_by_gsi = False
g_last_map_ts = 0.0
g_gsi_match = {}
g_gsi_lock = threading.Lock()

FORBIDDEN_CHARS = {
    "<": "‹", ">": "›", ":": "꞉", '"': "”",
    "/": "⁄", "\\": "⧵", "|": "｜", "?": "？", "*": "＊",
}


g_log_queue = []
g_log_lock = threading.Lock()


def log(msg):
    with g_log_lock:
        g_log_queue.append(f"[faceit-rename] {msg}")


def flush_logs():
    with g_log_lock:
        pending = list(g_log_queue)
        g_log_queue.clear()
    for line in pending:
        print(line)


def api_get(path):
    req = urllib.request.Request(API_BASE + path, headers={
        "Authorization": f"Bearer {g_api_key}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


class NicknameNotFound(Exception):
    pass


def resolve_player_id():
    global g_player_id
    if g_player_id:
        return g_player_id
    try:
        data = api_get("/players?nickname=" + urllib.parse.quote(g_nickname))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NicknameNotFound(g_nickname)
        raise
    g_player_id = data["player_id"]
    log(f"resolved {g_nickname} -> {g_player_id}")
    return g_player_id


def to_int(stats, key):
    try:
        return int(float(stats.get(key, 0)))
    except (TypeError, ValueError):
        return 0


def hltv_rating(kills, deaths, rounds, dbl, tpl, qd, pt, kd_fallback):
    if rounds <= 0:
        return kd_fallback
    ones = kills - 2 * dbl - 3 * tpl - 4 * qd - 5 * pt
    if ones < 0:
        return kd_fallback
    kill_rating = (kills / rounds) / 0.679
    surv_rating = ((rounds - deaths) / rounds) / 0.317
    multi_rating = ((ones + 4 * dbl + 9 * tpl + 16 * qd + 25 * pt) / rounds) / 1.277
    return (kill_rating + 0.7 * surv_rating + multi_rating) / 2.7


def clean_map(name):
    return (name or "unknown").replace("de_", "").replace("cs_", "").upper()


def sanitize(name):
    for bad, good in FORBIDDEN_CHARS.items():
        name = name.replace(bad, good)
    return name


def build_faceit_name(match_stats, finished_at):
    pid = resolve_player_id()
    round0 = match_stats["rounds"][0]
    round_stats = round0.get("round_stats", {})

    my_team, other_team, my_stats = None, None, None
    for team in round0.get("teams", []):
        found = None
        for p in team.get("players", []):
            if p.get("player_id") == pid:
                found = p
        if found:
            my_team, my_stats = team, found.get("player_stats", {})
        else:
            other_team = team
    if not my_stats:
        raise RuntimeError("player not found in match stats")

    my_score = my_team.get("team_stats", {}).get("Final Score")
    their_score = (other_team or {}).get("team_stats", {}).get("Final Score")
    if my_score is None or their_score is None:
        parts = round_stats.get("Score", "0 / 0").replace(" ", "").split("/")
        my_score, their_score = parts[0], parts[-1]
    score = f"{my_score}-{their_score}"

    rounds = to_int(round_stats, "Rounds")
    if rounds <= 0:
        rounds = to_int({"s": my_score}, "s") + to_int({"s": their_score}, "s")

    kills = to_int(my_stats, "Kills")
    deaths = to_int(my_stats, "Deaths")
    assists = to_int(my_stats, "Assists")
    try:
        kd = float(my_stats.get("K/D Ratio", 0))
    except (TypeError, ValueError):
        kd = 0.0
    rating = hltv_rating(
        kills, deaths, rounds,
        to_int(my_stats, "Double Kills"),
        to_int(my_stats, "Triple Kills"),
        to_int(my_stats, "Quadro Kills"),
        to_int(my_stats, "Penta Kills"),
        kd,
    )

    name = g_template_faceit.format(
        nickname=g_display_name or g_nickname,
        score=score,
        rating=f"{rating:.2f}",
        kd=f"{kd:.2f}",
        kda=f"{kills}/{deaths}",
        kills=kills,
        deaths=deaths,
        assists=assists,
        map=clean_map(round_stats.get("Map")),
        platform="FACEIT",
        date=datetime.fromtimestamp(finished_at).strftime("%b %d, %Y"),
    )
    return sanitize(name)


def build_gsi_name(snap, platform, when_ts):
    ct, t = snap.get("ct", 0), snap.get("t", 0)
    if snap.get("team") == "T":
        my, their = t, ct
    else:
        my, their = ct, t
    kills = snap.get("kills", 0)
    deaths = snap.get("deaths", 0)
    kd = kills / deaths if deaths else float(kills)

    name = g_template_other.format(
        nickname=g_display_name or g_nickname or snap.get("name", "player"),
        score=f"{my}-{their}",
        rating=f"{kd:.2f}",
        kd=f"{kd:.2f}",
        kda=f"{kills}/{deaths}",
        kills=kills,
        deaths=deaths,
        assists=snap.get("assists", 0),
        map=clean_map(snap.get("map")),
        platform=platform,
        date=datetime.fromtimestamp(when_ts).strftime("%b %d, %Y"),
    )
    return sanitize(name)


def rename_recording(path, new_stem):
    folder = os.path.dirname(path)
    old_stem = os.path.splitext(os.path.basename(path))[0]
    renamed = []
    for fname in os.listdir(folder):
        stem, ext = os.path.splitext(fname)
        if stem != old_stem:
            continue
        src = os.path.join(folder, fname)
        dst = os.path.join(folder, new_stem + ext)
        for attempt in range(6):
            try:
                os.rename(src, dst)
                renamed.append(dst)
                break
            except OSError as e:
                if attempt == 5:
                    log(f"failed to rename {src}: {e}")
                else:
                    time.sleep(5)
    return renamed


def start_upload(files, stem):
    video = None
    for f in files:
        if f.lower().endswith(".mp4"):
            video = f
    if video is None:
        video = files[0]
    uploader = os.path.join(g_script_dir, "youtube_upload.py")
    if not os.path.exists(uploader):
        log(f"uploader not found: {uploader}")
        return
    title = stem.replace("｜", " | ")
    cmd = ["py", "-3", uploader, video,
           "--title", title, "--privacy", g_upload_privacy]
    try:
        subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW, cwd=g_script_dir)
        log(f"upload started: {os.path.basename(video)} ({g_upload_privacy})")
    except Exception as e:
        log(f"failed to start upload: {e}")


def finish(path, new_stem):
    renamed = rename_recording(path, new_stem)
    if renamed:
        for f in renamed:
            log(f"renamed -> {f}")
        if g_auto_upload:
            start_upload(renamed, new_stem)
    return renamed


def worker(path, started_ts, stopped_ts, snap):
    global g_last_renamed_match
    mode = snap.get("mode", "")
    has_gsi = bool(snap.get("map"))

    if mode == "premier":
        finish(path, build_gsi_name(snap, "PREMIER", stopped_ts))
        return
    if mode == "scrimcomp2v2":
        finish(path, build_gsi_name(snap, "WINGMAN", stopped_ts))
        return
    if has_gsi and snap.get("knife"):
        finish(path, build_gsi_name(snap, "GC", stopped_ts))
        return
    if not g_api_key or not g_nickname:
        if has_gsi:
            finish(path, build_gsi_name(snap, "MM", stopped_ts))
        else:
            log("set nickname and API key in the script properties")
        return

    deadline = stopped_ts + g_max_wait_min * 60
    log(f"waiting for FACEIT stats to rename: {path}")
    while time.time() < deadline:
        try:
            try:
                pid = resolve_player_id()
            except NicknameNotFound as e:
                log(f"FACEIT nickname not found: {e} — check the script settings")
                break
            hist = api_get(f"/players/{pid}/history?game=cs2&offset=0&limit=1")
            items = hist.get("items", [])
            if items:
                match = items[0]
                finished_at = match.get("finished_at", 0)
                match_id = match.get("match_id")
                if finished_at >= started_ts - 120 and match_id != g_last_renamed_match:
                    stats = api_get(f"/matches/{match_id}/stats")
                    new_stem = build_faceit_name(stats, finished_at)
                    if finish(path, new_stem):
                        g_last_renamed_match = match_id
                    return
        except urllib.error.HTTPError as e:
            if e.code == 404:
                pass
            elif e.code == 401:
                log("invalid API key (401), check developers.faceit.com")
                break
            else:
                log(f"HTTP {e.code}: {e.reason}")
        except Exception as e:
            log(f"error: {e}")
        time.sleep(g_poll_secs)

    if has_gsi:
        log("no FACEIT match found, falling back to MM stats from GSI")
        finish(path, build_gsi_name(snap, "MM", stopped_ts))
    else:
        log("gave up: no FACEIT match and no GSI data, keeping original filename")


class GsiHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        global g_last_phase, g_want_start, g_want_stop_at, g_last_map_ts
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            payload = {}
        self.send_response(200)
        self.end_headers()

        map_info = payload.get("map") or {}
        phase = map_info.get("phase")
        mode = map_info.get("mode", "")

        if phase != g_last_phase:
            if phase == "live" and mode in ("competitive", "premier"):
                if g_last_phase in (None, "warmup"):
                    with g_gsi_lock:
                        g_gsi_match.clear()
                    g_want_start = True
                    g_want_stop_at = 0.0
                    log(f"GSI: match live (mode={mode}, map={map_info.get('name')})")
            elif phase == "gameover":
                g_want_stop_at = time.time() + g_stop_delay
            g_last_phase = phase

        if not phase:
            return
        g_last_map_ts = time.time()

        provider_sid = (payload.get("provider") or {}).get("steamid")
        player = payload.get("player") or {}
        with g_gsi_lock:
            g_gsi_match["mode"] = mode
            g_gsi_match["map"] = map_info.get("name")
            g_gsi_match["ct"] = (map_info.get("team_ct") or {}).get("score", 0)
            g_gsi_match["t"] = (map_info.get("team_t") or {}).get("score", 0)
            if provider_sid and player.get("steamid") == provider_sid:
                stats = player.get("match_stats") or {}
                g_gsi_match["name"] = player.get("name")
                g_gsi_match["team"] = player.get("team")
                g_gsi_match["kills"] = stats.get("kills", 0)
                g_gsi_match["deaths"] = stats.get("deaths", 0)
                g_gsi_match["assists"] = stats.get("assists", 0)
                if (phase == "live" and mode == "competitive"
                        and map_info.get("round", -1) == 0):
                    weapons = player.get("weapons") or {}
                    names = [w.get("name", "") for w in weapons.values()]
                    if names and all(n.startswith("weapon_knife") for n in names):
                        g_gsi_match["knife"] = True

    def log_message(self, fmt, *args):
        pass


def gsi_tick():
    global g_want_start, g_want_stop_at, g_started_by_gsi
    flush_logs()
    if g_want_start:
        g_want_start = False
        if not obs.obs_frontend_recording_active():
            obs.obs_frontend_recording_start()
            g_started_by_gsi = True
            log("GSI: recording started")
    if g_want_stop_at and time.time() >= g_want_stop_at:
        g_want_stop_at = 0.0
        if obs.obs_frontend_recording_active():
            obs.obs_frontend_recording_stop()
            log("GSI: match over, recording stopped")
    if (g_started_by_gsi and g_last_map_ts
            and time.time() - g_last_map_ts > 45
            and obs.obs_frontend_recording_active()):
        obs.obs_frontend_recording_stop()
        log("GSI: left the server, recording stopped")


def start_gsi_server():
    global g_gsi_server
    if g_gsi_server is not None or not g_gsi_enabled:
        return
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", g_gsi_port),
                                                 GsiHandler)
    except OSError as e:
        log(f"GSI server failed on port {g_gsi_port}: {e}")
        return
    g_gsi_server = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"GSI server listening on 127.0.0.1:{g_gsi_port}")


def stop_gsi_server():
    global g_gsi_server
    if g_gsi_server is not None:
        server = g_gsi_server
        g_gsi_server = None
        threading.Thread(target=lambda: (server.shutdown(),
                                         server.server_close()),
                         daemon=True).start()


def on_event(event):
    global g_recording_start_ts, g_started_by_gsi
    if event == obs.OBS_FRONTEND_EVENT_RECORDING_STARTED:
        g_recording_start_ts = time.time()
    elif event == obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED:
        g_started_by_gsi = False
        path = obs.obs_frontend_get_last_recording()
        if not path:
            log("could not get last recording path")
            return
        with g_gsi_lock:
            snap = dict(g_gsi_match)
        started = g_recording_start_ts or (time.time() - 3600)
        t = threading.Thread(target=worker,
                             args=(path, started, time.time(), snap),
                             daemon=True)
        t.start()


def script_description():
    return ("<b>FACEIT Auto Record</b><br>"
            "Starts/stops recording with CS2 matches (game state integration), "
            "renames the file with match stats (FACEIT API for FACEIT games, "
            "in-game stats for MM/Premier/GC) and optionally uploads it to "
            "YouTube.<br>Requires a free API key from "
            "<a href='https://developers.faceit.com'>developers.faceit.com</a>.")


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_text(props, "nickname", "FACEIT nickname",
                                obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "display_name",
                                "Display name in filename (optional)",
                                obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "api_key", "FACEIT API key (server-side)",
                                obs.OBS_TEXT_PASSWORD)
    obs.obs_properties_add_text(props, "template_faceit",
                                "FACEIT template",
                                obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "template_other",
                                "MM/Premier/GC template",
                                obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_int(props, "max_wait_min",
                               "Wait for FACEIT stats (min)", 2, 60, 1)
    obs.obs_properties_add_int(props, "poll_secs",
                               "Retry interval (s)", 10, 120, 5)
    obs.obs_properties_add_bool(props, "gsi_enabled",
                                "Auto start/stop recording with CS2 (GSI)")
    obs.obs_properties_add_int(props, "gsi_port", "GSI port", 1024, 65535, 1)
    obs.obs_properties_add_int(props, "stop_delay",
                               "Stop delay after game over (s)", 0, 120, 5)
    obs.obs_properties_add_bool(props, "auto_upload",
                                "Upload to YouTube after rename")
    plist = obs.obs_properties_add_list(props, "upload_privacy",
                                        "YouTube privacy",
                                        obs.OBS_COMBO_TYPE_LIST,
                                        obs.OBS_COMBO_FORMAT_STRING)
    for v in ("private", "unlisted", "public"):
        obs.obs_property_list_add_string(plist, v, v)
    return props


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, "template_faceit", g_template_faceit)
    obs.obs_data_set_default_string(settings, "template_other", g_template_other)
    obs.obs_data_set_default_int(settings, "max_wait_min", 6)
    obs.obs_data_set_default_int(settings, "poll_secs", 30)
    obs.obs_data_set_default_bool(settings, "gsi_enabled", True)
    obs.obs_data_set_default_int(settings, "gsi_port", 3456)
    obs.obs_data_set_default_int(settings, "stop_delay", 20)
    obs.obs_data_set_default_bool(settings, "auto_upload", False)
    obs.obs_data_set_default_string(settings, "upload_privacy", "private")


def script_update(settings):
    global g_api_key, g_nickname, g_template_faceit, g_template_other
    global g_max_wait_min, g_poll_secs, g_player_id
    global g_gsi_enabled, g_gsi_port, g_stop_delay
    global g_auto_upload, g_upload_privacy

    global g_display_name
    new_nick = obs.obs_data_get_string(settings, "nickname").strip()
    if new_nick != g_nickname:
        g_player_id = None
    g_nickname = new_nick
    g_display_name = obs.obs_data_get_string(settings, "display_name").strip()
    g_api_key = obs.obs_data_get_string(settings, "api_key").strip()
    g_template_faceit = (obs.obs_data_get_string(settings, "template_faceit")
                         or g_template_faceit)
    g_template_other = (obs.obs_data_get_string(settings, "template_other")
                        or g_template_other)
    g_max_wait_min = obs.obs_data_get_int(settings, "max_wait_min")
    g_poll_secs = obs.obs_data_get_int(settings, "poll_secs")
    g_stop_delay = obs.obs_data_get_int(settings, "stop_delay")
    g_auto_upload = obs.obs_data_get_bool(settings, "auto_upload")
    g_upload_privacy = obs.obs_data_get_string(settings, "upload_privacy") or "private"

    new_enabled = obs.obs_data_get_bool(settings, "gsi_enabled")
    new_port = obs.obs_data_get_int(settings, "gsi_port")
    if new_enabled != g_gsi_enabled or new_port != g_gsi_port:
        g_gsi_enabled, g_gsi_port = new_enabled, new_port
        stop_gsi_server()
        start_gsi_server()
    elif g_gsi_enabled and g_gsi_server is None:
        start_gsi_server()


def script_load(settings):
    global g_script_dir
    try:
        g_script_dir = script_path()
    except NameError:
        g_script_dir = os.path.dirname(os.path.abspath(__file__))
    obs.obs_frontend_add_event_callback(on_event)
    obs.timer_add(gsi_tick, 1000)
    start_gsi_server()
    log("loaded")


def script_unload():
    obs.timer_remove(gsi_tick)
    stop_gsi_server()
    flush_logs()
