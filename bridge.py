#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мост: Spotify (SMTC) -> текст песни (Spicy Lyrics / LRCLIB) -> виджет Rainmeter.

Рисует активную строку с подсветкой слова в PNG для скина LyricsGlass.

Запуск:  python bridge.py            (виджет + HTTP-сервер)
         python bridge.py --demo     (демо без Spotify)
         python bridge.py --window   (дополнительно откроет HTML-окно)
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
CACHE = BASE / "cache"
CACHE.mkdir(exist_ok=True)

CONFIG = {}
cfg_path = BASE / "config.json"
if cfg_path.exists():
    CONFIG = json.loads(cfg_path.read_text(encoding="utf-8"))

SPICY_KEY = (CONFIG.get("spicy_key") or "").strip()
PORT = int(CONFIG.get("port", 8765))
UA = {"User-Agent": "spotify-glass-lyrics-bridge/1.0 (local personal use)"}

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
    PIL_OK = True
except Exception:
    PIL_OK = False

SCALE = 1                                  # 1 = без суперсэмпла (60 фпс), 2 = retina
K = 2.25                                   # общий масштаб виджета (390 → 877)
WIDGET_W = int(CONFIG.get("widget_width", int(390 * K * 0.85)))
WIDGET_PAD = int(18 * K)
# сдвиг подсветки относительно звука (мс): положительное = текст показывается
# раньше звука (опережение), отрицательное = позже (отставание)
LAG_OFFSET = int(CONFIG.get("lag_offset_ms", 0))
DEFAULT_PNG = (Path.home() / "Documents" / "Rainmeter" / "Skins" /
               "LyricsGlass" / "@Resources" / "lyrics.png")
WIDGET_PNG = Path(CONFIG.get("widget_png") or DEFAULT_PNG)
# Пинг-понг кадров: Rainmeter показывает один файл (lyrics_a.png), мост пишет
# другой (lyrics_b.png) и переключает маркер frame.txt; Lua в скине меняет
# ImageName и подтверждает в ack.txt. Читатель и писатель никогда не работают
# с одним файлом — гонки, из-за которых метр обнулялся до 1x1 и виджет мигал,
# исключены конструктивно.
WIDGET_DIR = WIDGET_PNG.parent
FRAME_PATHS = {
    "a": WIDGET_PNG.with_name(WIDGET_PNG.stem + "_a.png"),
    "b": WIDGET_PNG.with_name(WIDGET_PNG.stem + "_b.png"),
}
FRAME_MARK = WIDGET_DIR / "frame.txt"    # мост -> Rainmeter: что показывать
FRAME_ACK = WIDGET_DIR / "ack.txt"       # Rainmeter -> мост: что показано

LOCK = threading.Lock()
STATE = {
    "connected": False,
    "status": "stopped",          # playing | paused | stopped
    "position_ms": 0,
    "title": "",
    "artist": "",
    "duration_ms": 0,
    "lyrics_id": None,
    "lyrics_status": "idle",      # idle | loading | ready | none
    "line_index": -1,
    "word_index": -1,
    "error": None,
}
LYRICS = {
    "id": None,
    "word_sync": "none",          # exact | estimated | none
    "source": "",
    "credit": None,               # {provider, uploader:{username,url}, maker:{...}}
    "lines": [],                  # [{start, end, text, words:[{start,end,text}]|null}]
}


# ---------------------------------------------------------------- SMTC (Win)

def ts_ms(v):
    """TimeSpan из winsdk -> миллисекунды."""
    if v is None:
        return None
    if hasattr(v, "duration"):
        return int(v.duration // 10000)
    if hasattr(v, "total_seconds"):
        return int(v.total_seconds() * 1000)
    if isinstance(v, (int, float)):
        return int(v // 10000) if v > 1_000_000 else int(v)
    return None


def pick_session(mgr):
    try:
        sessions = list(mgr.get_sessions())
    except Exception:
        sessions = []
    spotify = None
    for s in sessions:
        try:
            aumid = (s.source_app_user_model_id or "").lower()
        except Exception:
            aumid = ""
        if "spotify" in aumid:
            spotify = s
            break
    if spotify:
        return spotify
    try:
        return mgr.get_current_session()
    except Exception:
        return None


def map_status(ps):
    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as St,
        )
        if ps == St.PLAYING:
            return "playing"
        if ps == St.PAUSED:
            return "paused"
        return "stopped"
    except Exception:
        return "stopped"


async def poll_loop():
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SMTC,
    )
    mgr = await SMTC.request_async()

    last_rep = None
    last_status = None
    anchor_pos = 0
    anchor_t = time.monotonic()
    playing = False
    cur_key = None

    while True:
        try:
            s = pick_session(mgr)
            if s is None:
                with LOCK:
                    STATE.update(connected=False, status="stopped",
                                 position_ms=0, line_index=-1, word_index=-1)
                last_rep = None
                await asyncio.sleep(0.4)
                continue

            props = await s.try_get_media_properties_async()
            info = s.get_playback_info()
            tl = s.get_timeline_properties()

            title = (props.title or "").strip()
            artist = (props.artist or "").strip()
            start_ms = ts_ms(tl.start_time)
            end_ms = ts_ms(tl.end_time)
            duration = (end_ms - start_ms) if (start_ms is not None and end_ms is not None and end_ms > start_ms) else 0
            status = map_status(info.playback_status)
            rep = ts_ms(tl.position) or 0

            now = time.monotonic()
            if rep != last_rep or status != last_status:
                anchor_pos, anchor_t = rep, now
                last_rep, last_status = rep, status
            playing = (status == "playing")
            pos = int(anchor_pos + (now - anchor_t) * 1000) if playing else int(anchor_pos)
            pos += LAG_OFFSET
            if pos < 0:
                pos = 0
            if duration and pos > duration:
                pos = duration

            key = f"{title}|{artist}|{duration}"
            if key != cur_key:
                cur_key = key
                start_lyrics_job(key, title, artist, duration)

            with LOCK:
                STATE.update(
                    connected=bool(title or artist),
                    status=status,
                    position_ms=pos,
                    title=title,
                    artist=artist,
                    duration_ms=duration,
                    anchor_pos=anchor_pos,
                    anchor_t=anchor_t,
                )
                compute_indices(pos)

        except Exception as e:
            with LOCK:
                STATE["error"] = f"smtc: {e}"

        await asyncio.sleep(0.05)


