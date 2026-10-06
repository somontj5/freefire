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
# photo  - каждая сцена одно готовое фото (по умолчанию)
# layers - послойная анимация: фон, герои и предметы отдельно
MODE = (ENV("VIDEO_MODE") or "photo").lower()
PHOTO_ZOOM = (ENV("PHOTO_ZOOM") or "").lower() in ("1", "true", "yes")  # лёгкий наезд на фото
VOICE = ENV("TTS_VOICE") or "ru-RU-DmitryNeural"
# real    - реальные истории про Free Fire (поиск Gemini + проверка фактов), по умолчанию
# fiction - выдуманные истории
STORY_TYPE = (ENV("STORY_TYPE") or "real").lower()
# Эксперимент: герои по референс-картинке (Pollinations kontext) вместо одного текстового описания
CHAR_REF = (ENV("CHAR_REF") or "").lower() in ("1", "true", "yes")
REF_MODEL = ENV("REF_MODEL") or "kontext"
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

def _gemini_request(body):
    last = None
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=240)
            if r.status_code in (429, 500, 503):
                last = f"{model}: {r.status_code}"
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 404:
                last = f"{model}: модель не найдена"
                break
            if not r.ok:
                raise RuntimeError(f"Gemini {model} {r.status_code}: {r.text[:300]}")
            return r.json()
    raise RuntimeError(f"Gemini недоступен: {last}")


