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
# Для сценария можно взять модель посильнее (один запрос на ролик): например GEMINI_SCRIPT_MODEL=gemini-3.5-flash
# Если GEMINI_SCRIPT_MODEL пустая: цепочка от самой новой Flash к старой (у каждой свой дневной лимит, 20 запросов на free tier).
# Когда у модели кончился дневной лимит, код сразу берёт следующую, а в конце ждёт обычная lite-модель.
DEFAULT_SCRIPT_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
GEMINI_SCRIPT_MODELS = [ENV("GEMINI_SCRIPT_MODEL")] if ENV("GEMINI_SCRIPT_MODEL") else DEFAULT_SCRIPT_MODELS
# Проверка кадров и реальные фото
FRAME_CHECK = (ENV("FRAME_CHECK") or "1") != "0"      # Gemini смотрит каждую картинку: нет ли экрана телефона и надписей
PHOTOS_ON = (ENV("REAL_PHOTOS") or "1") != "0"        # реальные фото из Википедии и Commons для якорных сцен
MAX_PHOTOS = int(ENV("MAX_REAL_PHOTOS") or 4)
WHISPER_ON = (ENV("WHISPER") or "1") != "0"           # точные тайминги слов для субтитров
WHISPER_MODEL = ENV("WHISPER_MODEL") or "small"
TG_TOKEN = ENV("TG_TOKEN", "")
TG_CHAT = ENV("TG_CHAT_ID", "")
IG_USER = ENV("IG_USER_ID", "")
IG_TOKEN = ENV("IG_ACCESS_TOKEN", "")
POLL_KEY = ENV("POLLINATIONS_KEY", "")
# Генерация картинок: hf - Hugging Face Space (FLUX), pollinations, auto - сначала HF, при сбое Pollinations
HF_TOKEN = ENV("HF_TOKEN", "")
# Запасные токены HF_TOKEN1..HF_TOKEN6: когда у одного аккаунта кончается GPU-квота, берётся следующий
HF_TOKENS = list(dict.fromkeys(t.strip() for t in
                               [HF_TOKEN] + [ENV(f"HF_TOKEN{i}", "") for i in range(1, 7)] if t and t.strip()))
# Бесплатные Spaces Hugging Face по очереди: сначала качественный dev, при исчерпании GPU-лимита schnell
HF_SPACES = list(dict.fromkeys(x for x in [ENV("HF_SPACE"), "black-forest-labs/FLUX.2-klein-9B",
                                           "Tongyi-MAI/Z-Image-Turbo", "black-forest-labs/FLUX.1-schnell",
                                           "black-forest-labs/FLUX.1-dev"] if x))
# Картинка из картинки (герой по образцу): Space на FLUX.1-schnell
IMG2IMG_SPACE = ENV("IMG2IMG_SPACE") or "Akjava/flux1-schnell-img2img"
# чем выше, тем сильнее кадр отличается от образца героя (0.7 давало одинаковые стоячие позы)
IMG2IMG_STRENGTH = float(ENV("IMG2IMG_STRENGTH") or 0.85)
# AI Horde - бесплатная сеть GPU без дневной квоты (медленнее, качество проще). Ключ не нужен.
HORDE_KEY = ENV("HORDE_API_KEY") or "0000000000"
HORDE_BUDGET = int(ENV("HORDE_BUDGET") or 1500)      # сколько секунд за запуск готовы ждать Horde
# InstantID: картинка по лицу героя (герой везде узнаваем). Тратит много GPU-времени, поэтому выключен по умолчанию.
INSTANTID = (ENV("INSTANTID") or "").lower() in ("1", "true", "yes")
INSTANTID_SPACE = ENV("INSTANTID_SPACE") or "InstantX/InstantID"
HF_STEPS = ENV("HF_STEPS", "")
# Тема ролика из команды /тема в боте (пусто = случайная из prompts.DEFAULT_TOPICS, как раньше)
TOPIC = (ENV("TOPIC") or "").strip()
# Готовый сценарий автора (команда /make с текстом): Gemini только режет его на сцены и рисует картинки
SCRIPT_TEXT = (ENV("SCRIPT_TEXT") or "").strip()
IMAGE_PROVIDER = (ENV("IMAGE_PROVIDER") or "auto").lower()
AUTO_PUBLISH = (ENV("AUTO_PUBLISH") or "").lower() in ("1", "true", "yes")
# photo  - каждая сцена одно готовое фото (по умолчанию)
# layers - послойная анимация: фон, герои и предметы отдельно
MODE = (ENV("VIDEO_MODE") or "story").lower()   # story - как в примерах (по умолчанию)
PHOTO_ZOOM = (ENV("PHOTO_ZOOM") or "").lower() in ("1", "true", "yes")  # лёгкий наезд на фото
VOICE = ENV("TTS_VOICE") or "ru-RU-DmitryNeural"
# Озвучка: gemini - Gemini TTS (одним запросом на весь ролик), edge - голос Microsoft по фразам, auto - Gemini, потом Edge.
# Лимит бесплатного Gemini TTS: 10 запросов в сутки на модель, поэтому на ролик уходит 1 запрос.
TTS_PROVIDER = (ENV("TTS_PROVIDER") or "auto").lower()
# Сценарист: Claude через Puter (по токену аккаунта puter.com) или Gemini. auto - Claude, если есть токен, иначе Gemini.
PUTER_TOKEN = ENV("PUTER_AUTH_TOKEN", "")
CLAUDE_MODEL = ENV("CLAUDE_MODEL") or "claude-sonnet-4-6"
SCRIPT_WRITER = (ENV("SCRIPT_WRITER") or "auto").lower()
GEMINI_VOICE = ENV("GEMINI_VOICE") or "Charon"
GEMINI_TTS_MODELS = list(dict.fromkeys(m for m in [ENV("GEMINI_TTS_MODEL"), "gemini-3.8-flash-lite-tts",
                                                   "gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview",
                                                   "gemini-2.5-flash-preview-tts"] if m))
TTS_STYLE = ("Calm, even, unhurried conversational delivery, like a friend telling a story. No dramatic emotion, "
             "no exclamations. Steady brisk pace with very short pauses between sentences.")
TTS_PREFIX = "Say in Russian, calmly and evenly, like a friend telling a story, at a steady brisk pace"
# real    - реальные истории про Free Fire (поиск Gemini + проверка фактов), по умолчанию
# fiction - выдуманные истории
STORY_TYPE = (ENV("STORY_TYPE") or "real").lower()
# Tavily - поисковик для ИИ (1000 бесплатных кредитов в месяц): tavily.com
TAVILY_KEY = ENV("TAVILY_API_KEY", "")
TAVILY_DEPTH = ENV("TAVILY_DEPTH") or "advanced"   # basic = 1 кредит, advanced = 2 кредита за поиск
BAD_DOMAINS = ["pinterest.com", "quora.com", "reddit.com", "facebook.com", "instagram.com",
               "tiktok.com", "x.com", "twitter.com", "vk.com", "t.me"]
# Эксперимент: герои по референс-картинке (Pollinations kontext) вместо одного текстового описания
CHAR_REF = (ENV("CHAR_REF") or "true").lower() in ("1", "true", "yes")   # по умолчанию включено, выключить: false
REF_MODEL = ENV("REF_MODEL") or "kontext"
# Токен из «Instagram API с входом через Instagram» начинается с IG, из входа через Facebook - с EAA.
FB = ("https://graph.instagram.com/v21.0" if IG_TOKEN.startswith("IG")
      else "https://graph.facebook.com/v21.0")

W, H = 1080, 1920
BOX = 900
BOX_X = (W - BOX) // 2
BOX_Y = 300
SUB_Y = BOX_Y + BOX + 70
SUB_SIZE = 62
FPS = 30
FONT_CANDIDATES = ["/usr/share/fonts/truetype/montserrat/Montserrat-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
FONT = next((f for f in FONT_CANDIDATES if os.path.exists(f)), FONT_CANDIDATES[-1])
SERIES_CARD = [x.strip() for x in (ENV("SERIES_CARD") or "ИСТОРИЯ|ЗА ОДНУ МИНУТУ").split("|") if x.strip()]
STYLE_STORY = ("detailed digital comic illustration, semi-realistic, cinematic lighting, rich colors, "
               "vertical 9:16 composition, no text")
OVERLAP = 0.25      # длина перехода между кадрами, сек
RED = "0xE0101A"
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

def _err_text(r):
    try:
        return str(r.json()["error"]["message"])[:220]
    except Exception:  # noqa
        return r.text[:220]


def _retry_delay(r):
    try:
        for d in r.json()["error"].get("details", []):
            if "retryDelay" in d:
                return float(str(d["retryDelay"]).rstrip("s"))
    except Exception:  # noqa
        pass
    return None


def _gemini_request(body, patient=True, models=None):
    errors = []
    for model in list(dict.fromkeys((models or []) + GEMINI_MODELS)):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3 if patient else 1):
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=240)
            if r.status_code in (429, 500, 503):
                errors.append(f"{model}: {r.status_code} {_err_text(r)}")
                if not patient:
                    break
                if r.status_code == 429 and "perday" in r.text.lower():
                    break           # дневной лимит этой модели кончился: сразу следующая, не ждём
                wait = _retry_delay(r) or 10 * (attempt + 1)
                if wait > 75:
                    break
                time.sleep(wait + 1)
                continue
            if r.status_code == 404:
                errors.append(f"{model}: модель не найдена")
                break
            if not r.ok:
                raise RuntimeError(f"Gemini {model} {r.status_code}: {_err_text(r)}")
            return r.json()
    raise RuntimeError("Gemini недоступен: " + " | ".join(errors[-4:]))


def _text_of(resp):
    parts = resp["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def gemini(prompt, as_json=True, models=None):
    cfg = {"temperature": 1.0}
    if as_json:
        cfg["responseMimeType"] = "application/json"
    text = _text_of(_gemini_request({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg},
                                    models=models))
    if not as_json:
        return text
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def parse_json_loose(text):
    text = re.sub(r"^```(?:json)?|```$", "", str(text).strip(), flags=re.M).strip()
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("в ответе нет JSON")
    return json.loads(text[a:b + 1])


def claude_json(prompt):
    """Сценарий от Claude. Puter даёт OpenAI-совместимый адрес, токен берётся на puter.com/dashboard."""
    last = None
    for attempt in range(2):
        r = requests.post("https://api.puter.com/puterai/openai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {PUTER_TOKEN}", "Content-Type": "application/json"},
                          json={"model": CLAUDE_MODEL, "max_tokens": 8000,
                                "messages": [{"role": "user", "content": prompt}]}, timeout=600)
        if not r.ok:
            raise RuntimeError(f"Claude/Puter {r.status_code}: {r.text[:220]}")
        content = r.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
        try:
            return parse_json_loose(content)
        except Exception as e:  # noqa
            last = e
            prompt += "\n\nВерни ТОЛЬКО валидный JSON без пояснений."
    raise RuntimeError(f"Claude вернул не JSON: {last}")