# ------------------------------------------------------- текст: получение

def compute_indices(pos):
    if LYRICS["id"] != STATE["lyrics_id"] or not LYRICS["lines"]:
        STATE["line_index"] = -1
        STATE["word_index"] = -1
        return
    lines = LYRICS["lines"]
    idx = -1
    for i, ln in enumerate(lines):
        st = ln.get("start")
        if st is not None and pos >= st:
            idx = i
        else:
            break
    widx = -1
    if idx >= 0:
        words = lines[idx].get("words")
        if words:
            for j, w in enumerate(words):
                if w.get("start") is not None and pos >= w["start"]:
                    widx = j
    STATE["line_index"] = idx
    STATE["word_index"] = widx


def start_lyrics_job(key, title, artist, duration):
    if not title:
        return
    with LOCK:
        STATE["lyrics_id"] = key
        STATE["lyrics_status"] = "loading"
        STATE["error"] = None
    threading.Thread(target=fetch_lyrics_job, args=(key, title, artist, duration),
                     daemon=True).start()


def estimate_words(text, start, end):
    toks = text.split()
    if not toks or start is None or end is None or end <= start:
        return None
    weights = [len(t) + 1 for t in toks]
    total = sum(weights)
    words, t = [], float(start)
    for tok, w in zip(toks, weights):
        dur = (end - start) * w / total
        words.append({"start": int(t), "end": int(t + dur), "text": tok})
        t += dur
    return words


def finish(lyrics_id, lines, word_sync, source, credit):
    lines.sort(key=lambda l: (l.get("start") is None, l.get("start") or 0))
    for i, ln in enumerate(lines):
        if ln.get("end") is None:
            nxt = lines[i + 1].get("start") if i + 1 < len(lines) else None
            ln["end"] = nxt
    with LOCK:
        if STATE["lyrics_id"] != lyrics_id:
            return  # трек уже сменился — не затираем актуальное состояние
        LYRICS.update(id=lyrics_id, lines=lines, word_sync=word_sync,
                      source=source, credit=credit)
        STATE["lyrics_status"] = "ready" if lines else "none"
        compute_indices(STATE["position_ms"])


def fetch_lyrics_job(key, title, artist, duration):
    with LOCK:
        if STATE["lyrics_id"] != key:
            return
    cache_f = CACHE / (re.sub(r"[^A-Za-z0-9]+", "_", key)[:120] + ".json")
    if cache_f.exists():
        try:
            data = json.loads(cache_f.read_text(encoding="utf-8"))
            # кэш валиден, если Spicy уже проверялся (или ключа нет)
            if data.get("spicy_checked") or not SPICY_KEY:
                finish(key, data.get("lines") or [],
                       data.get("word_sync", "none"),
                       data.get("source", ""), data.get("credit"))
                return
        except Exception:
            pass

    try:
        spicy_checked = False
        spicy_res = None
        if SPICY_KEY:
            spicy_checked = True
            spicy_res = fetch_spicy(title, artist, duration)

        # приоритет: синхронизированный текст > статичный
        pick = None
        if spicy_res and spicy_res[1] != "none":
            pick = ("Spicy Lyrics", spicy_res)
        else:
            lr_res = fetch_lrclib(title, artist, duration)
            if lr_res and lr_res[1] != "none":
                pick = ("LRCLIB", lr_res)
            elif spicy_res:
                pick = ("Spicy Lyrics", spicy_res)
            elif lr_res:
                pick = ("LRCLIB", lr_res)

        if pick:
            src, payload = pick
            lines, word_sync = payload[0], payload[1]
            credit = payload[2] if len(payload) > 2 else {"provider": "LRCLIB.net"}
            cache_f.write_text(json.dumps(
                {"lines": lines, "word_sync": word_sync,
                 "source": src, "credit": credit,
                 "spicy_checked": spicy_checked},
                ensure_ascii=False), encoding="utf-8")
            finish(key, lines, word_sync, src, credit)
            return

        # текста нет нигде — кэшируем промах, чтобы не дёргать API каждый раз
        if spicy_checked:
            cache_f.write_text(json.dumps(
                {"lines": [], "word_sync": "none", "source": "",
                 "credit": None, "spicy_checked": True},
                ensure_ascii=False), encoding="utf-8")

        with LOCK:
            STATE["lyrics_status"] = "none"
    except Exception as e:
        with LOCK:
            STATE["lyrics_status"] = "none"
            STATE["error"] = f"lyrics: {e}"