def _text_of(resp):
    parts = resp["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def gemini(prompt, as_json=True):
    cfg = {"temperature": 1.0}
    if as_json:
        cfg["responseMimeType"] = "application/json"
    text = _text_of(_gemini_request({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}))
    if not as_json:
        return text
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def gemini_search(prompt):
    """Ответ Gemini с поиском Google. Возвращает (текст, [источники])."""
    resp = _gemini_request({"contents": [{"parts": [{"text": prompt}]}],
                            "tools": [{"google_search": {}}],
                            "generationConfig": {"temperature": 0.4}})
    srcs = []
    meta = resp["candidates"][0].get("groundingMetadata") or {}
    for ch in meta.get("groundingChunks") or []:
        w = ch.get("web") or {}
        if w.get("uri") and not any(x["url"] == w["uri"] for x in srcs):
            srcs.append({"title": w.get("title", ""), "url": w["uri"]})
    return _text_of(resp), srcs[:8]


# ───────────────────────── картинки ─────────────────────────

def pollinations(prompt, path, seed, refs=None):
    from urllib.parse import quote
    url = "https://gen.pollinations.ai/image/" + quote(f"{prompt}, {STYLE}"[:900])
    params = {"width": 1024, "height": 1024, "seed": seed, "nologo": "true", "model": "flux"}
    if refs:
        params["model"] = REF_MODEL
        params["image"] = refs
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
    if kind == "scene":
        im = Image.new("RGB", (1024, 1024))
        d = ImageDraw.Draw(im)
        for y in range(1024):
            d.line([(0, y), (1024, y)], fill=(col[0] - y // 8 % 60, col[1] - y // 14, 90 + y // 7))
        d.rectangle([0, 780, 1024, 1024], fill=(40, 90, 50))
        for x in (260, 700):
            c2 = tuple(rnd.randint(60, 220) for _ in range(3))
            d.ellipse([x, 330, x + 120, 450], fill=(240, 200, 160))
            d.rectangle([x - 10, 450, x + 130, 700], fill=c2)
            d.rectangle([x, 700, x + 50, 840], fill=(60, 60, 90))
            d.rectangle([x + 70, 700, x + 120, 840], fill=(60, 60, 90))
    elif kind == "bg":
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


def gen_image(kind, prompt, path, seed, refs=None):
    if OFFLINE:
        fake_image(kind, prompt, path)
    else:
        pollinations(prompt, path, seed, refs)


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


def ensure_character(name, desc, cast, portrait=True, ref=False):
    """Герой хранится в characters/. Портрет (PNG без фона) нужен только для режима layers."""
    key = name.lower()
    c = cast.get(key)
    if c is None:
        d = CHARS / slug(name)
        d.mkdir(parents=True, exist_ok=True)
        c = {"name": name, "description": desc, "seed": random.randint(1, 10_000_000),
             "created": now_iso()}
        (d / "meta.json").write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
        c["dir"] = d
        cast[key] = c
        log(f"новый герой: {name}")
    if portrait and not (c["dir"] / "portrait.png").exists():
        raw = c["dir"] / "raw.png"
        gen_image("char", f"{c['description']}, full body, standing, front view, plain solid white background",
                  raw, c["seed"])
        cutout(raw, c["dir"] / "portrait.png")
        raw.unlink(missing_ok=True)
    if ref and not (c["dir"] / "ref.png").exists():
        gen_image("char", f"{c['description']}, full body, standing, front view, plain light background",
                  c["dir"] / "ref.png", c["seed"])
    return c


def ref_urls(text, cast, have_ref):
    """Ссылки на референсы героев, которые упомянуты в промпте и уже лежат в репозитории."""
    repo, branch = ENV("GITHUB_REPOSITORY"), ENV("GITHUB_REF_NAME") or "main"
    out = []
    for m in re.finditer(r"\[([^\]]+)\]", text):
        key = m.group(1).strip().lower()
        c = cast.get(key)
        if c and key in have_ref and repo:
            u = f"https://raw.githubusercontent.com/{repo}/{branch}/characters/{c['dir'].name}/ref.png"
            if u not in out:
                out.append(u)
    return out[:2]


def expand_prompt(text, cast):
    """[Рэд] в промпте сцены заменяется на описание внешности героя - так он выглядит одинаково."""
    def sub(m):
        c = cast.get(m.group(1).strip().lower())
        return c["description"] if c else m.group(1)
    return re.sub(r"\[([^\]]+)\]", sub, text)


_char_cache = {}


def char_layer(c, pose, holding, slot, tag):
    """Герой + всё, что у него в руках, генерируются ВМЕСТЕ одной картинкой.
    Возвращает (путь, нужно ли зеркалить)."""
    default = not pose or pose.lower() in ("default", "stand", "standing", "idle")
    if default and not holding:
        return c["dir"] / "portrait.png", slot == 1
    facing = "right" if slot == 0 else "left"
    key = (c["name"], (pose or "").lower(), (holding or "").lower(), facing)
    if key in _char_cache:
        return _char_cache[key], False
    parts = [c["description"]]
    if not default:
        parts.append(pose)
    if holding:
        parts.append(f"holding {holding} in hands")
    parts.append(f"full body, facing {facing}, side view, plain solid white background")
    raw, out = WORK / f"{tag}_raw.png", WORK / f"{tag}.png"
    gen_image("char", ", ".join(parts), raw, c["seed"])
    cutout(raw, out)
    _char_cache[key] = out
    return out, False


# ───────────────────────── сценарий ─────────────────────────

NARRATION_STYLE = """- narration (текст озвучки): по-русски, 5-8 слов, одна мысль на фразу. Это НЕ диктор и НЕ описание картинки.
  Это живой разговор: так друг рассказывает историю другу в переписке голосом или за кружкой чая.
  * Говори как человек: разговорные связки («короче», «и тут», «слушай», «представляешь», «а дальше вообще»),
    эмоции, короткие рваные фразы, иногда вопрос слушателю («и что думаешь он сделал?»).
  * Можно от первого лица («я прыгнул, а там...») или про «одного моего кореша». Выбери одно и держись его всю историю.
  * Не пересказывай, что видно на картинке, и не комментируй как телеведущий. Передавай чувства, мысли, ожидание, удивление.
  * Без мата. Знаки ! ? , - допустимы, многоточия и кавычки не используй (озвучка их не любит).
  Плохо: «Рэд крался по заброшенной деревне с автоматом.»
  Хорошо: «Короче, иду я тихо, как мышь.»
  Плохо: «Рэд бросил гранату во врага.»
  Хорошо: «И тут я швыряю гранату, ну всё!»
  Вместе фразы складываются в одну цельную историю с интригой и твистом в конце."""


SAMPLE_STORY = {
    "title": "Последний бросок",
    "summary": "Рэд бросает гранату во врага и выигрывает матч.",
    "caption": "Он бросил гранату... 😱 #freefire #история #battleroyale",
    "cast": [
        {"name": "Рэд", "description": "young soldier in red bandana, green tactical vest, backpack"},
        {"name": "Враг", "description": "enemy soldier in black armor and gas mask"},
    ],
    "scenes": [
        {"narration": "Рэд крался с автоматом в руках", "background": "ruined village at sunset",
         "characters": [{"name": "Рэд", "pose": "sneaking", "holding": "assault rifle"}],
         "prop": None, "prop_motion": "none", "sfx": "none"},
        {"narration": "Впереди стоял вражеский боец", "background": "ruined village street",
         "characters": [{"name": "Рэд", "pose": "crouching", "holding": "hand grenade"},
                        {"name": "Враг", "pose": "default", "holding": "rifle"}],
         "prop": None, "prop_motion": "none", "sfx": "none"},
        {"narration": "Рэд бросил гранату во врага", "background": "ruined village street",
         "characters": [{"name": "Рэд", "pose": "just threw a grenade, arm extended forward, empty hand"},
                        {"name": "Враг", "pose": "default", "holding": "rifle"}],
         "prop": "hand grenade", "prop_motion": "throw_right", "sfx": "whoosh"},
        {"narration": "Взрыв накрыл вражескую позицию", "background": "ruined village street",
         "characters": [{"name": "Рэд", "pose": "default"}],
         "prop": "explosion fireball", "prop_motion": "explosion", "prop_side": "right", "sfx": "boom"},
        {"narration": "Рэд победил в этом матче", "background": "victory podium on the beach",
         "characters": [{"name": "Рэд", "pose": "victory pose", "holding": "golden trophy"}],
         "prop": None, "prop_motion": "none", "sfx": "hit"},
    ],
}

SAMPLE_STORY_PHOTO = {
    "title": "Последний бросок",
    "summary": "Рэд бросает гранату во врага и выигрывает матч.",
    "caption": "Он бросил гранату... 😱 #freefire #история #battleroyale",
    "cast": [
        {"name": "Рэд", "description": "young soldier in red bandana, green tactical vest, backpack"},
        {"name": "Враг", "description": "enemy soldier in black armor and gas mask"},
    ],
    "scenes": [
        {"narration": "Рэд крался с автоматом в руках",
         "image_prompt": "[Рэд] sneaking through a ruined village at sunset, holding an assault rifle", "sfx": "none"},
        {"narration": "Впереди стоял вражеский боец",
         "image_prompt": "[Рэд] crouching behind a wall, [Враг] standing in the street ahead with a rifle", "sfx": "none"},
        {"narration": "Рэд бросил гранату во врага",
         "image_prompt": "[Рэд] on the left throwing a grenade, grenade flying through the air toward [Враг] on the right",
         "sfx": "whoosh"},
        {"narration": "Взрыв накрыл вражескую позицию",
         "image_prompt": "big explosion on the street, [Враг] thrown back by the blast, [Рэд] watching from afar",
         "sfx": "boom"},
        {"narration": "Рэд победил в этом матче",
         "image_prompt": "[Рэд] on a victory podium on the beach holding a golden trophy", "sfx": "hit"},
    ],
}


def story_prompt_photo(videos, cast):
    hist = []
    for v in videos[-20:]:
        st = v.get("stats") or {}
        hist.append(f"- {v['title']}: {v.get('summary', '')} | статус: {v['status']} | "
                    f"просмотры: {st.get('views', '?')}, лайки: {st.get('likes', '?')}, "
                    f"комментарии: {st.get('comments', '?')}")
    heroes = [f"- {c['name']}: {c['description']}" for c in cast.values()]
    return f"""Ты сценарист коротких вертикальных видео (Instagram Reels) про мир Free Fire (королевская битва).
Видео - слайдшоу из картинок: каждая сцена это ОДНА готовая картинка, на которой уже есть всё: герои, предметы в руках, действие, фон.
Придумай НОВУЮ цельную историю с завязкой, напряжением и сильной концовкой или твистом.
Не копируй официальных персонажей игры: герои вымышленные.

Уже снятые ролики и их результаты (не повторяй сюжеты, учитывай, что заходит лучше):
{chr(10).join(hist) or '- пока нет'}

Уже существующие герои (можно использовать снова, тогда НЕ добавляй их в cast):
{chr(10).join(heroes) or '- пока нет'}

ПРАВИЛА
- 12-16 сцен, каждая = одна картинка на 2-3 секунды.
{NARRATION_STYLE}
- image_prompt: ТОЛЬКО по-английски, одно предложение: что именно на картинке. Должен ТОЧНО совпадать с narration:
  кто в кадре, что делает, что держит в руках, где находится враг, куда летит предмет, что происходит вокруг.
  Если в тексте бросили гранату во врага, на картинке виден бросающий, летящая граната и враг, в которого она летит.
  Композиция понятная, герои крупно, не более 2-3 героев. Никаких надписей на картинке.
- Героев в image_prompt пиши по имени в квадратных скобках: [Рэд]. Код сам подставит их внешность.
  Враги и противники тоже герои: добавляй их в cast.
- cast: только НОВЫЕ герои (до 4), description по-английски: внешность, одежда, цвета, без имён.
- caption: по-русски, цепляющая подпись и 3-5 хештегов.
- sfx: "whoosh", "hit", "boom", "shot" или "none".

Верни ТОЛЬКО JSON:
{{"title": "...", "summary": "1 предложение о сюжете", "caption": "...",
 "cast": [{{"name": "...", "description": "..."}}],
 "scenes": [{{"narration": "...", "image_prompt": "...", "sfx": "none"}}]}}"""


def calm(text):
    """Озвучка не передаёт эмоции: убираем восклицания, многоточия, кавычки и КАПС."""
    t = str(text).replace("!", ".").replace("…", ".").replace("...", ".")
    t = re.sub(r"[\"«»“”„]", "", t).replace("—", "-").replace("–", "-")
    t = re.sub(r"\b([А-ЯЁA-Z]{4,})\b", lambda m: m.group(1).capitalize(), t)
    t = re.sub(r"\.{2,}", ".", t)
    return re.sub(r"\s+", " ", t).strip()


def validate_photo_story(s):
    scenes = (s.get("scenes") or [])[:45]
    s["scenes"] = scenes
    if len(scenes) < 4:
        raise RuntimeError("Gemini вернул слишком короткую историю")
    for sc in scenes:
        sc["narration"] = calm(sc.get("narration", ""))
        sc["image_prompt"] = str(sc.get("image_prompt", "")).strip() or sc["narration"]
        if sc.get("sfx") not in SFX:
            sc["sfx"] = "none"
        sc["prop"], sc["prop_motion"], sc["prop_side"] = None, "none", "center"
    return s


MOTIONS = ("throw_right", "throw_left", "fall", "appear", "explosion")


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

ОБЩИЕ ПРАВИЛА
- 12-16 сцен, каждая сцена = один кадр на 2-3 секунды.
{NARRATION_STYLE}
- background, prop, pose, holding: ТОЛЬКО по-английски, коротко и визуально, СТРОГО по смыслу narration этой сцены.
- В кадре максимум 2 персонажа. Враги и противники тоже персонажи (добавляй их в cast).
- cast: только НОВЫЕ герои (до 4), description по-английски: внешность, одежда, цвета.
- caption: по-русски, цепляющая подпись и 3-5 хештегов.
- sfx: "whoosh", "hit", "boom", "shot" или "none".

ПРЕДМЕТЫ (очень важно)
- Всё, что персонаж ДЕРЖИТ в руках (оружие, граната, трофей, рюкзак, аптечка), указывай в поле holding этого персонажа. Картинка героя рисуется сразу вместе с предметом. НЕ дублируй такой предмет в prop.
- prop только для предметов ВНЕ рук: летящих, падающих, лежащих, взрывов. Иначе prop = null.
- prop_motion: "throw_right" (предмет вылетает из руки ЛЕВОГО по кадру героя и летит вправо), "throw_left" (вылетает из руки ПРАВОГО героя и летит влево), "fall" (падает сверху, например аирдроп), "appear" (лежит или появляется), "explosion" (взрыв), "none".
- prop_side для fall/appear/explosion: "left", "center" или "right".

БРОСКИ
- Первый персонаж в characters стоит слева, второй справа. Бросает ПЕРВЫЙ -> throw_right, цель (враг) вторым персонажем справа. Бросает ВТОРОЙ -> throw_left, цель первым слева.
- В сцене броска у бросающего pose = "just threw <предмет>, arm extended forward, empty hand" и без holding этого предмета.
- Если narration говорит, что бросили/выстрелили во врага, враг обязан быть в кадре на стороне, куда летит предмет. Взрыв показывай следующей сценой с prop_side стороны врага.

Верни ТОЛЬКО JSON:
{{"title": "...", "summary": "1 предложение о сюжете", "caption": "...",
 "cast": [{{"name": "...", "description": "..."}}],
 "scenes": [{{"narration": "...", "background": "...",
   "characters": [{{"name": "...", "pose": "default", "holding": null}}],
   "prop": null, "prop_motion": "none", "prop_side": "center", "sfx": "none"}}]}}"""


def validate_story(s):
    scenes = s.get("scenes") or []
    if len(scenes) < 4:
        raise RuntimeError("Gemini вернул слишком короткую историю")
    for sc in scenes:
        sc["narration"] = str(sc.get("narration", "")).strip()
        sc["background"] = str(sc.get("background", "jungle island")).strip()
        sc["characters"] = (sc.get("characters") or [])[:2]
        if sc.get("prop_motion") not in MOTIONS:
            sc["prop_motion"] = "none"
        if sc.get("prop_side") not in ("left", "center", "right"):
            sc["prop_side"] = "center"
        if sc.get("sfx") not in SFX:
            sc["sfx"] = "none"
        if not sc.get("prop") or sc["prop_motion"] == "none":
            sc["prop"], sc["prop_motion"] = None, "none"
    return s


# ───────────────────────── озвучка и сборка ─────────────────────────

async def _tts(text, path, voice, rate):
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=rate).save(str(path))


def make_voice(text, path):
    if OFFLINE:
        run(["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"sine=f=220:d={0.38 * len(text.split()):.2f}", str(path)])
        return
    # edge-tts иногда не отдаёт звук (лимиты Microsoft): повторяем, меняем скорость и голос
    clean = re.sub(r"[\"«»“”„]", "", text).strip() or "..."
    last = None
    for attempt in range(6):
        voice = VOICE if attempt < 4 else "ru-RU-SvetlanaNeural"
        rate = "+6%" if attempt % 2 == 0 else "+0%"
        try:
            asyncio.run(_tts(clean, path, voice, rate))
            if path.exists() and path.stat().st_size > 1000:
                time.sleep(0.7)
                return
            last = "пустой файл"
        except Exception as e:  # noqa
            last = e
        log(f"озвучка: попытка {attempt + 1} не удалась ({last})")
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Не удалось озвучить фразу «{text}»: {last}")


def subtitle_filters(text, tag):
    parts = []
    for i, line in enumerate(textwrap.wrap(text, width=24)):
        f = WORK / f"{tag}_sub{i}.txt"
        f.write_text(line, encoding="utf-8")
        y = SUB_Y + i * (SUB_SIZE + 18)
        parts.append(f"drawtext=fontfile={FONT}:textfile={f}:fontsize={SUB_SIZE}:"
                     f"fontcolor=black:x=(w-text_w)/2:y={y}")
    return ",".join(parts)


def render_scene(tag, sc, bg, chars, prop, voice, photo=False):
    """chars: список (путь, зеркалить?)."""
    from PIL import Image
    vlen = duration(voice)
    # кадр 2-3 секунды; длинную фразу чуть ускоряем, но речь не обрезаем
    tempo = min((vlen + 0.3) / 3.0, 1.25) if vlen + 0.3 > 3.0 else 1.0
    d = round(min(max(vlen / tempo + 0.3, 2.0), 4.5), 2)

    cmd, n = [], 0

    def add_img(p):
        nonlocal n
        cmd.extend(["-loop", "1", "-t", str(d), "-i", str(p)])
        n += 1
        return n - 1

    ib = add_img(bg)
    ics = [add_img(p) for p, _ in chars]
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
    zoom = (0.05 if PHOTO_ZOOM else 0.0) if photo else 0.08
    shake = (not photo) and (sc["prop_motion"] == "explosion" or sc["sfx"] == "boom")
    cx = f"(in_w-{BOX})/2" + ("+16*sin(80*t)*between(t,0.5,1.2)" if shake else "")
    cy = f"(in_h-{BOX})/2" + ("+10*cos(70*t)*between(t,0.5,1.2)" if shake else "")
    fc = [f"[{ib}:v]scale=w='trunc({BOX}*(1+{zoom}*t/{d})/2)*2':h='trunc({BOX}*(1+{zoom}*t/{d})/2)*2':"
          f"eval=frame,crop={BOX}:{BOX}:'{cx}':'{cy}',format=rgba[l0]"]
    last = "l0"
    ch = 600 if len(ics) > 1 else 640
    widths = []
    for k, ic in enumerate(ics):
        path, flip = chars[k]
        iw, ih = Image.open(path).size
        widths.append(int(iw * ch / ih))
        fc.append(f"[{ic}:v]scale=-2:{ch}{',hflip' if flip else ''},format=rgba[c{k}]")
        x = f"-w+(w+70)*{ease}" if k == 0 else f"{BOX}-(w+70)*{ease}"
        fc.append(f"[{last}][c{k}]overlay=x='{x}':y='H-h+30+7*sin(7*t+{k})':eval=frame[l{k + 1}]")
        last = f"l{k + 1}"

    if ip is not None:
        mot = sc["prop_motion"]
        sidex = int({"left": 0.26, "center": 0.5, "right": 0.74}[sc["prop_side"]] * BOX)
        T0, DUR = 0.4, 0.9
        p = f"min(max(t-{T0},0)/{DUR},1)"
        hand_y = int(BOX - ch + 30 + 0.40 * ch)
        if mot in ("throw_right", "throw_left"):
            if mot == "throw_right" or len(widths) < 2:
                x0 = 70 + int(0.78 * widths[0]) if widths else 300
                x1 = BOX - 90 if mot == "throw_right" else 90
            else:
                x0 = BOX - 70 - int(0.78 * widths[1])
                x1 = 90
            y1 = int(BOX * 0.74)
            fc.append(f"[{ip}:v]scale=-2:150,format=rgba,rotate=a='t*9':ow='hypot(iw,ih)':oh=ow:c=none[pr]")
            pos = (f"x='{x0}+({x1}-{x0})*{p}-w/2':y='{hand_y}+({y1}-{hand_y})*{p}-240*sin(PI*{p})-h/2':"
                   f"eval=frame:enable='between(t,{T0},{T0 + DUR})'")
        elif mot == "fall":
            fc.append(f"[{ip}:v]scale=-2:190,format=rgba,rotate=a='t*3':ow='hypot(iw,ih)':oh=ow:c=none[pr]")
            pos = (f"x='{sidex}-w/2':y='-h+({int(BOX * 0.62)}+h)*pow({p},2)':"
                   f"eval=frame:enable='gte(t,{T0})'")
        elif mot == "explosion":
            size = "trunc((120+420*min(max(t-0.5,0)/0.35,1))/2)*2"
            fc.append(f"[{ip}:v]format=rgba,scale=w='{size}':h='{size}':eval=frame[pr]")  # format ДО scale, иначе размер не меняется
            pos = f"x='{sidex}-w/2':y='{int(BOX * 0.55)}-h/2':eval=frame:enable='gte(t,0.5)'"
        else:  # appear
            fc.append(f"[{ip}:v]scale=-2:230,format=rgba[pr]")
            pos = f"x='{sidex}-w/2':y='{int(BOX * 0.55)}+6*sin(5*t)':eval=frame:enable='gte(t,{T0})'"
        fc.append(f"[{last}][pr]overlay={pos}[lp]")
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

def research(videos):
    """Шаг 1: Gemini с поиском Google находит реальную историю и проверенные факты."""
    import prompts
    hist = "\n".join(f"- {v['title']}: {v.get('summary', '')}" for v in videos[-30:]) or "- пока нет"
    last = ""
    for attempt in range(3):
        focus = random.choice(prompts.FOCUS)
        log(f"поиск истории: {focus}")
        text, srcs = gemini_search(prompts.RESEARCH_PROMPT.replace("<<FOCUS>>", focus).replace("<<HISTORY>>", hist))
        last = text[:200]
        if "НЕТ ИСТОРИИ" not in text and "ФАКТЫ" in text and len(text) > 300:
            return text.strip(), srcs
    raise RuntimeError(f"Не нашёл надёжную историю за 3 попытки: {last}")


def build_script_prompt(facts, cast):
    import prompts
    heroes = "\n".join(f"- {c['name']}: {c['description']}" for c in cast.values()) or "- пока нет"
    return prompts.SCRIPT_PROMPT.replace("<<FACTS>>", facts).replace("<<HEROES>>", heroes)


def cmd_create():
    WORK.mkdir(exist_ok=True)
    vid = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    try:
        refresh_stats()
        videos = load_videos()
        cast = load_cast()
        photo = MODE == "photo"
        facts, sources = "", []
        if OFFLINE:
            story = SAMPLE_STORY_PHOTO if photo else SAMPLE_STORY
        elif photo and STORY_TYPE == "real":
            facts, sources = research(videos)
            story = gemini(build_script_prompt(facts, cast))
        else:
            story = gemini((story_prompt_photo if photo else story_prompt)(videos, cast))
        story = validate_photo_story(story) if photo else validate_story(story)
        have_ref = {k for k, c in cast.items() if (c["dir"] / "ref.png").exists()}
        for c in story.get("cast") or []:
            ensure_character(c["name"], c["description"], cast, portrait=not photo,
                             ref=photo and CHAR_REF)
        if not photo:
            for c in list(cast.values()):
                ensure_character(c["name"], c["description"], cast, portrait=True)

        parts = []
        for i, sc in enumerate(story["scenes"]):
            tag = f"s{i:02d}"
            log(f"сцена {i + 1}/{len(story['scenes'])}: {sc['narration']}")
            seed = random.Random(f"{vid}{i}").randint(1, 10_000_000)
            if photo:
                img = WORK / f"{tag}_img.png"
                refs = ref_urls(sc["image_prompt"], cast, have_ref) if CHAR_REF else []
                extra = ", the characters look exactly like in the reference images" if refs else ""
                gen_image("scene", expand_prompt(sc["image_prompt"], cast) + extra +
                          ", no text, no letters, no watermark", img, seed, refs or None)
                voice = WORK / f"{tag}.mp3"
                make_voice(sc["narration"], voice)
                parts.append(render_scene(tag, sc, img, [], None, voice, photo=True))
                continue
            bg = WORK / f"{tag}_bg.png"
            gen_image("bg", sc["background"] + ", wide scene, no characters", bg, seed)
            layers = []
            for k, ch in enumerate(sc["characters"]):
                c = cast.get(str(ch.get("name", "")).lower())
                if c:
                    layers.append(char_layer(c, ch.get("pose"), ch.get("holding"), k, f"{tag}_c{k}"))
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
         "script": [s["narration"] for s in story["scenes"]], "status": "pending",
         "facts": facts[:6000], "sources": sources}
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
        if sources:
            lines = "\n".join(f"- {x['title'] or 'источник'}: {x['url']}" for x in sources[:6])
            notify("Проверь факты перед публикацией. Источники:\n" + lines)
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