def gemini_search(prompt):
    """Ответ Gemini с поиском Google. Возвращает (текст, [источники])."""
    resp = _gemini_request({"contents": [{"parts": [{"text": prompt}]}],
                            "tools": [{"google_search": {}}],
                            "generationConfig": {"temperature": 0.4}}, patient=False)
    srcs = []
    meta = resp["candidates"][0].get("groundingMetadata") or {}
    for ch in meta.get("groundingChunks") or []:
        w = ch.get("web") or {}
        if w.get("uri") and not any(x["url"] == w["uri"] for x in srcs):
            srcs.append({"title": w.get("title", ""), "url": w["uri"]})
    return _text_of(resp), srcs[:8]


# ───────────────────────── картинки ─────────────────────────

def pollinations(prompt, path, seed, refs=None, size=(1024, 1024), style=None):
    from urllib.parse import quote
    url = "https://gen.pollinations.ai/image/" + quote(f"{prompt}, {style or STYLE}"[:900])
    params = {"width": size[0], "height": size[1], "seed": seed, "nologo": "true", "model": "flux"}
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
            if r.status_code in (401, 402, 403):     # нет баланса или ключ не принят - повторы бесполезны
                raise PollDead(f"{r.status_code} {r.text[:140]}")
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
    if kind == "vscene":
        im = Image.new("RGB", (1024, 1824))
        d = ImageDraw.Draw(im)
        for y in range(1824):
            d.line([(0, y), (1024, y)], fill=(col[0] - y // 14 % 70, col[1] - y // 20, 90 + y // 12))
        d.rectangle([0, 1400, 1024, 1824], fill=(40, 70, 50))
        d.ellipse([380, 520, 640, 780], fill=(240, 200, 160))
        d.rectangle([330, 780, 690, 1380], fill=tuple(rnd.randint(60, 220) for _ in range(3)))
    elif kind == "scene":
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


class HFQuota(RuntimeError):
    pass


class PollDead(RuntimeError):
    pass


class NoStory(RuntimeError):
    """Не нашлась надёжная история по теме: не авария, просто сообщаем в Telegram."""


_hf = {"clients": {}, "dead": set(), "poll_dead": False, "horde_dead": False, "horde_spent": 0.0, "fails": {}, "tok_dead": set(), "api": {}, "noted": set()}


def quota_note(key, text):
    if key not in _hf["noted"]:
        _hf["noted"].add(key)
        notify(text)


def _walk_paths(obj):
    """Все строки-пути/ссылки на картинки внутри ответа Space (он бывает списком, словарём, галереей)."""
    if isinstance(obj, str):
        if re.search(r"\.(png|jpe?g|webp)(\?.*)?$", obj, re.I) or obj.startswith("/tmp") or obj.startswith("/var"):
            yield obj
    elif isinstance(obj, dict):
        for k in ("path", "image", "url", "value"):
            if k in obj:
                yield from _walk_paths(obj[k])
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            yield from _walk_paths(x)


def _space_kwargs(params, prompt, seed, size, face, strength=None):
    """Подбирает аргументы Space по именам его параметров: у разных Spaces они немного отличаются."""
    from gradio_client import handle_file
    kw, face_used = {}, False
    for p in params:
        name, low = p["parameter_name"], p["parameter_name"].lower()
        comp = str(p.get("component", "")).lower()
        has_def = p.get("parameter_has_default", False)
        if "negative" in low:
            kw[name] = "text, watermark, letters, deformed, blurry, low quality"
        elif low == "prompt" or low.endswith("prompt"):
            kw[name] = prompt
        elif "random" in low:
            kw[name] = False
        elif "seed" in low:
            kw[name] = int(seed) % 2147483647
        elif low == "width":
            kw[name] = size[0]
        elif low == "height":
            kw[name] = size[1]
        elif "resolution" in low:
            enum = (p.get("type") or {}).get("enum") or []
            pick = next((e for e in enum if any(t in str(e) for t in ("9:16", "768x1344", "720x1280", "1344"))
                         and "16:9" not in str(e)), None)
            if pick:
                kw[name] = pick
        elif low in ("steps", "num_inference_steps") and HF_STEPS:
            kw[name] = int(HF_STEPS)
        elif face and ("strength" in low or "denois" in low):
            kw[name] = strength if strength is not None else IMG2IMG_STRENGTH
        elif comp in ("image", "file", "gallery") or "image" in low or "file" in low:
            if face and not face_used and "pose" not in low and "mask" not in low:
                kw[name] = handle_file(str(face))
                face_used = True
            elif not has_def:
                kw[name] = None
        elif not has_def:
            kw[name] = None
    return kw


def fit_vertical(path):
    """Обрезает картинку по центру до 9:16 (если Space отдал квадрат)."""
    from PIL import Image
    im = Image.open(path)
    w, h = im.size
    want = 9 / 16
    if abs(w / h - want) > 0.02:
        if w / h > want:
            nw = int(h * want)
            im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
        else:
            nh = int(w / want)
            im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    im.convert("RGB").save(path)


def horde_generate(prompt, path, seed, size):
    """Картинка через AI Horde (анонимный ключ). Очередь может занять минуты."""
    import base64
    import io
    from PIL import Image
    base = "https://aihorde.net/api/v2"
    h = {"apikey": HORDE_KEY, "Client-Agent": "video-bot:1.0:github", "Content-Type": "application/json"}
    vertical = size[1] > size[0]
    body = {"prompt": f"{prompt} ### text, letters, watermark, deformed, blurry, low quality",
            "params": {"width": 448 if vertical else 512, "height": 768 if vertical else 512, "steps": 25,
                       "n": 1, "cfg_scale": 7, "sampler_name": "k_euler_a", "seed": str(int(seed) % 2147483647)},
            "nsfw": False, "censor_nsfw": True}
    t0 = time.time()
    try:
        r = requests.post(f"{base}/generate/async", headers=h, json=body, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"Horde {r.status_code}: {r.text[:200]}")
        jid = r.json()["id"]
        while True:
            if time.time() - t0 > 240:
                requests.delete(f"{base}/generate/status/{jid}", headers=h, timeout=30)
                raise RuntimeError("Horde: очередь слишком долгая")
            c = requests.get(f"{base}/generate/check/{jid}", headers=h, timeout=30).json()
            if c.get("faulted"):
                raise RuntimeError("Horde: сбой генерации")
            if c.get("done"):
                break
            time.sleep(4)
        gens = requests.get(f"{base}/generate/status/{jid}", headers=h, timeout=60).json().get("generations") or []
        if not gens:
            raise RuntimeError("Horde: пустой ответ")
        img = gens[0]["img"]
        data = requests.get(img, timeout=120).content if str(img).startswith("http") else base64.b64decode(img)
        Image.open(io.BytesIO(data)).convert("RGB").save(path)
        if vertical:
            fit_vertical(path)
    finally:
        _hf["horde_spent"] += time.time() - t0
        if _hf["horde_spent"] > HORDE_BUDGET:
            _hf["horde_dead"] = True


def ensure_gradio():
    """Если библиотека не установлена (старый requirements.txt в репозитории), ставит её сама."""
    try:
        import gradio_client  # noqa: F401
    except ImportError:
        log("ставлю gradio_client...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "gradio_client"], check=True)


def hf_generate(prompt, path, seed, size, space, face=None, strength=None):
    """Пробует токены по очереди. Квота ZeroGPU общая на аккаунт, поэтому токен без квоты
    сразу считается мёртвым для ВСЕХ Spaces. HFQuota наружу летит, когда кончились все токены."""
    tokens = HF_TOKENS or [""]            # без токенов - анонимный доступ, как раньше
    alive = [i for i in range(len(tokens)) if i not in _hf["tok_dead"]]
    if not alive:
        raise HFQuota(f"квота исчерпана на всех токенах ({len(tokens)})")
    for n, i in enumerate(alive):
        try:
            _hf_generate_one(prompt, path, seed, size, space, tokens[i], face, strength)
            return
        except HFQuota as e:
            _hf["tok_dead"].add(i)
            left = len(alive) - n - 1
            log(f"токен #{i + 1} без квоты, осталось токенов: {left}")
            if left == 0:
                raise HFQuota(f"квота исчерпана на всех токенах ({len(tokens)}): {str(e)[:200]}")


def _hf_generate_one(prompt, path, seed, size, space, token, face=None, strength=None):
    """Картинка из бесплатного Hugging Face Space через gradio_client (FLUX, Z-Image-Turbo, InstantID)."""
    ensure_gradio()
    from gradio_client import Client
    ck = (space, token)
    if ck not in _hf["clients"]:
        try:
            _hf["clients"][ck] = Client(space, hf_token=token or None, verbose=False)
        except TypeError:
            _hf["clients"][ck] = Client(space, token=token or None, verbose=False)
    c = _hf["clients"][ck]
    if ck not in _hf["api"]:
        _hf["api"][ck] = c.view_api(print_info=False, return_format="dict")["named_endpoints"]
    api = _hf["api"][ck]
    ep = next((e for e in ("/infer", "/generate", "/generate_image", "/predict") if e in api), None)
    if ep is None:
        ep = next((e for e, v in api.items() if any("prompt" in p["parameter_name"].lower() for p in v["parameters"])), None)
    if ep is None:
        raise RuntimeError(f"в Space {space} не нашёл метод генерации")
    kwargs = _space_kwargs(api[ep]["parameters"], prompt, seed, size, face, strength)
    last = None
    for attempt in range(2):
        try:
            res = c.predict(api_name=ep, **kwargs)
            found = next((p for p in _walk_paths(res) if pathlib.Path(p).exists()), None)
            if not found:
                raise RuntimeError(f"Space не вернул картинку: {str(res)[:160]}")
            from PIL import Image
            Image.open(found).convert("RGB").save(path)
            if size[1] > size[0]:
                fit_vertical(path)
            _hf["fails"][space] = 0
            return
        except Exception as e:  # noqa
            last = e
            low = str(e).lower()
            if "quota" in low or "unauthorized" in low or "invalid token" in low:
                raise HFQuota(str(e)[:300])
            time.sleep(5 * (attempt + 1))
    _hf["fails"][space] = _hf["fails"].get(space, 0) + 1
    if _hf["fails"][space] >= 2:
        _hf["dead"].add(space)
    raise RuntimeError(f"HF {space}: {str(last)[:300]}")


def gen_image(kind, prompt, path, seed, refs=None, face=None, init=None, strength=None):
    if OFFLINE:
        fake_image(kind, prompt, path)
        return
    vertical = kind == "vscene"
    use_hf = IMAGE_PROVIDER in ("hf", "auto") and not refs      # референсы умеет только Pollinations
    use_poll = IMAGE_PROVIDER in ("pollinations", "auto") or bool(refs)
    use_horde = IMAGE_PROVIDER in ("horde", "auto") and not refs
    errors = []
    if init and CHAR_REF and IMAGE_PROVIDER in ("hf", "auto") and IMG2IMG_SPACE not in _hf["dead"]:
        try:       # новая сцена по образцу героя: одежда и внешность сохраняются
            hf_generate(f"{prompt}, {STYLE_STORY if vertical else STYLE}", path, seed,
                        (768, 1344) if vertical else (1024, 1024), IMG2IMG_SPACE, face=init, strength=strength)
            return
        except HFQuota as e:
            _hf["dead"].add(IMG2IMG_SPACE)
            quota_note("img2img", "ℹ️ Квота GPU на всех токенах Hugging Face исчерпана, рисую без образца героя")
        except Exception as e:  # noqa
            errors.append(f"img2img: {str(e)[:140]}")
    if face and INSTANTID and IMAGE_PROVIDER in ("hf", "auto") and INSTANTID_SPACE not in _hf["dead"]:
        try:       # картинка по лицу героя
            hf_generate(f"{prompt}, {STYLE_STORY if vertical else STYLE}", path, seed,
                        (768, 1344) if vertical else (1024, 1024), INSTANTID_SPACE, face=face)
            return
        except HFQuota as e:
            _hf["dead"].add(INSTANTID_SPACE)
            quota_note("instantid", "ℹ️ Квота GPU на всех токенах Hugging Face исчерпана, рисую без привязки к лицу")
        except Exception as e:  # noqa
            errors.append(f"InstantID: {str(e)[:160]}")
    if use_hf:
        for space in HF_SPACES:
            if space in _hf["dead"]:
                continue
            try:
                hf_generate(f"{prompt}, {STYLE_STORY if vertical else STYLE}", path, seed,
                            (768, 1344) if vertical else (1024, 1024), space)
                return
            except HFQuota as e:
                _hf["dead"].add(space)
                errors.append(f"{space}: квота")
                quota_note("hf", "ℹ️ Квота GPU Hugging Face исчерпана на всех токенах. Дальше Pollinations и Horde.")
            except Exception as e:  # noqa
                errors.append(f"{space}: {str(e)[:160]}")
    if use_poll and not _hf["poll_dead"]:
        try:
            if vertical:
                pollinations(prompt, path, seed, refs, size=(1024, 1824), style=STYLE_STORY)
            else:
                pollinations(prompt, path, seed, refs)
            return
        except PollDead as e:
            _hf["poll_dead"] = True
            errors.append(f"pollinations: {str(e)[:120]}")
            notify(f"ℹ️ Pollinations недоступен ({str(e)[:140]}). Дальше рисую только через Hugging Face.")
        except Exception as e:  # noqa
            errors.append(f"pollinations: {str(e)[:160]}")
    if use_horde and not _hf["horde_dead"]:
        try:
            horde_generate(f"{prompt}, {STYLE_STORY if vertical else STYLE}", path, seed,
                           (768, 1344) if vertical else (1024, 1024))
            return
        except Exception as e:  # noqa
            errors.append(f"horde: {str(e)[:140]}")
    raise RuntimeError("Не удалось нарисовать картинку: " + (" | ".join(errors) or "лимиты всех сервисов исчерпаны"))


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


def ensure_character(name, desc, cast, portrait=True, ref=False, face=False):
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
    if face and not (c["dir"] / "face.png").exists():
        gen_image("scene", f"{c['description']}, close-up portrait, face centered, looking at the camera, "
                  "plain simple background, sharp clear face", c["dir"] / "face.png", c["seed"])
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
    text = re.sub(r"[^0-9A-Za-zА-Яа-яЁё\s.,?!:;%/+&'\"«»“”„…—–()\-]", "", str(text))   # хинди, иероглифы и т.п.
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


SHOTS = ["extreme close-up on the face", "wide establishing shot from high above", "low angle shot looking up",
         "over-the-shoulder shot", "side profile medium shot", "dramatic aerial view", "close-up on hands and objects",
         "silhouette against a bright light", "dutch angle, dynamic action pose", "far wide shot, tiny figure in a huge space"]


def diversify_prompts(scenes):
    """Если подряд идут почти одинаковые промпты картинок, добавляем другой ракурс."""
    seen = []
    for i, sc in enumerate(scenes):
        words = set(re.findall(r"[a-z]{4,}", sc["image_prompt"].lower()))
        if any(len(words & w) / max(1, len(words | w)) > 0.5 for w in seen[-4:]):
            sc["image_prompt"] = sc["image_prompt"].rstrip(". ") + ", " + SHOTS[i % len(SHOTS)]
        seen.append(words)


def ahash(path):
    """Мини-отпечаток картинки (256 бит): похожие кадры дают близкие числа."""
    from PIL import Image
    im = Image.open(path).convert("L").resize((16, 16))
    px = list(im.getdata())
    avg = sum(px) / len(px)
    return sum(1 << k for k, v in enumerate(px) if v > avg)


def is_repeat(path, recent, limit=22):
    try:
        h = ahash(path)
    except Exception:  # noqa
        return False
    return any(bin(h ^ r).count("1") <= limit for r in recent)


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


class TTSQuota(RuntimeError):
    pass


_tts_dead = set()


def gemini_tts_once(model, text, out_wav):
    """Один запрос Gemini TTS -> wav. Модели 3.8 принимают стиль отдельным полем, старые - фразой перед текстом."""
    import base64
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    cfg = {"responseModalities": ["AUDIO"],
           "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE}}}}
    v38 = "3.8" in model
    parts = [{"text": text, "speech_metadata": {"style": TTS_STYLE}}] if v38 else [{"text": f"{TTS_PREFIX}: {text}"}]
    r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY},
                      json={"contents": [{"parts": parts}], "generationConfig": cfg}, timeout=300)
    if r.status_code == 400 and v38:        # поле стиля не принято - читаем без стиля, но дословно
        r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY},
                          json={"contents": [{"parts": [{"text": text}]}], "generationConfig": cfg}, timeout=300)
    if r.status_code == 429:
        raise TTSQuota(_err_text(r))
    if not r.ok:
        raise RuntimeError(f"{model} {r.status_code}: {_err_text(r)}")
    inline = None
    for p in r.json()["candidates"][0]["content"]["parts"]:
        inline = p.get("inlineData") or p.get("inline_data") or inline
    if not inline:
        raise RuntimeError(f"{model}: ответ без аудио")
    raw = base64.b64decode(inline["data"])
    mime = inline.get("mimeType") or inline.get("mime_type") or ""
    if "wav" in mime or "mpeg" in mime or "mp3" in mime:
        tmp = WORK / "tts_raw.bin"
        tmp.write_bytes(raw)
        run(["ffmpeg", "-y", "-i", str(tmp), "-ac", "1", "-ar", "24000", str(out_wav)])
    else:                                    # PCM 16 бит, обычно 24 кГц
        rate = (re.search(r"rate=(\d+)", mime) or [None, "24000"])[1]
        tmp = WORK / "tts_raw.pcm"
        tmp.write_bytes(raw)
        run(["ffmpeg", "-y", "-f", "s16le", "-ar", rate, "-ac", "1", "-i", str(tmp), str(out_wav)])
    if not out_wav.exists() or duration(out_wav) < 0.5:
        raise RuntimeError(f"{model}: пустое аудио")