# --- Spicy Lyrics ---------------------------------------------------------

def spicy_get(path, **params):
    r = requests.get(
        f"https://api.spicylyrics.org{path}",
        params=params,
        headers={**UA, "Authorization": f"Bearer {SPICY_KEY}"},
        timeout=15,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("Body")


def fetch_spicy(title, artist, duration):
    body = spicy_get("/v1/tracks/search", title=title, artist=artist,
                     **({"duration": duration / 1000.0} if duration else {}))
    if not body or not body.get("results"):
        body = spicy_get("/v1/tracks/search", q=f"{artist} {title}",
                         **({"duration": duration / 1000.0} if duration else {}))
    if not body or not body.get("results"):
        return None
    pick = None
    for tier in ("exact", "likely"):
        for r in body["results"]:
            if r.get("tier") == tier:
                pick = r
                break
        if pick:
            break
    if not pick:
        return None
    lyr = spicy_get(f"/v1/lyrics/{pick['id']}")
    if not lyr:
        return None

    credit = {"provider": "Spicy Lyrics"}
    ua = lyr.get("UploadAttribution")
    if lyr.get("source") == "spicy_lyrics" and ua:
        if ua.get("Uploader"):
            credit["uploader"] = {"username": ua["Uploader"].get("username"),
                                  "url": ua["Uploader"].get("url")}
        if ua.get("Maker"):
            credit["maker"] = {"username": ua["Maker"].get("username"),
                               "url": ua["Maker"].get("url")}

    t = lyr.get("Type")
    lines = []
    if t == "Syllable":
        for ln in lyr.get("Content", []):
            if ln.get("Type") != "Vocal":
                continue
            lead = ln.get("Lead") or {}
            syls = lead.get("Syllables") or []
            words, cur = [], None
            for s in syls:
                txt = s.get("Text") or ""
                st, en = _sec(s.get("StartTime")), _sec(s.get("EndTime"))
                if cur is None:
                    cur = {"text": txt, "start": st, "end": en}
                else:
                    cur["text"] += txt
                    if en is not None:
                        cur["end"] = en
                if not s.get("IsPartOfWord"):
                    words.append(cur)
                    cur = None
            if cur:
                words.append(cur)
            if not words:
                continue
            has_time = all(w["start"] is not None and w["end"] is not None for w in words)
            start = words[0]["start"] if has_time else _sec(lead.get("StartTime"))
            end = words[-1]["end"] if has_time else _sec(lead.get("EndTime"))
            lines.append({"start": start, "end": end,
                          "text": " ".join(w["text"] for w in words),
                          "words": words if has_time else None})
        sync = "exact" if lines and lines[0].get("words") else "none"
    elif t == "Line":
        for ln in lyr.get("Content", []):
            if ln.get("Type") != "Vocal":
                continue
            text = (ln.get("Text") or "").strip()
            if not text:
                continue
            start, end = _sec(ln.get("StartTime")), _sec(ln.get("EndTime"))
            lines.append({"start": start, "end": end, "text": text,
                          "words": estimate_words(text, start, end)})
        sync = "estimated"
    else:  # Static
        for ln in lyr.get("Lines", []):
            text = (ln.get("Text") or "").strip()
            if text:
                lines.append({"start": None, "end": None, "text": text, "words": None})
        sync = "none"

    if not lines:
        return None
    return lines, sync, credit


def _sec(v):
    return None if v is None else int(round(float(v) * 1000))


# --- LRCLIB ---------------------------------------------------------------

LRC_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text):
    stamps = []
    for raw in text.splitlines():
        ms = list(LRC_RE.finditer(raw))
        if not ms:
            continue
        content = raw[ms[-1].end():].strip()
        for m in ms:
            mm, ss = int(m.group(1)), int(m.group(2))
            frac = m.group(3) or "0"
            t = int(mm * 60000 + ss * 1000 + float("0." + frac) * 1000)
            stamps.append((t, content))
    stamps.sort(key=lambda x: x[0])
    lines = []
    for i, (t, content) in enumerate(stamps):
        if not content:
            continue
        end = stamps[i + 1][0] if i + 1 < len(stamps) else None
        lines.append({"start": t, "end": end, "text": content, "words": None})
    return lines


