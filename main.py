#!/usr/bin/env python3
"""Автомонтажёр роликов про Free Fire.

Команды:
  python main.py create   - придумать историю, собрать ролик, отправить в Telegram
  python main.py poll     - ~50 секунд слушать Telegram (кнопки и команды)
  python main.py stats    - обновить статистику из Instagram и прислать отчёт

OFFLINE=1 python main.py create  - тест монтажа без интернета (заглушки).
"""
import asyncio
import datetime
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import textwrap
import time

import requests

ENV = os.environ.get
OFFLINE = ENV("OFFLINE") == "1"

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data" / "videos"
WORK = ROOT / "work"
CHARS = (WORK / "chars_offline") if OFFLINE else (ROOT / "characters")

GEMINI_KEY = ENV("GEMINI_API_KEY", "")
GEMINI_MODELS = [m for m in [ENV("GEMINI_MODEL"), "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"] if m]
TG_TOKEN = ENV("TG_TOKEN", "")
TG_CHAT = ENV("TG_CHAT_ID", "")
IG_USER = ENV("IG_USER_ID", "")
IG_TOKEN = ENV("IG_ACCESS_TOKEN", "")
POLL_KEY = ENV("POLLINATIONS_KEY", "")
AUTO_PUBLISH = (ENV("AUTO_PUBLISH") or "").lower() in ("1", "true", "yes")
VOICE = ENV("TTS_VOICE") or "ru-RU-DmitryNeural"
FB = "https://graph.facebook.com/v21.0"

W, H = 1080, 1920
BOX = 900
BOX_X = (W - BOX) // 2
BOX_Y = 300
SUB_Y = BOX_Y + BOX + 70
SUB_SIZE = 62
FPS = 30
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
STYLE = "stylized 3D cartoon game art, vibrant colors, clean shapes, battle royale setting"

# SFX генерируются прямо в ffmpeg: (источник, фильтры, задержка в мс)
SFX = {
    "whoosh": ("anoisesrc=d=0.6:c=pink:r=44100",
               "highpass=f=500,lowpass=f=5000,afade=t=in:d=0.2,afade=t=out:st=0.2:d=0.35,volume=0.5", 250),
    "hit": ("sine=f=70:d=0.4:r=44100", "afade=t=out:st=0.05:d=0.3,volume=1.0", 900),
    "boom": ("anoisesrc=d=1.2:c=brown:r=44100", "lowpass=f=300,afade=t=out:st=0.1:d=1.0,volume=2.5", 900),
    "shot": ("anoisesrc=d=0.25:c=white:r=44100", "highpass=f=800,afade=t=out:st=0.02:d=0.2,volume=0.7", 600),
}


# ───────────────────────── утилиты ─────────────────────────

def log(*a):
    print(*a, flush=True)


def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode:
        raise RuntimeError("ffmpeg error: " + r.stderr[-1500:])


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


# ───────────────────────── хранилище ─────────────────────────

def load_videos():
    DATA.mkdir(parents=True, exist_ok=True)
    vs = [json.loads(p.read_text(encoding="utf-8")) for p in DATA.glob("*.json")]
    return sorted(vs, key=lambda v: v["id"])


def save_video(v):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / f"{v['id']}.json").write_text(json.dumps(v, ensure_ascii=False, indent=1), encoding="utf-8")


def get_video(vid):
    p = DATA / f"{vid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ───────────────────────── Telegram ─────────────────────────

def tg(method, files=None, **params):
    for k, v in list(params.items()):
        if isinstance(v, (dict, list)):
            params[k] = json.dumps(v, ensure_ascii=False)
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                      data=params, files=files, timeout=180)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Telegram {method}: {j}")
    return j["result"]


def notify(text):
    log("NOTIFY:", text)
    if TG_TOKEN and TG_CHAT:
        try:
            tg("sendMessage", chat_id=TG_CHAT, text=text[:4000])
        except Exception as e:  # noqa
            log("notify failed:", e)


# ───────────────────────── Gemini ─────────────────────────