def find_silences(wav):
    r = subprocess.run(["ffmpeg", "-i", str(wav), "-af", "silencedetect=noise=-38dB:d=0.10", "-f", "null", "-"],
                       capture_output=True, text=True)
    out = []
    for e, dur in re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", r.stderr):
        e, dur = float(e), float(dur)
        out.append((e - dur / 2, dur))
    return out


def plan_cuts(total, weights, cands):
    """Где резать общую озвучку на фразы-сцены: ближайшие к ожидаемым местам паузы (динамическое программирование)."""
    n = len(weights)
    if n <= 1:
        return []
    tot, cum, exp = sum(weights), 0, []
    for w in weights[:-1]:
        cum += w
        exp.append(total * cum / tot)
    pts = [(c, -min(d, 0.6)) for c, d in cands if 0.3 < c < total - 0.3] + [(e, 0.8) for e in exp]
    pts.sort()
    m, INF = len(pts), 1e18
    dp = [[INF] * m for _ in range(n - 1)]
    back = [[-1] * m for _ in range(n - 1)]
    for j in range(m):
        dp[0][j] = (pts[j][0] - exp[0]) ** 2 + pts[j][1]
    for k in range(1, n - 1):
        best, bi, p = INF, -1, 0
        for j in range(m):
            while p < j and pts[p][0] <= pts[j][0] - 0.3:
                if dp[k - 1][p] < best:
                    best, bi = dp[k - 1][p], p
                p += 1
            if bi >= 0:
                dp[k][j] = best + (pts[j][0] - exp[k]) ** 2 + pts[j][1]
                back[k][j] = bi
    j = min(range(m), key=lambda x: dp[n - 2][x])
    cuts = [pts[j][0]]
    for k in range(n - 2, 0, -1):
        j = back[k][j]
        cuts.append(pts[j][0])
    return sorted(cuts)


def split_voice(wav, phrases, tag):
    total = duration(wav)
    cuts = [0.0] + plan_cuts(total, [len(p) + 3 for p in phrases], find_silences(wav)) + [total]
    files = []
    for i in range(len(phrases)):
        out = WORK / f"{tag}_{i:02d}.wav"
        run(["ffmpeg", "-y", "-i", str(wav), "-ss", f"{cuts[i]:.3f}", "-to", f"{cuts[i + 1]:.3f}",
             "-ac", "1", str(out)])
        trim_silence(out)
        files.append(out)
    return files


def make_all_voices(scenes):
    """Озвучка всего ролика одним запросом (частями, если текст длиннее лимита поля). None - если не вышло."""
    if OFFLINE or TTS_PROVIDER == "edge" or not GEMINI_KEY:
        return None
    phrases = [(sc["narration"].rstrip() if sc["narration"].rstrip()[-1:] in ".?" else sc["narration"].rstrip() + ".")
               for sc in scenes]
    chunks, cur, size = [], [], 0
    for i, ph in enumerate(phrases):
        b = len(ph.encode("utf-8")) + 1
        if cur and size + b > 3800:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += b
    chunks.append(cur)
    files = [None] * len(phrases)
    try:
        for ci, idxs in enumerate(chunks):
            wav = WORK / f"tts_chunk{ci}.wav"
            text = " ".join(phrases[i] for i in idxs)
            last = None
            for model in GEMINI_TTS_MODELS:
                if model in _tts_dead:
                    continue
                try:
                    gemini_tts_once(model, text, wav)
                    log(f"озвучка: {model}, часть {ci + 1}/{len(chunks)}")
                    break
                except TTSQuota as e:
                    _tts_dead.add(model)
                    last = f"{model}: лимит ({str(e)[:100]})"
                except Exception as e:  # noqa
                    last = f"{model}: {str(e)[:140]}"
            else:
                raise RuntimeError(last or "нет доступных моделей")
            for i, f in zip(idxs, split_voice(wav, [phrases[i] for i in idxs], f"tts{ci}")):
                files[i] = f
        return files
    except Exception as e:  # noqa
        notify(f"ℹ️ Gemini TTS недоступен ({str(e)[:200]}). Озвучиваю голосом Microsoft.")
        return None