def fetch_lrclib(title, artist, duration):
    def get(url, **params):
        r = requests.get(url, params=params, headers=UA, timeout=15)
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return r.json()

    body = get("https://lrclib.net/api/get", track_name=title,
               artist_name=artist,
               **({"duration": round(duration / 1000.0, 2)} if duration else {}))
    if body is None:
        q = get("https://lrclib.net/api/search", q=f"{artist} {title}")
        if not q:
            return None
        cand = None
        if duration:
            best = 999
            for c in q:
                d = abs((c.get("duration") or 0) - duration / 1000.0)
                if d < best:
                    best, cand = d, c
            if best > 3:
                cand = None
        body = cand or q[0]

    sync_text = body.get("syncedLyrics")
    if sync_text:
        lines = parse_lrc(sync_text)
        for ln in lines:
            ln["words"] = estimate_words(ln["text"], ln["start"], ln["end"])
        if lines:
            return lines, "estimated"
    plain = body.get("plainLyrics")
    if plain:
        lines = [{"start": None, "end": None, "text": t, "words": None}
                 for t in plain.splitlines() if t.strip()]
        return lines, "none"
    return None


# ------------------------------------------- виджет PNG (стиль SpotifyGlass)
# Палитра и панель — как в SpotifyGlass.ini: Text/Muted, стекло 18,20,26,22
COL_TEXT = (245, 247, 250, 245)
COL_MUTED = (190, 198, 210, 205)
COL_SIDE = (190, 198, 210, 130)
COL_PEND = (190, 198, 210, 150)
COL_HINT = (150, 160, 174, 170)
COL_DIVIDER = (255, 255, 255, 24)
PANEL_FILL = (18, 20, 26, 22)
PANEL_STROKE = (255, 255, 255, 22)

_FONT_CACHE = {}


def _font(size, bold=False):
    key = (round(size, 1), bold)
    if key not in _FONT_CACHE:
        name = "segoeuib.ttf" if bold else "segoeui.ttf"
        path = Path("C:/Windows/Fonts") / name
        _FONT_CACHE[key] = ImageFont.truetype(str(path), int(round(size * SCALE)))
    return _FONT_CACHE[key]


def _fh(font):
    b = font.getbbox("Agy")
    return b[3] - b[1]


def _wrap_text(text, font, max_w):
    words = text.split()
    if not words:
        return [""]
    rows, cur = [], ""
    for w in words:
        t = w if not cur else cur + " " + w
        if font.getlength(t) <= max_w or not cur:
            cur = t
        else:
            rows.append(cur)
            cur = w
    rows.append(cur)
    return rows


def _wrap_words(words, font, max_w):
    rows, cur, cur_w = [], [], 0.0
    space = font.getlength(" ")
    for j, w in enumerate(words):
        if not w:
            continue
        ww = font.getlength(w)
        add = ww if not cur else space + ww
        if cur and cur_w + add > max_w:
            rows.append(cur)
            cur = [(j, w)]
            cur_w = ww
        else:
            cur.append((j, w))
            cur_w += add
    if cur:
        rows.append(cur)
    return rows or [[(0, "")]]


def _credit_text(sync, credit, source):
    label = {"exact": "пословно", "estimated": "построчно"}.get(sync, "")
    prov = (credit or {}).get("provider") or source or ""
    parts = []
    if label:
        parts.append(label)
    if prov:
        parts.append("Текст: " + prov)
    if credit:
        up = credit.get("uploader") or {}
        mk = credit.get("maker") or {}
        if up.get("username"):
            s = "загрузил " + up["username"]
            if up.get("url"):
                s += " " + up["url"]
            parts.append(s)
        if mk.get("username"):
            s = "собрал " + mk["username"]
            if mk.get("url"):
                s += " " + mk["url"]
            parts.append(s)
    return "  ·  ".join(parts)


def _smooth(t):
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return t * t * (3 - 2 * t)


def _lerp_col(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(4))


def _wipe_word(layer, x, y, wtxt, font, asc, prog, ww):
    """Белый плавно проникает в слово слева направо, кромка мягкая."""
    wpx = int(math.ceil(ww)) + 4
    hpx = asc + font.getmetrics()[1] + 2
    soft = max(6.0 * SCALE, min(ww * 0.35, 16.0 * SCALE))
    ex = -soft + prog * (ww + 2 * soft)
    row = bytearray(wpx)
    for xx in range(wpx):
        a = (ex - xx) / soft
        row[xx] = 0 if a <= 0 else (255 if a >= 1 else int(a * 255))
    mask = Image.frombytes("L", (wpx, 1), bytes(row)).resize((wpx, hpx))
    white = Image.new("RGBA", (wpx, hpx), (0, 0, 0, 0))
    ImageDraw.Draw(white).text((0, asc), wtxt, font=font,
                               fill=(255, 255, 255, 255), anchor="ls")
    white.putalpha(ImageChops.multiply(white.getchannel("A"), mask))
    layer.paste(white, (int(x), int(y)), white)