def gemini(prompt, as_json=True):
    cfg = {"temperature": 1.0}
    if as_json:
        cfg["responseMimeType"] = "application/json"
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}
    last = None
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=180)
            if r.status_code in (429, 500, 503):
                last = f"{model}: {r.status_code}"
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 404:
                last = f"{model}: модель не найдена"
                break
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            if not as_json:
                return text
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            return json.loads(text)
    raise RuntimeError(f"Gemini недоступен: {last}")


# ───────────────────────── картинки ─────────────────────────

def pollinations(prompt, path, seed):
    from urllib.parse import quote
    url = "https://gen.pollinations.ai/image/" + quote(f"{prompt}, {STYLE}"[:600])
    params = {"width": 1024, "height": 1024, "seed": seed, "nologo": "true", "model": "flux"}
    headers = {"User-Agent": "video-bot/1.0"}
    if POLL_KEY:
        headers["Authorization"] = f"Bearer {POLL_KEY}"
    err = None
    for attempt in range(5):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=180)
            if r.ok and r.headers.get("content-type", "").startswith("image"):
                path.write_bytes(r.content)
                time.sleep(2)
                return
            err = f"{r.status_code} {r.text[:120]}"
        except requests.RequestException as e:
            err = str(e)
        time.sleep(8 * (attempt + 1))
    raise RuntimeError(f"Pollinations не отдал картинку: {err}")