def trim_silence(path):
    """Срезает тишину в начале и конце фразы, чтобы между предложениями не было длинных пауз."""
    tmp = pathlib.Path(str(path) + ".trim.wav")
    run(["ffmpeg", "-y", "-i", str(path), "-af",
         "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.03,areverse,"
         "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse",
         "-f", "wav", str(tmp)])
    if tmp.exists() and tmp.stat().st_size > 2000:
        tmp.replace(path)


def make_voice(text, path):
    if OFFLINE:
        run(["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"sine=f=220:d={0.38 * len(text.split()):.2f}", str(path)])
        trim_silence(path)
        return
    # edge-tts иногда не отдаёт звук (лимиты Microsoft): повторяем, меняем скорость и голос
    clean = re.sub(r"[\"«»“”„]", "", text).strip() or "..."
    last = None
    for attempt in range(6):
        voice = VOICE if attempt < 4 else "ru-RU-SvetlanaNeural"
        rate = "+12%" if attempt % 2 == 0 else "+0%"
        try:
            asyncio.run(_tts(clean, path, voice, rate))
            if path.exists() and path.stat().st_size > 1000:
                trim_silence(path)
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
    for i, line in enumerate(wrap_px(text, SUB_SIZE, BOX * 0.96)):
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


# ───────────────────────── режим story: как в примерах ─────────────────────────
# Полноэкранные картинки 9:16, слова на экране по одному, плавные переходы, музыка под голосом.

SAMPLE_STORY_SHORTS = {
    "title": "Вратарь, который изменил футбол",
    "summary": "История вратаря, ставшего легендой.",
    "caption": "Он начинал на заводе... #историязаминуту #freefire",
    "title_line": "ЛЕВ ЯШИН",
    "cast": [{"name": "Игрок", "description": "tall man with short dark hair in a black goalkeeper jersey and flat cap, 1950s"}],
    "scenes": [
        {"narration": "Этот вратарь поймал мяч, который считали невозможным",
         "image_prompt": "dark silhouette of a goalkeeper in a rainy stadium under floodlights, leather ball on the grass",
         "accent": ["невозможным"], "sfx": "whoosh"},
        {"narration": "А начинал он простым рабочим на заводе",
         "image_prompt": "[Игрок] as a young factory worker among sparks and machines", "accent": ["заводе"],
         "flashback": True},
        {"narration": "В 1929 году в Москве родился мальчик",
         "image_prompt": "old Moscow street, a boy holding a ball", "accent": [],
         "card": {"lines": ["1929 ГОД", "МОСКВА"], "red": 1}},
        {"narration": "Тренеры сомневались, но он не сдавался",
         "image_prompt": "[Игрок] standing before skeptical coaches around a table", "accent": ["сомневались"]},
        {"narration": "Отправь это другу, который любит футбол",
         "image_prompt": "[Игрок] raising a hand in a packed stadium at sunset", "accent": ["другу"], "sfx": "hit"},
    ],
}


def norm_word(w):
    return re.sub(r"[^\w]", "", w.lower())


def looks_like_card(text):
    t = str(text).upper()
    return "/" in t or ("ИСТОРИЯ" in t and "МИНУТ" in t)


def validate_story_script(s):
    s["scenes"] = [sc for sc in (s.get("scenes") or []) if not looks_like_card(sc.get("narration", ""))]
    s = validate_photo_story(s)
    s["title_line"] = str(s.get("title_line") or s.get("title", "")).upper()[:40]
    for sc in s["scenes"]:
        acc = sc.get("accent") or []
        sc["accent"] = [norm_word(a) for a in acc if isinstance(a, str) and a.strip()][:3]
        sc["flashback"] = bool(sc.get("flashback"))
        card = sc.get("card")
        if isinstance(card, dict) and card.get("lines"):
            lines = [str(x).upper()[:28] for x in card["lines"][:3]]
            red = card.get("red")
            sc["card"] = {"lines": lines, "red": red if isinstance(red, int) and 0 <= red < len(lines) else None}
        else:
            sc["card"] = None
    # с первой секунды видно, про что ролик
    if s["scenes"] and not s["scenes"][0].get("card"):
        s["scenes"][0]["card"] = {"lines": ["FREE FIRE"], "red": 0}
    # карточка рубрики после хука: ИМЯ / ИСТОРИЯ / ЗА ОДНУ МИНУТУ (последняя строка красная)
    idx = min(3, len(s["scenes"]) - 1)
    if s["title_line"] and not s["scenes"][idx].get("card"):
        lines = [s["title_line"]] + SERIES_CARD
        s["scenes"][idx]["card"] = {"lines": lines[:3], "red": len(lines[:3]) - 1}
    diversify_prompts(s["scenes"])
    return s


# ───────────────────── промпты картинок, проверка кадров, реальные фото ─────────────────────

# Эти слова генератор принимает за «нарисуй скриншот игры / экран телефона», поэтому из промптов их убираем
BANNED_IMG = ["free fire", "garena", "video game", "mobile game", "gameplay", "screenshot", "phone screen",
              "smartphone screen", "game screen", "computer screen", "tv screen", "screens", "screen", "display",
              "interface", "minimap", "health bar", "hud", "ui", "app", "menu", "logo", "watermark", "caption",
              "subtitle", "text", "game"]
DEVICES_IMG = ["smartphone", "phone", "mobile", "tablet", "laptop", "monitor", "computer", "television", "tv",
               "console", "controller"]


def _word_re(words):
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


def _tidy(t):
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"(\s*,){2,}", ",", t)
    t = re.sub(r"\s+([,.])", r"\1", t)
    return t.strip(" ,")


def clean_image_prompt(prompt):
    """Последняя страховка: выкидываем целые фрагменты (между запятыми) с запрещёнными словами.
    Телефон как предмет в руках остаётся, пропадают экран, интерфейс, название игры."""
    prompt = re.sub(r"\b(no|without)\s+(text|letters|logos?|watermarks?|captions?)\b", "", prompt, flags=re.I)
    bad = _word_re(BANNED_IMG)
    clauses = [c for c in prompt.split(",") if not bad.search(re.sub(r"\[[^\]]*\]", "", c))]
    out = _tidy(", ".join(clauses))
    if len(out.split()) < 5:                     # почти всё выкинули: безопасная замена, герой из скобок остаётся
        names = re.findall(r"\[[^\]]*\]", prompt)
        out = (names[0] if names else "A young man") + " stands in a cinematic wide shot, dramatic lighting, detailed comic illustration"
    return out


def strip_devices(prompt):
    """Для повторной попытки: устройства заменяем нейтральным предметом."""
    parts = re.split(r"(\[[^\]]*\])", prompt)
    dev = _word_re(DEVICES_IMG)
    return _tidy("".join(dev.sub("object", t) if k % 2 == 0 else t for k, t in enumerate(parts)))


def jpeg_small(src, maxside=768):
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src).convert("RGB")
    im.thumbnail((maxside, maxside))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=80)
    return b.getvalue()