def build_widget_image():
    if not PIL_OK:
        return None
    with LOCK:
        st = dict(STATE)
        same = LYRICS.get("id") == STATE.get("lyrics_id")
        lyr = list(LYRICS["lines"]) if same else []
        sync = LYRICS.get("word_sync", "none")
        credit = LYRICS.get("credit")
        source = LYRICS.get("source", "")

    S = SCALE
    width = WIDGET_W * S
    pad = WIDGET_PAD * S
    inner = width - 2 * pad
    vpad = int(16 * K * S)
    pos = int(st.get("position_ms") or 0)
    # между опросами SMTC (50 мс) позиция экстраполируется на каждый кадр —
    # иначе край подсветки дёргается шагами 50 мс и это видно как не-60 фпс
    if st.get("status") == "playing" and st.get("anchor_t"):
        pos = int(st["anchor_pos"] +
                  (time.monotonic() - st["anchor_t"]) * 1000) + LAG_OFFSET
        if pos < 0:
            pos = 0

    f_eye = _font(8 * K, True)
    f_title = _font(15 * K, True)
    f_artist = _font(10 * K)
    f_active = _font(19 * K, True)
    f_side = _font(11.5 * K)
    f_msg = _font(12.5 * K)
    f_credit = _font(8.5 * K)

    connected = bool(st.get("connected"))
    lstatus = st.get("lyrics_status")
    li = st.get("line_index", -1)
    wi = st.get("word_index", -1)
    title = (st.get("title") or "").strip()
    artist = (st.get("artist") or "").strip()

    if not lyr:
        badge = (("ПОИСК" if lstatus == "loading" else "НЕТ ТЕКСТА")
                 if connected else "ОЖИДАНИЕ")
    elif sync == "exact":
        badge = "ПОСЛОВНО"
    elif sync == "estimated":
        badge = "ПОСТРОЧНО"
    else:
        badge = "БЕЗ СИНХРОНИЗАЦИИ"

    msg = None
    if not connected:
        msg = "Открой Spotify и включи песню"
    elif not lyr:
        msg = ("Ищу текст…" if lstatus == "loading"
               else "Текст для этого трека не найден")

    # (kind, data, font, fill|wi, gap, align, meta)
    items = []
    items.append(("t", "LYRICS  /  " + badge, f_eye, COL_MUTED, 0, "l", None))
    if title:
        items.append(("t", title, f_title, COL_TEXT, 5, "l", None))
        if artist:
            items.append(("t", artist, f_artist, COL_MUTED, 2, "l", None))
    items.append(("d", None, None, None, 12, "l", None))

    if msg:
        for k, row in enumerate(_wrap_text(msg, f_msg, inner)):
            items.append(("t", row, f_msg, COL_MUTED, 12 if k == 0 else 2,
                          "c", None))
    elif lyr:
        if 0 < li < len(lyr):
            for k, row in enumerate(_wrap_text(lyr[li - 1]["text"], f_side,
                                               inner)):
                items.append(("t", row, f_side, COL_SIDE, 12 if k == 0 else 2,
                              "c", None))
        idx = li if 0 <= li < len(lyr) else 0
        active = (li == idx)
        ln = lyr[idx]
        words = ln.get("words")
        meta = {"pos": pos, "line": ln, "active": active}
        if words:
            rows = _wrap_words([w.get("text", "") for w in words],
                               f_active, inner)
            items.append(("w", rows, f_active, wi if active else -1, 9, "c",
                          {"pos": pos, "words": words, "active": active}))
        else:
            for k, row in enumerate(_wrap_text(ln["text"], f_active, inner)):
                items.append(("t", row, f_active,
                              COL_TEXT if active else COL_PEND,
                              9 if k == 0 else 2, "c", meta))
        if idx + 1 < len(lyr):
            for k, row in enumerate(_wrap_text(lyr[idx + 1]["text"], f_side,
                                               inner)):
                items.append(("t", row, f_side, COL_SIDE, 8 if k == 0 else 2,
                              "c", None))

    heights, total = [], 2 * vpad
    for kind, data, font, fill, gap, align, meta in items:
        if kind == "d":
            hgt = S
        else:
            asc, desc = font.getmetrics()
            hgt = asc + desc
            if kind == "w":
                hgt *= len(data)
        gap = int(gap * K * S)
        heights.append((gap, hgt))
        total += gap + hgt

    height = max(total, int(76 * K * S))
    base = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d0 = ImageDraw.Draw(base)
    d0.rounded_rectangle([0, 0, width - 1, height - 1], radius=int(18 * K * S),
                         fill=PANEL_FILL, outline=PANEL_STROKE, width=S)

    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    y = vpad
    for (kind, data, font, fill, gap, align, meta), (gap2, hgt) in zip(items,
                                                                      heights):
        y += gap2
        if kind == "d":
            d.line([(pad, y + hgt // 2), (pad + inner, y + hgt // 2)],
                   fill=COL_DIVIDER, width=S)
            y += hgt
            continue
        asc, desc = font.getmetrics()
        if kind == "t":
            # базовая линия: все строки текста сидят на одном уровне
            by = y + asc
            if align == "l":
                d.text((pad, by), data, font=font, fill=fill, anchor="ls")
            else:
                d.text((width // 2, by), data, font=font, fill=fill,
                       anchor="ms")
            y += hgt
            continue
        # активная строка: белый плавно проникает в поёмные слова (wipe)
        wi_val = fill
        words = meta["words"]
        pos_m = meta["pos"]
        active_w = meta.get("active", True)
        lh = asc + desc
        for row in data:
            ws = [font.getlength(w) for _, w in row]
            space = font.getlength(" ")
            tw = sum(ws) + space * (len(row) - 1)
            x = (width - tw) / 2
            for (j, wtxt), ww in zip(row, ws):
                wd = words[j] if j < len(words) else None
                stw = wd.get("start") if wd else None
                enw = wd.get("end") if wd else None
                if stw is None or enw is None or enw <= stw:
                    prog = (1.0 if (active_w and wi_val >= 0
                                    and j <= wi_val) else 0.0)
                else:
                    # старт за 120 мс до слова, замыкание через 120 мс после
                    prog = (_smooth((pos_m - (stw - 120)) /
                                    float(enw - stw + 240))
                            if active_w else 0.0)
                if prog <= 0.01:
                    d.text((x, y + asc), wtxt, font=font, fill=COL_PEND,
                           anchor="ls")
                elif prog >= 0.99:
                    d.text((x, y + asc), wtxt, font=font,
                           fill=(255, 255, 255, 255), anchor="ls")
                else:
                    d.text((x, y + asc), wtxt, font=font, fill=COL_PEND,
                           anchor="ls")
                    _wipe_word(layer, x, y, wtxt, font, asc, prog, ww)
                x += ww + space
            y += lh

    img = Image.alpha_composite(base, layer)

    if SCALE > 1:
        # суперсэмпл: рисуем в 2x и уменьшаем — сглаженные края
        img = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
    return img


_LAST_FRAME = {"key": None}
_STABLE_H = {"h": 0}
_WRITE_T = {"t": 0.0}
_MIN_WRITE_GAP = 0.004          # лишь гасит шквал записей; темп задаёт ack Lua
# active — что по подтверждению Lua показывает Rainmeter; pending — что ждём
# подтверждения. Писать можно только в файл, не являющийся active/pending.
_PONG = {"active": None, "pending": None}


def _read_ack():
    try:
        return FRAME_ACK.read_bytes()[:1].decode("ascii", "ignore")
    except OSError:
        return ""


def _write_mark(value):
    """Маркер переключения — маленький файл через rename, с ретраями."""
    tmp = FRAME_MARK.with_name(FRAME_MARK.name + ".tmp")
    for _ in range(100):
        try:
            tmp.write_bytes(value.encode("ascii"))
            os.replace(tmp, FRAME_MARK)
            return True
        except OSError:
            time.sleep(0.001)
    return False


def _wait_ack(value, timeout=0.05):
    """Ждём, пока Lua подтвердит показ файла value (обычно ≤16 мс)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if _read_ack() == value:
            return True
        time.sleep(0.001)
    return False


def _init_pong(timeout=1.0):
    # берём active из ack: Lua пишет его каждое обновление скина,
    # поэтому по стабильному значению видно, что реально показано
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        ack = _read_ack()
        if ack in FRAME_PATHS:
            time.sleep(0.005)
            if _read_ack() == ack:
                _PONG["active"] = ack
                return
        time.sleep(0.002)
    _PONG["active"] = "a"          # Lua ещё не отвечает — как в ImageName скина


def save_widget(img):
    # высота окна не должна прыгать при смене строк — ресайз виден как
    # мигание. Растягиваем холст до максимума за сессию (только рост).
    max_h = max(_STABLE_H["h"], img.height)
    if img.height < max_h:
        canvas = Image.new("RGBA", (img.width, max_h), (0, 0, 0, 0))
        canvas.paste(img, (0, 0))
        img = canvas
    _STABLE_H["h"] = max_h
    # кадр не изменился — файл не трогаем (лишние перезагрузки = мерцание)
    key = (img.size, hashlib.md5(img.tobytes()).digest())
    if key == _LAST_FRAME["key"]:
        return
    if time.monotonic() - _WRITE_T["t"] < _MIN_WRITE_GAP:
        return
    WIDGET_DIR.mkdir(parents=True, exist_ok=True)

    if _PONG["pending"]:
        ok = _wait_ack(_PONG["pending"])
        _PONG["active"] = _PONG["pending"]
        _PONG["pending"] = None
        with LOCK:
            STATE["error"] = None if ok else "widget: Lua ack timeout"
    else:
        # ресинк: если Lua переключился сам (перезагрузка скина) —
        # active берём из его подтверждения, писать можно только «другой»
        ack = _read_ack()
        if ack in FRAME_PATHS:
            _PONG["active"] = ack
    if _PONG["active"] is None:
        _PONG["active"] = "a"

    target = "b" if _PONG["active"] == "a" else "a"
    path = FRAME_PATHS[target]
    tmp = path.with_name(path.stem + ".tmp.png")
    img.save(tmp, "PNG", compress_level=1)
    # замена в неактивный файл: Rainmeter этот путь не держит — ретраи
    # нужны только для редкого случая перезагрузки скина в этот момент
    for _ in range(100):
        try:
            os.replace(tmp, path)
            break
        except OSError:
            time.sleep(0.001)
    else:
        return
    # маркер — строго после полной записи кадра; следующий кадр можно
    # писать только после ack, т.е. после реального переключения Rainmeter
    if _write_mark(target):
        _PONG["pending"] = target
    _LAST_FRAME["key"] = key
    _WRITE_T["t"] = time.monotonic()


# ------------------------------------- окно виджета: только на рабочем столе

_WSTATE = {"hwnd": None, "moved": False, "last_z": 0.0}
_DESKTOP_CLASSES = ("Progman", "WorkerW")


def _enum_windows():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    out = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                  wintypes.LPARAM)
    buf = ctypes.create_unicode_buffer(512)
    cls = ctypes.create_unicode_buffer(64)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            user32.GetWindowTextW(hwnd, buf, 512)
            user32.GetClassNameW(hwnd, cls, 64)
            out.append((hwnd, buf.value, cls.value))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out


def _should_reposition():
    """True, если в Rainmeter.ini позиция виджета не задана (стоит по умолчанию)."""
    try:
        ini = (Path.home() / "AppData" / "Roaming" / "Rainmeter" /
               "Rainmeter.ini")
        txt = ini.read_text(encoding="utf-8-sig", errors="ignore")
        m = re.search(r"\[LyricsGlass\](.*?)(?:\[|\Z)", txt, re.S | re.I)
        block = m.group(1) if m else ""
        mx = re.search(r"WindowX=(-?\d+)", block)
        my = re.search(r"WindowY=(-?\d+)", block)
        if not mx or not my:
            return True
        return int(mx.group(1)) <= 0 or int(my.group(1)) <= 0
    except Exception:
        return True


def _is_fullscreen_app():
    """True, если активно пользовательское окно на весь экран (игра, видео, браузер на весь экран)
    на ЛЮБОМ из мониторов. В это время не рисуем и не пишем кадры, чтобы не жрать FPS.
    Исключаем рабочий стол, панели задач и оверлеи (NVIDIA и т.п.)."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        # Активное окно (передового плана) — только оно может быть игрой на переднем плане
        fg = user32.GetForegroundWindow()
        if not fg:
            return False

        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(fg, title, 512)
        if title.value.endswith("LyricsGlass\\LyricsGlass.ini"):
            return False

        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(fg, cls, 64)
        # пропускаем рабочий стол, панели и оверлеи (не являются полноэкранными приложениями)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "CEF-OSC-WIDGET"):
            return False

        # Монитор, на котором находится активное окно (работает с 2+ мониторами)
        MONITOR_DEFAULTTONULL = 0
        hmon = user32.MonitorFromWindow(fg, MONITOR_DEFAULTTONULL)
        if not hmon:
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False

        mon_w = mi.rcMonitor.right - mi.rcMonitor.left
        mon_h = mi.rcMonitor.bottom - mi.rcMonitor.top

        r = wintypes.RECT()
        if not user32.GetWindowRect(fg, ctypes.byref(r)):
            return False
        w = r.right - r.left
        h = r.bottom - r.top

        # Активное окно почти на весь СВОЙ монитор — считаем полноэкранным приложением
        if w >= mon_w - 10 and h >= mon_h - 10 and w > 0 and h > 0:
            return True
        return False
    except Exception:
        return False


def ensure_widget_window():
    """Окно виджета — на рабочем столе: выше обоев, ниже обычных окон."""
    try:
        if not PIL_OK:
            return
        now = time.monotonic()
        if now - _WSTATE["last_z"] < 1.0:
            return
        _WSTATE["last_z"] = now

        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetWindow.restype = wintypes.HWND

        me = None
        apps = []
        for hwnd, title, cls in _enum_windows():
            if title.endswith("LyricsGlass\\LyricsGlass.ini"):
                me = hwnd
            elif cls in _DESKTOP_CLASSES or cls == "Shell_TrayWnd":
                continue
            else:
                apps.append(hwnd)

        if me is None:
            _WSTATE["hwnd"] = None
            return

        if me != _WSTATE["hwnd"]:
            # новое окно (старт/refresh) — ставим по центру по горизонтали
            # и на 4/7 высоты по вертикали (4 сверху, 3 снизу),
            # если пользователь сам не перетаскивал
            _WSTATE["hwnd"] = me
            if _should_reposition():
                sw = user32.GetSystemMetrics(0)      # ширина экрана
                sh = user32.GetSystemMetrics(1)      # высота экрана
                r = wintypes.RECT()
                user32.GetWindowRect(me, ctypes.byref(r))
                w = r.right - r.left
                h = r.bottom - r.top
                x = (sw - w) // 2                    # центр по горизонтали
                y = sh - h - 150                      # 150 px отступа снизу от панели задач — не перекрывает её
                # SWP_NOSIZE | SWP_NOACTIVATE — только переместить
                user32.SetWindowPos(me, 0, x, y, 0, 0, 0x0001 | 0x0010)

        # z-order только при реальном нарушении: безусловный SetWindowPos
        # каждую секунду вызывает перерисовку и видимое мерцание
        target = apps[-1] if apps else 0
        prev = user32.GetWindow(me, 2)          # GW_HWNDPREV
        if prev != target:
            # SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE — только порядок окон
            user32.SetWindowPos(me, target, 0, 0, 0, 0,
                                0x0001 | 0x0002 | 0x0010)
    except Exception:
        pass


def widget_loop():
    last = None
    while True:
        try:
            with LOCK:
                sig = (STATE["connected"], STATE["lyrics_id"],
                       STATE["lyrics_status"], STATE["line_index"],
                       STATE["word_index"], STATE["title"], STATE["artist"],
                       LYRICS.get("id"), len(LYRICS["lines"]),
                       LYRICS.get("word_sync"), LYRICS.get("source"))
                # во время пения перерисовываем каждый кадр (плавность)
                anim = (STATE["connected"] and STATE["status"] == "playing"
                        and STATE["line_index"] >= 0
                        and LYRICS.get("id") == STATE.get("lyrics_id")
                        and bool(LYRICS["lines"]))
            if sig != last or anim:
                last = sig
                # не рисуем и не пишем кадры во время полноэкранной игры —
                # экономим FPS
                if _is_fullscreen_app():
                    time.sleep(0.2)
                    continue
                img = build_widget_image()
                if img is not None:
                    save_widget(img)
            ensure_widget_window()
        except Exception as e:
            with LOCK:
                STATE["error"] = f"widget: {e}"
        time.sleep(0.004)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send((STATIC / "index.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif path == "/state":
            with LOCK:
                self._send(json.dumps(STATE).encode(), "application/json")
        elif path == "/lyrics":
            with LOCK:
                self._send(json.dumps(LYRICS, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
        else:
            self.send_error(404)


# ---------------------------------------------------------------- демо

def demo_loop():
    import urllib.request
    title, artist = "Blinding Lights", "The Weeknd"
    duration = 200000
    key = f"{title}|{artist}|{duration}"
    with LOCK:
        STATE.update(connected=True, status="playing", title=title,
                     artist=artist, duration_ms=duration, lyrics_id=key,
                     lyrics_status="loading")
    fetch_lyrics_job(key, title, artist, duration)
    t0 = time.monotonic()
    while True:
        with LOCK:
            STATE["position_ms"] = int((time.monotonic() - t0) * 1000) % duration
            compute_indices(STATE["position_ms"])
        time.sleep(0.2)


# ---------------------------------------------------------------- main

def open_window():
    edge = (Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Microsoft/Edge/Application/msedge.exe")
    url = f"http://127.0.0.1:{PORT}"
    if edge.exists():
        subprocess.Popen([str(edge), f"--app={url}", "--window-size=460,780"])
    else:
        webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--window", action="store_true",
                    help="дополнительно открыть HTML-окно в браузере")
    ap.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if os.name == "nt":
        # разрешение таймера 1 мс — иначе sleep(0.004) спит до 15.6 мс
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    if PIL_OK:
        WIDGET_DIR.mkdir(parents=True, exist_ok=True)
        # оба кадровых файла должны существовать до первой перезагрузки скина
        missing = [f for f in FRAME_PATHS.values() if not f.exists()]
        if missing:
            seed = None
            if WIDGET_PNG.exists():
                try:
                    seed = WIDGET_PNG.read_bytes()
                except OSError:
                    seed = None
            if seed is None:
                try:
                    img0 = build_widget_image()
                    if img0 is not None:
                        img0.save(missing[0], "PNG", compress_level=1)
                        seed = missing[0].read_bytes()
                except Exception:
                    seed = None
            if seed:
                for f in missing:
                    try:
                        f.write_bytes(seed)
                    except OSError:
                        pass
        _init_pong()
        threading.Thread(target=widget_loop, daemon=True).start()

    if args.demo:
        threading.Thread(target=demo_loop, daemon=True).start()
    else:
        threading.Thread(target=lambda: asyncio.run(poll_loop()),
                         daemon=True).start()

    if args.window and not args.no_open:
        time.sleep(1.0)
        open_window()

    print(f"bridge: http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