def fake_image(kind, key, path):
    from PIL import Image, ImageDraw
    rnd = random.Random(key)
    col = tuple(rnd.randint(60, 220) for _ in range(3))
    if kind == "bg":
        im = Image.new("RGB", (1024, 1024))
        d = ImageDraw.Draw(im)
        for y in range(1024):
            d.line([(0, y), (1024, y)], fill=(col[0] - y // 8 % 60, col[1] - y // 14, 90 + y // 7))
        d.rectangle([0, 780, 1024, 1024], fill=(40, 90, 50))
    elif kind == "char":
        im = Image.new("RGBA", (500, 900), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse([160, 20, 340, 200], fill=(240, 200, 160, 255))
        d.rectangle([150, 200, 350, 560], fill=col + (255,))
        d.rectangle([160, 560, 230, 880], fill=(60, 60, 90, 255))
        d.rectangle([270, 560, 340, 880], fill=(60, 60, 90, 255))
        d.rectangle([150, 60, 350, 100], fill=(200, 30, 30, 255))
    else:
        im = Image.new("RGBA", (260, 260), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse([40, 60, 220, 240], fill=col + (255,))
        d.rectangle([110, 10, 150, 70], fill=(150, 150, 150, 255))
    im.save(path)


def gen_image(kind, prompt, path, seed):
    if OFFLINE:
        fake_image(kind, prompt, path)
    else:
        pollinations(prompt, path, seed)


_session = None


def cutout(src, dst):
    """Убирает фон -> PNG с прозрачностью."""
    if OFFLINE:
        shutil.copy(src, dst)
        return
    global _session
    from rembg import new_session, remove
    if _session is None:
        _session = new_session("u2net")
    dst.write_bytes(remove(src.read_bytes(), session=_session))


# ───────────────────────── герои ─────────────────────────

def slug(name):
    return re.sub(r"[^\w-]", "", name.lower().strip().replace(" ", "-")) or "hero"


def load_cast():
    out = {}
    if CHARS.exists():
        for m in CHARS.glob("*/meta.json"):
            c = json.loads(m.read_text(encoding="utf-8"))
            c["dir"] = m.parent
            out[c["name"].lower()] = c
    return out


def ensure_character(name, desc, cast):
    key = name.lower()
    if key in cast:
        return cast[key]
    d = CHARS / slug(name)
    d.mkdir(parents=True, exist_ok=True)
    seed = random.randint(1, 10_000_000)
    raw = d / "raw.png"
    gen_image("char", f"{desc}, full body, standing, front view, plain solid white background", raw, seed)
    cutout(raw, d / "portrait.png")
    raw.unlink(missing_ok=True)
    meta = {"name": name, "description": desc, "seed": seed, "created": now_iso()}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    meta["dir"] = d
    cast[key] = meta
    log(f"новый герой: {name}")
    return meta


def char_layer(c, pose, tag):
    if not pose or pose.lower() in ("default", "stand", "standing", "idle"):
        return c["dir"] / "portrait.png"
    raw = WORK / f"{tag}_raw.png"
    out = WORK / f"{tag}.png"
    gen_image("char", f"{c['description']}, {pose}, full body, plain solid white background", raw, c["seed"])
    cutout(raw, out)
    return out


# ───────────────────────── сценарий ─────────────────────────

SAMPLE_STORY = {
    "title": "Последний прыжок",
    "summary": "Новичок Рэд падает на остров, находит гранату и спасает команду.",
    "caption": "Он думал, что это конец... 😱 #freefire #история #battleroyale",
    "cast": [
        {"name": "Рэд", "description": "young soldier in red bandana, green tactical vest, backpack"},
        {"name": "Лис", "description": "girl sniper with orange hair, black hoodie, long rifle"},
    ],
    "scenes": [
        {"narration": "Рэд выпрыгнул из вертолёта", "background": "helicopter high above island, sky",
         "characters": [{"name": "Рэд", "pose": "default"}], "prop": None, "prop_motion": "none", "sfx": "whoosh"},
        {"narration": "Внизу он увидел гранату", "background": "jungle clearing with ruins",
         "characters": [{"name": "Рэд", "pose": "default"}], "prop": "hand grenade",
         "prop_motion": "fly", "sfx": "whoosh"},
        {"narration": "Лис кричала: бросай её скорее", "background": "old ruined bridge, sunset",
         "characters": [{"name": "Рэд", "pose": "default"}, {"name": "Лис", "pose": "default"}],
         "prop": "hand grenade", "prop_motion": "fall", "sfx": "shot"},
        {"narration": "Взрыв разнёс вражеский лагерь", "background": "enemy camp tents in the desert",
         "characters": [{"name": "Лис", "pose": "default"}], "prop": "explosion fireball",
         "prop_motion": "static", "sfx": "boom"},
        {"narration": "Команда победила в этом матче", "background": "victory podium on the beach",
         "characters": [{"name": "Рэд", "pose": "default"}, {"name": "Лис", "pose": "default"}],
         "prop": "golden trophy", "prop_motion": "static", "sfx": "hit"},
    ],
}


def story_prompt(videos, cast):
    hist = []
    for v in videos[-20:]:
        s = v.get("stats") or {}
        hist.append(f"- {v['title']}: {v.get('summary', '')} | статус: {v['status']} | "
                    f"просмотры: {s.get('views', '?')}, лайки: {s.get('likes', '?')}, "
                    f"комментарии: {s.get('comments', '?')}")
    heroes = [f"- {c['name']}: {c['description']}" for c in cast.values()]
    return f"""Ты сценарист коротких вертикальных видео (Instagram Reels) про мир Free Fire (королевская битва).
Придумай НОВУЮ цельную историю с завязкой, напряжением и сильной концовкой или твистом.
Не копируй официальных персонажей игры: герои вымышленные.

Уже снятые ролики и их результаты (не повторяй сюжеты, учитывай, что заходит лучше):
{chr(10).join(hist) or '- пока нет'}

Уже существующие герои (можно использовать снова, тогда НЕ добавляй их в cast):
{chr(10).join(heroes) or '- пока нет'}

Правила:
- 12-16 сцен, каждая сцена = один кадр на 2-3 секунды.
- narration: по-русски, 5-8 слов, живая речь диктора. Вместе фразы складываются в историю.
- background, prop, pose: ТОЛЬКО по-английски, коротко и визуально, строго по смыслу narration этой сцены.
- В кадре максимум 2 героя. pose = "default" (предпочтительно) или короткое английское описание позы.
- prop: предмет, с которым взаимодействуют герои, или null. prop_motion: "fly" (пролетает), "fall" (падает сверху), "static" (появляется), "none".
- sfx: "whoosh", "hit", "boom", "shot" или "none".
- cast: только НОВЫЕ герои (до 3), description по-английски: внешность, одежда, цвета.
- caption: по-русски, цепляющая подпись и 3-5 хештегов.

Верни ТОЛЬКО JSON:
{{"title": "...", "summary": "1 предложение о сюжете", "caption": "...",
 "cast": [{{"name": "...", "description": "..."}}],
 "scenes": [{{"narration": "...", "background": "...",
   "characters": [{{"name": "...", "pose": "default"}}],
   "prop": null, "prop_motion": "none", "sfx": "none"}}]}}"""


def validate_story(s):
    scenes = s.get("scenes") or []
    if len(scenes) < 4:
        raise RuntimeError("Gemini вернул слишком короткую историю")
    for sc in scenes:
        sc["narration"] = str(sc.get("narration", "")).strip()
        sc["background"] = str(sc.get("background", "jungle island")).strip()
        sc["characters"] = (sc.get("characters") or [])[:2]
        if sc.get("prop_motion") not in ("fly", "fall", "static"):
            sc["prop_motion"] = "none"
        if sc.get("sfx") not in SFX:
            sc["sfx"] = "none"
        if not sc.get("prop"):
            sc["prop"] = None
    return s


# ───────────────────────── озвучка и сборка ─────────────────────────

async def _tts(text, path):
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate="+6%").save(str(path))


def make_voice(text, path):
    if OFFLINE:
        run(["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"sine=f=220:d={0.38 * len(text.split()):.2f}", str(path)])
    else:
        asyncio.run(_tts(text, path))


def subtitle_filters(text, tag):
    parts = []
    for i, line in enumerate(textwrap.wrap(text, width=24)):
        f = WORK / f"{tag}_sub{i}.txt"
        f.write_text(line, encoding="utf-8")
        y = SUB_Y + i * (SUB_SIZE + 18)
        parts.append(f"drawtext=fontfile={FONT}:textfile={f}:fontsize={SUB_SIZE}:"
                     f"fontcolor=black:x=(w-text_w)/2:y={y}")
    return ",".join(parts)


def render_scene(tag, sc, bg, chars, prop, voice):
    vlen = duration(voice)
    tempo = min(vlen / 2.75, 1.35) if vlen > 2.75 else 1.0
    d = round(min(max(vlen / tempo + 0.3, 2.0), 3.0), 2)

    cmd, n = [], 0

    def add_img(p):
        nonlocal n
        cmd.extend(["-loop", "1", "-t", str(d), "-i", str(p)])
        n += 1
        return n - 1

    ib = add_img(bg)
    ics = [add_img(p) for p in chars]
    ip = add_img(prop) if prop else None
    cmd.extend(["-i", str(voice)])
    iv = n
    n += 1
    cmd.extend(["-f", "lavfi", "-t", str(d), "-i", f"color=white:s={W}x{H}:r={FPS}"])
    icv = n
    n += 1
    sfx = SFX.get(sc["sfx"])
    isf = None
    if sfx:
        cmd.extend(["-f", "lavfi", "-t", "1.5", "-i", sfx[0]])
        isf = n
        n += 1

    ease = "(1-pow(1-min(t/0.7,1),3))"
    p = "min(max(t-0.3,0)/0.9,1)"
    fc = [f"[{ib}:v]scale=w='trunc({BOX}*(1+0.08*t/{d})/2)*2':h='trunc({BOX}*(1+0.08*t/{d})/2)*2':"
          f"eval=frame,crop={BOX}:{BOX}:(in_w-{BOX})/2:(in_h-{BOX})/2,format=rgba[l0]"]
    last = "l0"
    ch = 600 if len(ics) > 1 else 640
    for k, ic in enumerate(ics):
        flip = ",hflip" if k == 1 else ""
        fc.append(f"[{ic}:v]scale=-2:{ch}{flip},format=rgba[c{k}]")
        if k == 0:
            x = f"-w+(w+70)*{ease}"
        else:
            x = f"{BOX}-(w+70)*{ease}"
        fc.append(f"[{last}][c{k}]overlay=x='{x}':y='H-h+30+7*sin(7*t+{k})':eval=frame[l{k + 1}]")
        last = f"l{k + 1}"
    if ip is not None:
        mot = sc["prop_motion"]
        if mot == "fly":
            fc.append(f"[{ip}:v]scale=-2:190,format=rgba,rotate=a='t*7':ow='hypot(iw,ih)':oh=ow:c=none[pr]")
            pos = f"x='{BOX}-60-420*{p}':y='430-170*sin(PI*{p})'"
        elif mot == "fall":
            fc.append(f"[{ip}:v]scale=-2:190,format=rgba,rotate=a='t*4':ow='hypot(iw,ih)':oh=ow:c=none[pr]")
            pos = f"x='(W-w)/2+150':y='-h+(H*0.8+h)*pow({p},2)'"
        else:
            fc.append(f"[{ip}:v]scale=-2:230,format=rgba[pr]")
            pos = "x='W*0.5-w/2':y='H*0.5'"
        fc.append(f"[{last}][pr]overlay={pos}:eval=frame:enable='gte(t,0.3)'[lp]")
        last = "lp"
    fc.append(f"[{icv}:v][{last}]overlay={BOX_X}:{BOX_Y}[canvas]")
    fc.append(f"[canvas]{subtitle_filters(sc['narration'], tag)}[v]")

    at = f"atempo={tempo:.3f}," if tempo > 1.0 else ""
    fc.append(f"[{iv}:a]{at}apad,atrim=0:{d}[voice]")
    if isf is not None:
        fc.append(f"[{isf}:a]{sfx[1]},adelay={sfx[2]}|{sfx[2]}[sfx]")
        fc.append("[voice][sfx]amix=inputs=2:duration=first:dropout_transition=0,volume=2[a]")
    else:
        fc.append("[voice]anull[a]")

    out = WORK / f"{tag}.mp4"
    cmd = (["ffmpeg", "-y"] + cmd + ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]",
           "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-t", str(d), str(out)])
    run(cmd)
    return out


def concat(parts, out):
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", "-movflags", "+faststart", str(out)])


# ───────────────────────── Instagram ─────────────────────────

def ig_publish(video_bytes, caption):
    if not (IG_USER and IG_TOKEN):
        raise RuntimeError("Instagram не настроен (нет IG_USER_ID / IG_ACCESS_TOKEN)")
    r = requests.post(f"{FB}/{IG_USER}/media", data={
        "media_type": "REELS", "upload_type": "resumable",
        "caption": caption[:2200], "access_token": IG_TOKEN}, timeout=60)
    j = r.json()
    if "id" not in j:
        raise RuntimeError(f"Instagram создание контейнера: {j}")
    cid = j["id"]
    up = requests.post(f"https://rupload.facebook.com/ig-api-upload/v21.0/{cid}", data=video_bytes, headers={
        "Authorization": f"OAuth {IG_TOKEN}", "offset": "0", "file_size": str(len(video_bytes))}, timeout=600)
    if not up.ok:
        raise RuntimeError(f"Instagram загрузка файла: {up.text[:300]}")
    for _ in range(60):
        st = requests.get(f"{FB}/{cid}", params={"fields": "status_code,status", "access_token": IG_TOKEN},
                          timeout=60).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram обработка: {st}")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram не успел обработать видео за 10 минут")
    pub = requests.post(f"{FB}/{IG_USER}/media_publish",
                        data={"creation_id": cid, "access_token": IG_TOKEN}, timeout=120).json()
    if "id" not in pub:
        raise RuntimeError(f"Instagram публикация: {pub}")
    return pub["id"]


def ig_stats(media_id):
    out = {}
    r = requests.get(f"{FB}/{media_id}", params={"fields": "like_count,comments_count",
                                                  "access_token": IG_TOKEN}, timeout=60)
    if r.ok:
        out["likes"] = r.json().get("like_count")
        out["comments"] = r.json().get("comments_count")
    for metrics in ("views,reach,shares,saved", "plays,reach,shares,saved"):
        r = requests.get(f"{FB}/{media_id}/insights", params={"metric": metrics,
                                                               "access_token": IG_TOKEN}, timeout=60)
        if r.ok:
            for m in r.json().get("data", []):
                vals = m.get("values") or [{}]
                key = "views" if m["name"] == "plays" else m["name"]
                out[key] = vals[0].get("value")
            break
    return out


def refresh_stats(limit=10):
    if not (IG_USER and IG_TOKEN):
        return
    pubs = [v for v in load_videos() if v["status"] == "published" and v.get("ig_media_id")]
    for v in pubs[-limit:]:
        try:
            s = ig_stats(v["ig_media_id"])
            if s:
                v["stats"] = {**s, "updated": now_iso()}
                save_video(v)
        except Exception as e:  # noqa
            log("stats error", v["id"], e)


def stats_report():
    refresh_stats()
    pubs = [v for v in load_videos() if v["status"] == "published"]
    if not pubs:
        return "Пока нет опубликованных роликов."
    lines = []
    for v in pubs[-10:]:
        s = v.get("stats") or {}
        lines.append(f"• {v['title']} ({v['created'][:10]}): 👁 {s.get('views', '—')}  "
                     f"❤ {s.get('likes', '—')}  💬 {s.get('comments', '—')}  "
                     f"↗ {s.get('shares', '—')}  🔖 {s.get('saved', '—')}")
    text = "Статистика последних роликов:\n" + "\n".join(lines)
    try:
        data = json.dumps([{"title": v["title"], "summary": v.get("summary"), "stats": v.get("stats")}
                           for v in pubs[-15:]], ensure_ascii=False)
        text += "\n\nВывод:\n" + gemini(
            "Вот данные по моим Instagram Reels про Free Fire. Кратко (3 пункта, по-русски): "
            f"что заходит лучше, что хуже, и какую идею снять следующей.\n{data}", as_json=False)
    except Exception as e:  # noqa
        log("gemini analysis failed", e)
    return text


def publish_video(v, data):
    media_id = ig_publish(data, v["caption"])
    v["status"] = "published"
    v["ig_media_id"] = media_id
    v["published"] = now_iso()
    save_video(v)
    return media_id


# ───────────────────────── команда create ─────────────────────────

def cmd_create():
    WORK.mkdir(exist_ok=True)
    vid = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    try:
        refresh_stats()
        videos = load_videos()
        cast = load_cast()
        story = SAMPLE_STORY if OFFLINE else gemini(story_prompt(videos, cast))
        story = validate_story(story)
        for c in story.get("cast") or []:
            ensure_character(c["name"], c["description"], cast)

        parts = []
        for i, sc in enumerate(story["scenes"]):
            tag = f"s{i:02d}"
            log(f"сцена {i + 1}/{len(story['scenes'])}: {sc['narration']}")
            seed = random.Random(f"{vid}{i}").randint(1, 10_000_000)
            bg = WORK / f"{tag}_bg.png"
            gen_image("bg", sc["background"] + ", wide scene, no characters", bg, seed)
            layers = []
            for k, ch in enumerate(sc["characters"]):
                c = cast.get(str(ch.get("name", "")).lower())
                if c:
                    layers.append(char_layer(c, ch.get("pose"), f"{tag}_c{k}"))
            prop = None
            if sc["prop"]:
                raw = WORK / f"{tag}_prop_raw.png"
                prop = WORK / f"{tag}_prop.png"
                gen_image("prop", f"{sc['prop']}, isolated object, plain solid white background", raw, seed + 1)
                cutout(raw, prop)
            voice = WORK / f"{tag}.mp3"
            make_voice(sc["narration"], voice)
            parts.append(render_scene(tag, sc, bg, layers, prop, voice))

        final = WORK / "final.mp4"
        concat(parts, final)
    except Exception as e:
        notify(f"❌ Не удалось собрать ролик: {e}")
        raise

    v = {"id": vid, "created": now_iso(), "title": story["title"], "summary": story.get("summary", ""),
         "caption": story.get("caption", story["title"]), "scenes": len(story["scenes"]),
         "script": [s["narration"] for s in story["scenes"]], "status": "pending"}
    log(f"готово: {final} ({duration(final):.0f} с)")

    if TG_TOKEN and TG_CHAT:
        with open(final, "rb") as f:
            msg = tg("sendVideo", files={"video": f}, chat_id=TG_CHAT, width=W, height=H,
                     supports_streaming="true",
                     caption=f"🎬 {v['title']}\n\n{v['caption']}"[:1000],
                     reply_markup={"inline_keyboard": [[
                         {"text": "✅ Опубликовать", "callback_data": f"pub:{vid}"},
                         {"text": "🗑 Отклонить", "callback_data": f"rej:{vid}"}]]})
        v["tg_file_id"] = msg["video"]["file_id"]
    save_video(v)

    if AUTO_PUBLISH and IG_TOKEN:
        try:
            publish_video(v, final.read_bytes())
            notify(f"✅ Опубликовано в Instagram: {v['title']}")
        except Exception as e:
            notify(f"❌ Instagram: {e}")
            raise


# ───────────────────────── команда poll ─────────────────────────

HELP = ("Я делаю ролики про Free Fire сам по расписанию.\n"
        "/make - сделать ролик прямо сейчас\n"
        "/stats - статистика и вывод\n"
        "/status - сколько роликов ждёт решения")


def dispatch_create():
    repo, tok = ENV("GITHUB_REPOSITORY"), ENV("GITHUB_TOKEN")
    r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/create.yml/dispatches",
                      headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                      json={"ref": ENV("GITHUB_REF_NAME") or "main"}, timeout=60)
    if r.status_code != 204:
        raise RuntimeError(f"не смог запустить сборку: {r.status_code} {r.text[:200]}")


def handle_message(m):
    chat = str(m["chat"]["id"])
    text = (m.get("text") or "").strip()
    if TG_CHAT and chat != str(TG_CHAT):
        return
    if not TG_CHAT:
        tg("sendMessage", chat_id=chat, text=f"Твой chat id: {chat}\nДобавь его в секрет TG_CHAT_ID.")
        return
    cmd = text.split()[0].split("@")[0].lower() if text else ""
    if cmd in ("/start", "/help"):
        tg("sendMessage", chat_id=chat, text=HELP)
    elif cmd == "/make":
        dispatch_create()
        tg("sendMessage", chat_id=chat, text="Запустил сборку. Ролик придёт через 10-20 минут.")
    elif cmd == "/stats":
        tg("sendMessage", chat_id=chat, text="Собираю статистику...")
        tg("sendMessage", chat_id=chat, text=stats_report()[:4000])
    elif cmd == "/status":
        vs = load_videos()
        pend = sum(1 for v in vs if v["status"] == "pending")
        pub = sum(1 for v in vs if v["status"] == "published")
        tg("sendMessage", chat_id=chat, text=f"Всего: {len(vs)}, ждут решения: {pend}, опубликовано: {pub}")


def handle_callback(cq):
    chat = str(cq["message"]["chat"]["id"])
    if str(TG_CHAT) != chat:
        return
    action, _, vid = cq["data"].partition(":")
    v = get_video(vid)
    qid = cq["id"]
    mid = cq["message"]["message_id"]
    if not v or v["status"] != "pending":
        tg("answerCallbackQuery", callback_query_id=qid, text="Уже обработано")
        return
    tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
    if action == "rej":
        v["status"] = "rejected"
        save_video(v)
        tg("answerCallbackQuery", callback_query_id=qid, text="Отклонено")
        return
    tg("answerCallbackQuery", callback_query_id=qid, text="Публикую...")
    try:
        info = tg("getFile", file_id=v["tg_file_id"])
        data = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}", timeout=300).content
        publish_video(v, data)
        tg("sendMessage", chat_id=chat, text=f"✅ Опубликовано в Instagram: {v['title']}")
    except Exception as e:
        tg("sendMessage", chat_id=chat, text=f"❌ Не получилось опубликовать: {e}"[:4000])


def cmd_poll():
    end = time.time() + 50
    offset = None
    while time.time() < end:
        params = {"timeout": 10, "allowed_updates": ["message", "callback_query"]}
        if offset:
            params["offset"] = offset
        for u in tg("getUpdates", **params):
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    handle_message(u["message"])
                elif "callback_query" in u:
                    handle_callback(u["callback_query"])
            except Exception as e:  # noqa
                log("handler error:", e)
                notify(f"⚠️ Ошибка: {e}")
    if offset:
        tg("getUpdates", offset=offset, timeout=0)


def cmd_stats():
    text = stats_report()
    notify(text)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    {"create": cmd_create, "poll": cmd_poll, "stats": cmd_stats}.get(
        mode, lambda: sys.exit(__doc__))()