def gemini_vision_json(prompt, jpeg_bytes):
    """Gemini смотрит на картинку и отвечает JSON. Идёт на обычные lite-модели (500 запросов в день)."""
    import base64
    body = {"contents": [{"parts": [{"inline_data": {"mime_type": "image/jpeg",
                                                    "data": base64.b64encode(jpeg_bytes).decode()}},
                                    {"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    return parse_json_loose(_text_of(_gemini_request(body, patient=False)))


_vis = {"fails": 0}
FRAME_Q = ("Look at this image. Reply with JSON {\"screen\": true/false, \"text\": true/false}. "
           "screen = true if the picture is mostly or largely a smartphone, computer or TV screen, a game screenshot, "
           "or a game interface (buttons, health bars, minimap, menus). "
           "text = true if there is large readable text, captions, a watermark or a logo.")


def frame_problem(path):
    """Причина, по которой кадр надо перерисовать, или None."""
    if not FRAME_CHECK or OFFLINE or _vis["fails"] >= 2:
        return None
    try:
        out = gemini_vision_json(FRAME_Q, jpeg_small(path))
        _vis["fails"] = 0
    except Exception as e:  # noqa
        _vis["fails"] += 1
        log("проверка кадра пропущена:", str(e)[:100])
        return None
    if out.get("screen") is True:
        return "экран телефона или интерфейс игры"
    if out.get("text") is True:
        return "надписи на кадре"
    return None


def rewrite_image_prompts(story, cast):
    """Второй вызов: отдельный «арт-директор» пишет промпты картинок по готовым фразам, плюс выбирает якорные
    сцены для реальных фото. Если не вышло, остаются промпты из сценария (после чистки запрещённых слов)."""
    import prompts
    scenes = story["scenes"]
    names = "\n".join(f"- [{c['name']}]: {c['description']}" for c in cast.values()) or "- нет"
    p = (prompts.IMAGE_PROMPTS_PROMPT
         .replace("<<SCENES>>", "\n".join(f"{i + 1}: {sc['narration']}" for i, sc in enumerate(scenes)))
         .replace("<<CAST>>", names).replace("<<N>>", str(len(scenes))).replace("<<MAXPHOTOS>>", str(MAX_PHOTOS)))
    lst, photos = None, []
    try:
        out = gemini(p, models=GEMINI_SCRIPT_MODELS)
        lst = out.get("prompts") if isinstance(out, dict) else out
        if not isinstance(lst, list) or len(lst) != len(scenes) or not all(isinstance(x, str) and len(x) > 15 for x in lst):
            raise ValueError(f"промптов {len(lst) if isinstance(lst, list) else '?'} вместо {len(scenes)}")
        photos = out.get("photos") or [] if isinstance(out, dict) else []
    except Exception as e:  # noqa
        log(f"промпты картинок: второй вызов не удался ({str(e)[:140]}), беру из сценария")
        lst = None
    for i, sc in enumerate(scenes):
        sc["image_prompt"] = clean_image_prompt(lst[i] if lst else sc["image_prompt"]) or sc["image_prompt"]
    seen_scene = set()
    for ph in photos[:MAX_PHOTOS]:        # код сам ограничивает число якорных сцен, а не Gemini
        try:
            k, q = int(ph["scene"]) - 1, str(ph["query"]).strip()
        except Exception:  # noqa
            continue
        if 0 <= k < len(scenes) and q and k not in seen_scene:
            scenes[k]["photo_query"] = q[:80]
            seen_scene.add(k)
    diversify_prompts(scenes)


UA = {"User-Agent": "freefire-video-bot/1.0 (GitHub Actions; educational project)"}
_used_photos = set()
_rejected_photos = set()


def _plain(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


def photo_candidates(query):
    """Свободные фото: главная картинка статьи в Википедии и поиск по Wikimedia Commons."""
    from urllib.parse import quote
    out = []
    try:
        r = requests.get("https://en.wikipedia.org/w/api.php", headers=UA, timeout=30, params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": query, "gsrlimit": 4,
            "gsrnamespace": 0, "prop": "pageimages|description", "piprop": "thumbnail", "pithumbsize": 1600,
            "pilicense": "free"})
        pages = sorted(((r.json().get("query") or {}).get("pages") or {}).values(), key=lambda p: p.get("index", 99))
        for p in pages:
            o = p.get("thumbnail") or {}
            if o.get("source"):
                out.append({"url": o["source"], "w": o.get("width", 0), "h": o.get("height", 0),
                            "title": p.get("title", ""), "desc": p.get("description", ""),
                            "artist": "Wikipedia", "license": "free license",
                            "page": "https://en.wikipedia.org/wiki/" + quote(p.get("title", "").replace(" ", "_"))})
    except Exception as e:  # noqa
        log("Википедия (фото):", str(e)[:100])
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=30, params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": 6, "gsrlimit": 8, "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": 1400})
        pages = sorted(((r.json().get("query") or {}).get("pages") or {}).values(), key=lambda p: p.get("index", 99))
        for p in pages:
            ii = (p.get("imageinfo") or [{}])[0]
            md = ii.get("extmetadata") or {}
            if ii.get("mime") not in ("image/jpeg", "image/png"):
                continue
            out.append({"url": ii.get("thumburl") or ii.get("url"), "w": ii.get("width", 0), "h": ii.get("height", 0),
                        "title": p.get("title", "").replace("File:", ""),
                        "desc": _plain((md.get("ImageDescription") or {}).get("value", ""))[:300],
                        "artist": _plain((md.get("Artist") or {}).get("value", ""))[:60] or "Wikimedia Commons",
                        "license": (md.get("LicenseShortName") or {}).get("value", "free license"),
                        "page": ii.get("descriptionurl", "")})
    except Exception as e:  # noqa
        log("Commons (фото):", str(e)[:100])
    return [c for c in out if c["url"] and c["w"] >= 700 and c["h"] >= 500
            and not str(c["url"]).lower().endswith((".svg", ".gif", ".tif", ".tiff"))]


PHOTO_JUDGE = ("You check a photo for use in a video. The narrator says: «<<NARR>>». We searched for: «<<QUERY>>». "
               "The photo caption is: «<<CAP>>». "
               "Do NOT identify anyone by their face: rely only on the caption and on what is visible (place, setting, objects). "
               "ok = true only if the caption and the visible content fit the narrator's phrase, the photo is sharp, has no watermark "
               "or text overlay, is not a collage, a drawing or a screenshot of an interface, and does not show a person in a "
               "humiliating or ambiguous situation. Reply with JSON {\"ok\": true/false, \"reason\": \"short\"}.")


def make_vertical(data, out):
    """Фото -> кадр 9:16. Почти вертикальное режем под кадр, горизонтальное кладём по центру на размытый фон."""
    import io
    from PIL import Image, ImageFilter, ImageEnhance
    im = Image.open(io.BytesIO(data)).convert("RGB")
    fit = min(W / im.width, H / im.height)
    cover = max(W / im.width, H / im.height)
    if (im.width * fit) * (im.height * fit) >= 0.75 * W * H:
        big = im.resize((max(W, int(im.width * cover) + 1), max(H, int(im.height * cover) + 1)))
        x, y = (big.width - W) // 2, (big.height - H) // 2
        big.crop((x, y, x + W, y + H)).save(out)
        return
    bg = im.resize((max(W, int(im.width * cover) + 1), max(H, int(im.height * cover) + 1)))
    x, y = (bg.width - W) // 2, (bg.height - H) // 2
    bg = bg.crop((x, y, x + W, y + H)).filter(ImageFilter.GaussianBlur(28))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    fg = im.resize((W, max(1, int(im.height * W / im.width)))) if im.width * H >= im.height * W \
        else im.resize((max(1, int(im.width * H / im.height)), H))
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    bg.save(out)


def real_photo(sc, out):
    """Ищет свободное фото под фразу сцены, Gemini смотрит и решает, подходит ли. Возвращает источник или None."""
    cands = [c for c in photo_candidates(sc["photo_query"]) if c["url"] not in _used_photos and c["url"] not in _rejected_photos][:5]
    checked = 0
    for c in cands:
        if checked >= 3:
            break
        try:
            data = requests.get(c["url"], headers=UA, timeout=40).content
        except Exception:  # noqa
            continue
        if not 20_000 < len(data) < 15_000_000:
            continue
        checked += 1
        q = (PHOTO_JUDGE.replace("<<NARR>>", sc["narration"]).replace("<<QUERY>>", sc["photo_query"])
             .replace("<<CAP>>", f"{c['title']}. {c['desc']}"[:400]))
        try:
            verdict = gemini_vision_json(q, jpeg_small(data))
        except Exception as e:  # noqa
            log("реальное фото: Gemini не смог проверить, рисую сам:", str(e)[:100])
            return None
        if verdict.get("ok") is True:
            make_vertical(data, out)
            _used_photos.add(c["url"])
            return {"title": f"Фото: {c['title'][:70]}, {c['artist'][:40]}, {c['license']}", "url": c["page"] or c["url"]}
        _rejected_photos.add(c["url"])
        log(f"фото «{c['title'][:50]}» не подошло: {str(verdict.get('reason'))[:80]}")
    return None


# ───────────────────── точные тайминги слов (Whisper) ─────────────────────

_wh = {"model": None, "dead": False}


def whisper_words(wav):
    """[(слово, начало, конец)] по записи диктора или None, если Whisper не заработал."""
    if _wh["dead"] or OFFLINE or not WHISPER_ON:
        return None
    try:
        if _wh["model"] is None:
            from faster_whisper import WhisperModel
            _wh["model"] = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segs, _ = _wh["model"].transcribe(str(wav), language="ru", word_timestamps=True, beam_size=1,
                                          condition_on_previous_text=False)
        return [(w.word.strip(), float(w.start), float(w.end)) for sg in segs for w in (sg.words or []) if w.word.strip()]
    except Exception as e:  # noqa
        _wh["dead"] = True
        log("Whisper недоступен, субтитры по длине слов:", str(e)[:120])
        return None


def align_words(script_words, rec):
    """Привязывает слова сценария к времени из распознавания. Совпавшие слова берут время напрямую,
    остальные (числа, имена, другое написание) делят промежуток между соседями пропорционально длине."""
    import difflib
    a = [norm_word(w) for w in script_words]
    b = [norm_word(w) for w, _, _ in rec]
    times = [None] * len(a)
    matched = 0
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            times[blk.a + k] = (rec[blk.b + k][1], rec[blk.b + k][2])
            matched += 1
    if not a or matched < max(2, len(a) * 0.4):
        return None
    t0, t1 = rec[0][1], rec[-1][2]
    i = 0
    while i < len(a):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(a) and times[j] is None:
            j += 1
        left = times[i - 1][1] if i > 0 else t0
        right = times[j][0] if j < len(a) else t1
        right = max(right, left + 0.05 * (j - i))
        wts = [len(script_words[k]) + 2 for k in range(i, j)]
        t = left
        for k, wt in zip(range(i, j), wts):
            dur = (right - left) * wt / sum(wts)
            times[k] = (t, t + dur)
            t += dur
        i = j
    return times


def words_timeline(text, d, accent, voice=None):
    """Время показа каждого слова. С записью диктора: по распознанным меткам Whisper.
    Без неё или если не вышло: пропорционально длине слова."""
    words = [w for w in (re.sub(r"[.,;:?!«»\"“”]", "", x) for x in text.split()) if w]
    if not words:
        return []

    def is_hot(w):
        nw = norm_word(w)
        return any(nw == a or (len(nw) >= 5 and len(a) >= 5 and nw[:5] == a[:5]) for a in accent)

    real = None
    if voice is not None:
        rec = whisper_words(voice)
        real = align_words(words, rec) if rec else None
    if real:
        starts = [max(0.02, st - 0.04) for st, _ in real]
        for k in range(1, len(starts)):                     # слова идут строго друг за другом
            starts[k] = max(starts[k], starts[k - 1] + 0.05)
        out = []
        for k, w in enumerate(words):
            end = starts[k + 1] if k + 1 < len(words) else d + OVERLAP + 0.5
            out.append([w, starts[k], end, is_hot(w)])
        return out
    weights = [len(w) + 2 for w in words]
    lead, tail = 0.05, 0.10
    span = max(d - lead - tail, 0.5)
    tot, t, out = sum(weights), lead, []
    for w, wt in zip(words, weights):
        dur = span * wt / tot
        out.append([w, t, t + dur, is_hot(w)])
        t += dur
    out[-1][2] = d + OVERLAP + 0.5      # последнее слово держится до конца кадра
    return out


_font_cache = {}


def text_w(text, size):
    """Ширина текста в пикселях тем же шрифтом, которым его рисует ffmpeg."""
    from PIL import ImageFont
    f = _font_cache.get(size)
    if f is None:
        f = _font_cache[size] = ImageFont.truetype(FONT, int(size))
    return f.getlength(text)


def fit_size(text, size, max_w, min_size=36):
    """Уменьшает шрифт, пока текст не влезет по ширине (с запасом на обводку)."""
    while size > min_size and text_w(text, size) + 14 > max_w:
        size -= 2
    return size


def wrap_px(text, size, max_w):
    """Перенос строк по реальной ширине в пикселях."""
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if cur and text_w(trial, size) + 14 > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    return lines + ([cur] if cur else [])


def card_layout(lines, red, size=92, max_w=None, min_size=60):
    """Строки карточки: каждая вписана в ширину, слишком длинная делится на две."""
    max_w = max_w or W * 0.92
    out = []
    for i, ln in enumerate(lines):
        sz = fit_size(ln, size, max_w, min_size)
        parts = [ln]
        if text_w(ln, sz) + 14 > max_w and " " in ln:
            words = ln.split()
            k = min(range(1, len(words)),
                    key=lambda k: max(text_w(" ".join(words[:k]), sz), text_w(" ".join(words[k:]), sz)))
            parts = [" ".join(words[:k]), " ".join(words[k:])]
        for part in parts:
            out.append((part, fit_size(part, size, max_w, 36), red == i))
    return out


def _drawtext(txtfile, size, color, y_expr, enable, border=5, pop_at=None):
    fs = f"'{size}*(1+0.35*max(0,1-(t-{pop_at:.2f})/0.14))'" if pop_at is not None else size
    return (f"drawtext=fontfile={FONT}:textfile={txtfile}:expansion=none:fontsize={fs}:fontcolor={color}:"
            f"borderw={border}:bordercolor=black@0.85:shadowx=2:shadowy=3:shadowcolor=black@0.55:"
            f"x=(w-text_w)/2:y={y_expr}:enable='{enable}'")


def render_clip(tag, sc, img, d, voice=None):
    """Видео-кадр: картинка на весь экран с плавным движением, слова по одному, карточка сверху."""
    total = d + OVERLAP
    frames = int(total * FPS) + 2
    rnd = random.Random(tag)
    kind = rnd.choice(["in", "in", "out", "pan_l", "pan_r"])
    if kind == "in":
        z, x, y = f"1+0.10*on/{frames}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif kind == "out":
        z, x, y = f"1.10-0.10*on/{frames}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif kind == "pan_l":
        z, x, y = "1.08", f"(iw-iw/zoom)*(0.85-0.7*on/{frames})", "ih/2-(ih/zoom/2)"
    else:
        z, x, y = "1.08", f"(iw-iw/zoom)*(0.15+0.7*on/{frames})", "ih/2-(ih/zoom/2)"
    vf = [f"scale=2160:3840,zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS}"]
    if sc.get("flashback"):      # сцены из прошлого: чёрно-белые с зерном
        vf.append("hue=s=0,eq=contrast=1.25:brightness=-0.03,noise=alls=14:allf=t")
    if sc.get("sfx") in ("hit", "boom", "shot"):   # вспышка на ударе
        vf.append("format=yuv420p,fade=t=in:st=0:d=0.10:color=white")

    for i, (w, a, b, hot) in enumerate(words_timeline(sc["narration"], d, sc.get("accent", []), voice)):
        f = WORK / f"{tag}_w{i}.txt"
        f.write_text(w, encoding="utf-8")
        vf.append(_drawtext(f, fit_size(w, 66, W * 0.94 / 1.35, 34), RED if hot else "white",
                            "h*0.63-text_h/2", f"between(t,{a:.2f},{b:.2f})", pop_at=a))
    card = sc.get("card")
    if card:
        y = int(H * 0.09)
        for i, (line, size, is_red) in enumerate(card_layout(card["lines"], card["red"])):
            f = WORK / f"{tag}_c{i}.txt"
            f.write_text(line, encoding="utf-8")
            vf.append(_drawtext(f, size, RED if is_red else "white", str(y), "gte(t,0)", 6))
            y += int(size * 1.22)
    out = WORK / f"{tag}_clip.mp4"
    run(["ffmpeg", "-y", "-i", str(img), "-vf", ",".join(vf) + ",format=yuv420p", "-t", f"{total:.2f}",
         "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", str(out)])
    return out


def scene_audio(tag, sc, voice, d):
    """Озвучка (точно d секунд) + SFX, стерео 44.1 кГц."""
    sfx = SFX.get(sc.get("sfx", "none"))
    cmd = ["ffmpeg", "-y", "-i", str(voice)]
    fc = [f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,apad,atrim=0:{d:.2f}[v]"]
    if sfx:
        cmd += ["-f", "lavfi", "-t", "1.5", "-i", sfx[0]]
        fc.append(f"[1:a]{sfx[1]},aformat=sample_rates=44100:channel_layouts=stereo,adelay={sfx[2]}|{sfx[2]}[x]")
        fc.append("[v][x]amix=inputs=2:duration=first:dropout_transition=0,volume=2[a]")
    else:
        fc.append("[v]anull[a]")
    out = WORK / f"{tag}_aud.wav"
    run(cmd + ["-filter_complex", ";".join(fc), "-map", "[a]", "-ar", "44100", "-ac", "2", str(out)])
    return out


def music_input(total):
    """Музыка под голосом: свой файл из папки music/ или спокойный синтезированный фон."""
    mus = sorted((ROOT / "music").glob("*.mp3")) if (ROOT / "music").exists() else []
    if mus:
        return ["-stream_loop", "-1", "-i", str(random.choice(mus))]
    expr = ("(0.30*sin(2*PI*110*t)+0.22*sin(2*PI*164.8*t)+0.18*sin(2*PI*220.5*t)+0.12*sin(2*PI*329.6*t))"
            "*(0.75+0.25*sin(2*PI*0.12*t))")
    return ["-f", "lavfi", "-t", f"{total:.1f}", "-i", f"aevalsrc='{expr}':s=44100:c=stereo"]


def assemble(clips, audios, durs, out):
    n = len(clips)
    total_audio = sum(durs)
    total = total_audio + OVERLAP
    vlist = WORK / "voice_list.txt"
    vlist.write_text("".join(f"file '{p.resolve()}'\n" for p in audios), encoding="utf-8")
    voice = WORK / "voice_all.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(vlist), "-c", "copy", str(voice)])

    trans = ["fade", "zoomin", "slideleft", "slideright", "wipeleft", "wiperight", "circleopen",
             "radial", "pixelize", "distance", "vuslice", "fadefast"]
    rnd = random.Random(n)
    fc, prev, off = [], "0:v", 0.0
    for k in range(1, n):
        off += durs[k - 1]
        fc.append(f"[{prev}][{k}:v]xfade=transition={rnd.choice(trans)}:duration={OVERLAP}:offset={off:.2f}[x{k}]")
        prev = f"x{k}"
    cmd = ["ffmpeg", "-y"]
    for c in clips:
        cmd += ["-i", str(c)]
    cmd += ["-i", str(voice)] + music_input(total)
    iv, im = n, n + 1
    fc.append(f"[{iv}:a]apad=pad_dur={OVERLAP + 0.5}[vo]")
    fc.append(f"[{im}:a]volume=0.16,afade=t=in:d=1.5,afade=t=out:st={max(total - 2, 0):.1f}:d=2[mu]")
    fc.append("[vo][mu]amix=inputs=2:duration=first:dropout_transition=0,volume=2,"
              "loudnorm=I=-14:TP=-1.5:LRA=9[a]")
    vmap = f"[{prev}]" if n > 1 else "[0:v]"
    run(cmd + ["-filter_complex", ";".join(fc), "-map", vmap, "-map", "[a]", "-t", f"{total:.2f}",
               "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "5M",
               "-bufsize", "10M", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)])


# ───────────────────────── Instagram ─────────────────────────

def ig_publish(video_bytes, caption):
    if not (IG_USER and IG_TOKEN):
        raise RuntimeError("Instagram не настроен (нет IG_USER_ID / IG_ACCESS_TOKEN)")
    r = requests.post(f"{FB}/{IG_USER}/media", data={
        "media_type": "REELS", "upload_type": "resumable",
        "caption": caption[:2200], "access_token": IG_TOKEN}, timeout=60)
    j = r.json()
    if "id" not in j:
        raise RuntimeError(f"Instagram создание контейнера: {j}. Отправь боту /igtest, он покажет причину.")
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


def ig_diagnose():
    """Проверка подключения Instagram: что именно блокирует публикацию. Результат приходит в Telegram."""
    if not (IG_USER and IG_TOKEN):
        return "Instagram не настроен: нет IG_USER_ID или IG_ACCESS_TOKEN."
    kind = ("вход через Instagram (токен IG...)" if IG_TOKEN.startswith("IG")
            else "вход через Facebook (токен EAA...)" if IG_TOKEN.startswith("EAA") else "неизвестный вид токена")
    out = [f"Токен: {kind}", f"Адрес API: {FB.split('//')[1].split('/')[0]}", f"ID аккаунта: {IG_USER}"]

    def check(title, path, fields=None):
        params = {"access_token": IG_TOKEN}
        if fields:
            params["fields"] = fields
        try:
            r = requests.get(f"{FB}/{path}", params=params, timeout=60)
            j = r.json()
        except Exception as e:  # noqa
            out.append(f"❌ {title}: {str(e)[:120]}")
            return None
        if r.ok and "error" not in j:
            out.append(f"✅ {title}: {json.dumps(j, ensure_ascii=False)[:160]}")
            return j
        err = j.get("error", {})
        out.append(f"❌ {title}: {err.get('message', r.text[:120])} (код {err.get('code')}/{err.get('error_subcode')}"
                   f", тип {err.get('type')}, trace {err.get('fbtrace_id')})")
        return None

    check("токен читает профиль", "me", "id,username" if IG_TOKEN.startswith("IG") else "id,name")
    check("аккаунт по IG_USER_ID", IG_USER, "username")
    check("доступ к API публикации", f"{IG_USER}/content_publishing_limit", "quota_usage,config")
    if not IG_TOKEN.startswith("IG"):
        check("выданные права", "me/permissions")
    if any("blocked" in x.lower() for x in out):
        out.append("\n💡 Эта ошибка приходит от Meta, а не от бота: не проходит даже чтение профиля, значит закрыт "
                   "доступ у приложения или токена целиком. Что проверить: 1) в developers.facebook.com в твоём приложении "
                   "нет ли уведомления об ограничении; 2) аккаунт Business или Creator, добавлен тестером в роли приложения, "
                   "и приглашение принято в Instagram (настройки, приложения и сайты); 3) выпусти новый токен и обнови IG_ACCESS_TOKEN.")
    return "\n".join(out)


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


TG_LIMIT_MB = 45     # Telegram принимает файлы бота до 50 МБ


def video_for_telegram(final):
    """Если ролик тяжелее лимита Telegram, делаем лёгкую копию для просмотра."""
    if final.stat().st_size <= TG_LIMIT_MB * 1024 * 1024:
        return final
    kbps = max(int(TG_LIMIT_MB * 0.85 * 1024 * 8 / duration(final)) - 128, 500)
    out = WORK / "preview.mp4"
    run(["ffmpeg", "-y", "-i", str(final), "-vf", "scale=720:1280", "-c:v", "libx264", "-preset", "veryfast",
         "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.3)}k", "-bufsize", f"{kbps * 2}k",
         "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)])
    return out


def upload_release(vid, title, path):
    """Полная версия ролика кладётся во Releases репозитория: оттуда её берёт публикация в Instagram
    (через Telegram бот может скачать только файлы до 20 МБ)."""
    repo, tok = ENV("GITHUB_REPOSITORY"), ENV("GITHUB_TOKEN")
    if not (repo and tok):
        return None
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    r = requests.post(f"https://api.github.com/repos/{repo}/releases", headers=h, timeout=60,
                      json={"tag_name": f"video-{vid}", "name": title[:100], "prerelease": True,
                            "body": "Автоматически созданный ролик"})
    if not r.ok:
        raise RuntimeError(f"release: {r.status_code} {r.text[:200]}")
    with open(path, "rb") as f:
        up = requests.post(r.json()["upload_url"].split("{")[0] + "?name=video.mp4",
                           headers={**h, "Content-Type": "video/mp4"}, data=f, timeout=1800)
    if not up.ok:
        raise RuntimeError(f"upload: {up.status_code} {up.text[:200]}")
    return up.json()["browser_download_url"]


# ───────────────────────── команда create ─────────────────────────

WIKI_UA = {"User-Agent": "video-bot/1.0 (educational project)"}
WIKI_QUERIES = ["Free Fire professional player", "Free Fire YouTuber", "Free Fire streamer",
                "Garena founder", "Free Fire esports player", "Free Fire team captain"]


def wiki_sources(extra=None):
    """Запасной источник фактов: статьи Википедии (без поиска Google)."""
    from urllib.parse import quote
    pages, seen = [], set()
    for q in ([extra] if extra else []) + random.sample(WIKI_QUERIES, 3):
        for lang in ("ru", "en"):
            try:
                api = f"https://{lang}.wikipedia.org/w/api.php"
                r = requests.get(api, params={"action": "query", "list": "search", "srsearch": q,
                                              "srlimit": 2, "format": "json"}, headers=WIKI_UA, timeout=30)
                for hit in r.json().get("query", {}).get("search", []):
                    key = (lang, hit["title"])
                    if key in seen:
                        continue
                    seen.add(key)
                    ex = requests.get(api, params={"action": "query", "prop": "extracts", "explaintext": 1,
                                                   "redirects": 1, "titles": hit["title"], "format": "json"},
                                      headers=WIKI_UA, timeout=30).json()
                    page = next(iter(ex["query"]["pages"].values()))
                    text = (page.get("extract") or "")[:7000]
                    if len(text) > 500:
                        pages.append({"title": hit["title"], "text": text,
                                      "url": f"https://{lang}.wikipedia.org/wiki/" + quote(hit["title"].replace(" ", "_"))})
            except Exception as e:  # noqa
                log("wiki error:", e)
        if len(pages) >= 5:
            break
    return pages[:5]


def tavily_sources(focus):
    """Поиск через Tavily: возвращает список {title, url, text}."""
    queries = [f"Free Fire {focus}"[:380],
               random.choice(["Free Fire pro player biography how he started", "Free Fire streamer success story interview",
                              "Garena Free Fire esports player career path story",
                              "Free Fire content creator how he became famous"])]
    pages, seen = [], set()
    for q in queries:
        r = requests.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {TAVILY_KEY}"},
                          json={"query": q, "search_depth": TAVILY_DEPTH, "max_results": 6,
                                "exclude_domains": BAD_DOMAINS}, timeout=90)
        if not r.ok:
            raise RuntimeError(f"Tavily {r.status_code}: {r.text[:200]}")
        for x in r.json().get("results", []):
            url = x.get("url", "")
            text = (x.get("content") or "")[:3000]
            if url and url not in seen and len(text) > 200:
                seen.add(url)
                pages.append({"title": x.get("title") or url, "url": url, "text": text})
    return pages[:10]


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def _name_tokens(text):
    out = set()
    for w in re.findall(r"\w+", str(text).lower()):
        w = "".join(_TR.get(ch, ch) for ch in w)
        if len(w) >= 4:
            out.add(w)
    return out


def hero_seen(hero_line, people):
    """Этот герой уже был? Сравниваем только имя (до запятой, без страны) и требуем совпадения
    всех слов более короткого имени: Bruno Bittencourt и Bruno Nobru - разные люди."""
    mine = _name_tokens(str(hero_line).split(",")[0])
    if not mine:
        return False
    for p in people:
        theirs = _name_tokens(p)
        common = len(mine & theirs)
        if common and common >= min(len(mine), len(theirs)):
            return True
    return False


def research(videos):
    """Шаг 1: находим реальную историю и проверенные факты.
    Источники по очереди: Tavily -> поиск Google в Gemini -> статьи Википедии.
    Если тема задана вручную, держимся её; иначе берём случайные из prompts.DEFAULT_TOPICS."""
    import prompts
    hist = "\n".join(f"- {v['title']}: {v.get('summary', '')}" for v in videos[-30:]) or "- пока нет"
    people = [str(v.get("person") or "").strip() for v in videos if v.get("person")]
    people_txt = "\n".join(f"- {x}" for x in people) or "- пока нет"
    use_tavily = bool(TAVILY_KEY)
    use_search = (ENV("NO_SEARCH") or "").lower() not in ("1", "true", "yes")
    custom = bool(TOPIC)
    attempts = 3 if custom else 6
    topics = prompts.DEFAULT_TOPICS
    pool = random.sample(topics, min(len(topics), attempts))
    last = ""
    for attempt in range(attempts):
        focus = TOPIC if custom else pool[attempt % len(pool)]
        label = (f"ТЕМА ЗАДАНА ВРУЧНУЮ, держись строго её: {focus}. Если это имя или ник человека, героем должен быть именно он."
                 if custom else focus)
        log(f"поиск истории: {focus}")
        pages, text, srcs = [], None, None
        if use_tavily:
            try:
                pages = tavily_sources(focus)
            except Exception as e:  # noqa
                use_tavily = False
                log("Tavily недоступен:", e)
                notify(f"ℹ️ Tavily недоступен ({str(e)[:200]}). Пробую другой поиск.")
        if not pages and use_search:
            try:
                text, srcs = gemini_search(prompts.RESEARCH_PROMPT.replace("<<FOCUS>>", label)
                                           .replace("<<HISTORY>>", hist).replace("<<PEOPLE>>", people_txt))
            except RuntimeError as e:
                use_search = False
                log("поиск Google недоступен:", e)
                notify(f"ℹ️ Поиск Google в Gemini недоступен ({str(e)[:200]}). Беру факты из Википедии.")
        if text is None:
            if not pages:
                pages = wiki_sources(focus if custom else None)
            if not pages:
                last = "источники не нашлись"
                continue
            blob = "\n\n".join(f"### {p['title']} ({p['url']})\n{p['text']}" for p in pages)
            text = gemini(prompts.RESEARCH_FROM_TEXT_PROMPT.replace("<<FOCUS>>", label)
                          .replace("<<HISTORY>>", hist).replace("<<PEOPLE>>", people_txt)
                          .replace("<<SOURCES>>", blob), as_json=False)
            srcs = [{"title": p["title"], "url": p["url"]} for p in pages]
        last = text[:200]
        hero = (re.search(r"ГЕРОЙ:\s*(.+)", text) or [None, ""])[1]
        if not custom and hero and hero_seen(hero, people):
            log(f"герой уже был ({hero}), ищу другого")
            continue
        if "НЕТ ИСТОРИИ" not in text and "ФАКТЫ" in text and len(text) > 300:
            return text.strip(), srcs
    if custom:
        raise NoStory(f"Не нашёл надёжных источников по теме «{TOPIC[:80]}». Уточни имя или ник игрока и страну, "
                      "например: /тема Nobru Бразилия.")
    raise NoStory("За 6 попыток не нашлась проверяемая история. Запусти /make ещё раз "
                  "или задай тему сам: /тема имя или ник игрока.")


def build_script_prompt(facts, cast, writer="gemini"):
    import prompts
    heroes = "\n".join(f"- {c['name']}: {c['description']}" for c in cast.values()) or "- пока нет"
    tpl = prompts.SCRIPT_PROMPT_STORY if MODE == "story" else prompts.SCRIPT_PROMPT
    if writer == "claude" and MODE == "story":
        tpl = prompts.SCRIPT_PROMPT_CLAUDE
    return tpl.replace("<<FACTS>>", facts).replace("<<HEROES>>", heroes)


def cmd_create():
    WORK.mkdir(exist_ok=True)
    vid = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    try:
        refresh_stats()
        videos = load_videos()
        cast = load_cast()
        story_mode = MODE == "story"
        photo = MODE in ("photo", "story")
        facts, sources = "", []
        if OFFLINE:
            story = SAMPLE_STORY_SHORTS if story_mode else (SAMPLE_STORY_PHOTO if photo else SAMPLE_STORY)
        elif photo and (STORY_TYPE == "real" or SCRIPT_TEXT):
            if SCRIPT_TEXT:
                facts, sources = ("ГОТОВЫЙ СЦЕНАРИЙ АВТОРА. Озвучку бери из него почти дословно, ничего не добавляй от себя "
                                  "и не меняй факты: только разбей на короткие сцены и придумай картинки.\n\n"
                                  + SCRIPT_TEXT[:12000]), []
            else:
                facts, sources = research(videos)
            story = None
            if PUTER_TOKEN and SCRIPT_WRITER in ("auto", "claude") and story_mode and not SCRIPT_TEXT:
                try:
                    story = claude_json(build_script_prompt(facts, cast, "claude"))
                    log(f"сценарий написал Claude ({CLAUDE_MODEL})")
                except Exception as e:  # noqa
                    notify(f"ℹ️ Claude недоступен ({str(e)[:200]}). Сценарий пишет Gemini.")
            if story is None:
                story = gemini(build_script_prompt(facts, cast), models=GEMINI_SCRIPT_MODELS)
        else:
            story = gemini((story_prompt_photo if photo else story_prompt)(videos, cast))
        story = (validate_story_script(story) if story_mode else
                 validate_photo_story(story) if photo else validate_story(story))
        have_ref = {k for k, c in cast.items() if (c["dir"] / "ref.png").exists()}
        for c in story.get("cast") or []:
            ensure_character(c["name"], c["description"], cast, portrait=not photo,
                             ref=photo and CHAR_REF, face=story_mode and INSTANTID)
        if story_mode and not OFFLINE:
            rewrite_image_prompts(story, cast)
        if not photo:
            for c in list(cast.values()):
                ensure_character(c["name"], c["description"], cast, portrait=True)
        elif CHAR_REF:
            used = {n.strip().lower() for sc in story["scenes"] for n in re.findall(r"\[([^\]]+)\]", sc["image_prompt"])}
            for key in used:
                if key in cast:
                    ensure_character(cast[key]["name"], cast[key]["description"], cast, portrait=False, ref=True)

        parts, clips, audios, durs = [], [], [], []
        voices = make_all_voices(story["scenes"]) if story_mode else None
        last_img, img_fail, recent, photos_used = None, 0, [], 0
        for i, sc in enumerate(story["scenes"]):
            tag = f"s{i:02d}"
            log(f"сцена {i + 1}/{len(story['scenes'])}: {sc['narration']}")
            seed = random.Random(f"{vid}{i}").randint(1, 10_000_000)
            if story_mode:
                img = WORK / f"{tag}_img.png"
                refs = ref_urls(sc["image_prompt"], cast, have_ref) if (CHAR_REF and IMAGE_PROVIDER == "pollinations") else []
                extra = ", the characters look exactly like in the reference images" if refs else ""
                init_ref = None
                if CHAR_REF:
                    for nm in re.findall(r"\[([^\]]+)\]", sc["image_prompt"]):
                        c0 = cast.get(nm.strip().lower())
                        if c0 and (c0["dir"] / "ref.png").exists():
                            init_ref = c0["dir"] / "ref.png"
                            break
                face = None
                if INSTANTID:
                    for nm in re.findall(r"\[([^\]]+)\]", sc["image_prompt"]):
                        c0 = cast.get(nm.strip().lower())
                        if c0 and (c0["dir"] / "face.png").exists():
                            face = c0["dir"] / "face.png"
                            break
                used_photo = False
                if PHOTOS_ON and sc.get("photo_query") and photos_used < MAX_PHOTOS and not OFFLINE:
                    try:
                        credit = real_photo(sc, img)
                    except Exception as e:  # noqa
                        credit = None
                        log("реальное фото не вышло:", str(e)[:120])
                    if credit:
                        used_photo = True
                        photos_used += 1
                        sources.append(credit)
                        log(f"сцена {i + 1}: реальное фото ({sc['photo_query']})")
                base_prompt = expand_prompt(sc["image_prompt"], cast) + extra + ", no text, no letters, no watermark"
                try:
                    if not used_photo:
                        gen_image("vscene", base_prompt, img, seed, refs or None, face=face, init=init_ref,
                                  strength=min(0.95, IMG2IMG_STRENGTH + (-0.05, 0.0, 0.05)[i % 3]))
                        if is_repeat(img, recent):
                            log(f"кадр {i + 1} похож на предыдущие, рисую заново в другом ракурсе")
                            try:
                                gen_image("vscene", base_prompt + ", " + SHOTS[i % len(SHOTS)], img, seed + 7919,
                                          refs or None, face=face, init=init_ref, strength=0.93)
                            except RuntimeError:
                                pass        # оставляем первый вариант
                        prob = frame_problem(img)
                        if prob:
                            log(f"кадр {i + 1}: {prob}, рисую заново без устройств")
                            try:
                                gen_image("vscene", strip_devices(base_prompt) + ", wide cinematic shot of people and landscape",
                                          img, seed + 4241, refs or None, face=face, init=init_ref, strength=0.9)
                            except RuntimeError:
                                pass
                except RuntimeError as e:
                    if last_img is None or img_fail > max(4, int(len(story["scenes"]) * 0.4)):
                        raise
                    img_fail += 1
                    log(f"картинка сцены {i + 1} не получилась, беру предыдущую: {str(e)[:120]}")
                    shutil.copy(last_img, img)
                last_img = img
                try:
                    recent = (recent + [ahash(img)])[-6:]
                except Exception:  # noqa
                    pass
                if voices:
                    voice = voices[i]
                else:
                    voice = WORK / f"{tag}.mp3"
                    make_voice(sc["narration"], voice)
                d = round(max(1.4, duration(voice) + 0.06), 2)
                clips.append(render_clip(tag, sc, img, d, voice))
                audios.append(scene_audio(tag, sc, voice, d))
                durs.append(d)
                continue
            if photo:
                img = WORK / f"{tag}_img.png"
                refs = ref_urls(sc["image_prompt"], cast, have_ref) if (CHAR_REF and IMAGE_PROVIDER == "pollinations") else []
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
        if story_mode and img_fail:
            notify(f"⚠️ Не удалось нарисовать картинок: {img_fail}. Вместо них использованы соседние кадры.")
        if story_mode:
            assemble(clips, audios, durs, final)
        else:
            concat(parts, final)
    except NoStory as e:
        notify(f"🔎 {e}")
        return
    except Exception as e:
        notify(f"❌ Не удалось собрать ролик: {e}")
        raise

    v = {"id": vid, "created": now_iso(), "title": story["title"], "person": story.get("title_line", ""), "summary": story.get("summary", ""),
         "caption": story.get("caption", story["title"]), "scenes": len(story["scenes"]),
         "script": [s["narration"] for s in story["scenes"]], "status": "pending", "mode": MODE,
         "facts": facts[:6000], "sources": sources, "topic": TOPIC, "own_script": bool(SCRIPT_TEXT)}
    log(f"готово: {final} ({duration(final):.0f} с)")

    try:
        v["video_url"] = upload_release(vid, v["title"], final)
    except Exception as e:  # noqa
        log("не удалось сохранить полную версию:", e)
        notify(f"⚠️ Не смог сохранить полную версию ролика на GitHub ({str(e)[:160]}). "
               "Публикация в Instagram возможна только для файлов до 20 МБ.")
    if TG_TOKEN and TG_CHAT:
        with open(video_for_telegram(final), "rb") as f:
            msg = tg("sendVideo", files={"video": f}, chat_id=TG_CHAT, width=W, height=H,
                     supports_streaming="true",
                     caption=f"🎬 {v['title']}\n\n{v['caption']}"[:1000],
                     reply_markup={"inline_keyboard": [[
                         {"text": "✅ Опубликовать", "callback_data": f"pub:{vid}"},
                         {"text": "🗑 Отклонить", "callback_data": f"rej:{vid}"}]]})
        v["tg_file_id"] = msg["video"]["file_id"]
        facts_src = [x for x in sources if not str(x.get("title", "")).startswith("Фото:")]
        photo_src = [x for x in sources if str(x.get("title", "")).startswith("Фото:")]
        if facts_src:
            lines = "\n".join(f"- {x['title'] or 'источник'}: {x['url']}" for x in facts_src[:6])
            notify("Проверь факты перед публикацией. Источники:\n" + lines)
        if photo_src:
            lines = "\n".join(f"- {x['title'][6:]}: {x['url']}" for x in photo_src)
            notify("В ролике реальные фото. При публикации укажи авторов, если лицензия этого требует (CC BY):\n" + lines)
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
        "/make - сделать ролик сразу: бот сам ищет историю и пишет сценарий\n"
        "/make и ниже твой готовый сценарий - ролик по твоему тексту\n"
        "/тема имя или тема - ролик по твоей теме, например: /тема Nobru Бразилия\n"
        "/stats - статистика и вывод\n"
        "/status - сколько роликов ждёт решения\n"
        "/igtest - проверить подключение Instagram")


def dispatch_create(topic=None, script=None):
    repo, tok = ENV("GITHUB_REPOSITORY"), ENV("GITHUB_TOKEN")
    body = {"ref": ENV("GITHUB_REF_NAME") or "main"}
    inputs = {k: v for k, v in (("topic", topic), ("script", script)) if v}
    if inputs:
        body["inputs"] = inputs
    r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/create.yml/dispatches",
                      headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                      json=body, timeout=60)
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
        arg = (re.split(r"\s+", text, maxsplit=1) + [""])[1].strip()
        if not arg:
            dispatch_create()
            tg("sendMessage", chat_id=chat, text="Запустил сборку: бот сам найдёт историю. Ролик придёт через 10-20 минут.")
        elif len(arg) < 80:
            tg("sendMessage", chat_id=chat,
               text="Для /make с текстом нужен готовый сценарий (хотя бы пара предложений). "
                    "Если это тема или имя игрока, напиши так: /тема " + arg)
        else:
            dispatch_create(script=arg)
            tg("sendMessage", chat_id=chat, text=f"Запустил сборку по твоему сценарию ({len(arg)} символов). "
                                                 "Ролик придёт через 10-20 минут.")
    elif cmd in ("/тема", "/tema", "/topic"):
        arg = (re.split(r"\s+", text, maxsplit=1) + [""])[1].strip()
        if not arg:
            tg("sendMessage", chat_id=chat, text="Напиши тему после команды, например: /тема Nobru Бразилия")
        else:
            dispatch_create(topic=arg)
            tg("sendMessage", chat_id=chat, text=f"Запустил сборку по теме: {arg[:200]}\nРолик придёт через 10-20 минут.")
    elif cmd == "/stats":
        tg("sendMessage", chat_id=chat, text="Собираю статистику...")
        tg("sendMessage", chat_id=chat, text=stats_report()[:4000])
    elif cmd == "/igtest":
        tg("sendMessage", chat_id=chat, text="Проверяю Instagram...")
        tg("sendMessage", chat_id=chat, text=ig_diagnose()[:4000])
    elif cmd == "/status":
        vs = load_videos()
        pend = sum(1 for v in vs if v["status"] == "pending")
        pub = sum(1 for v in vs if v["status"] == "published")
        tg("sendMessage", chat_id=chat, text=f"Всего: {len(vs)}, ждут решения: {pend}, опубликовано: {pub}")


def tg_safe(method, **params):
    """Служебные вызовы (ответ на нажатие, смена кнопок) не должны ронять обработчик.
    Типичные безвредные ошибки: «query is too old» (бот опрашивает раз в минуту) и «message is not modified»."""
    try:
        return tg(method, **params)
    except Exception as e:  # noqa
        log(f"{method}: {str(e)[:160]}")
        return None


def handle_callback(cq):
    chat = str(cq["message"]["chat"]["id"])
    if str(TG_CHAT) != chat:
        return
    action, _, vid = cq["data"].partition(":")
    qid = cq["id"]
    mid = cq["message"]["message_id"]
    v = get_video(vid)
    buttons = {"inline_keyboard": [[{"text": "✅ Опубликовать", "callback_data": f"pub:{vid}"},
                                    {"text": "🗑 Отклонить", "callback_data": f"rej:{vid}"}]]}
    if not v or v["status"] != "pending":
        tg_safe("answerCallbackQuery", callback_query_id=qid, text="Уже обработано")
        tg_safe("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
        return
    if action == "rej":
        v["status"] = "rejected"
        save_video(v)
        tg_safe("answerCallbackQuery", callback_query_id=qid, text="Отклонено")
        tg_safe("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
        return
    # публикация: сразу помечаем, чтобы повторное нажатие не опубликовало дважды
    v["status"] = "publishing"
    save_video(v)
    tg_safe("answerCallbackQuery", callback_query_id=qid, text="Публикую...")
    tg_safe("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
    tg_safe("sendMessage", chat_id=chat, text=f"⏳ Публикую в Instagram: {v['title']}")
    try:
        if v.get("video_url"):
            data = requests.get(v["video_url"], timeout=900).content
        else:
            info = tg("getFile", file_id=v["tg_file_id"])
            data = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}", timeout=300).content
        publish_video(v, data)
        tg_safe("sendMessage", chat_id=chat, text=f"✅ Опубликовано в Instagram: {v['title']}")
    except Exception as e:
        v["status"] = "pending"          # можно нажать кнопку ещё раз
        save_video(v)
        tg_safe("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup=buttons)
        tg_safe("sendMessage", chat_id=chat, text=f"❌ Не получилось опубликовать: {e}"[:4000])


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
    {"create": cmd_create, "poll": cmd_poll, "stats": cmd_stats,
     "igtest": lambda: print(ig_diagnose())}.get(
        mode, lambda: sys.exit(__doc__))()
