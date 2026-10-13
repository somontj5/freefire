"""Монтаж 2.0: камера, переходы, комикс-эффекты, анимация текста, звук, вырезка героя.

Только ffmpeg, Pillow и numpy, всё бесплатно. main.py вызывает init() и дальше берёт функции отсюда.
Любая функция, которая может не получиться (вырезка героя, определение ритма), возвращает None,
а main.py тогда монтирует по-старому. Отключается переменной MONTAGE=0.
"""
import math
import pathlib
import random
import re
import subprocess

C = {}


def init(**kw):
    """W, H, FPS, OVERLAP, FONT, RED, WORK, text_w, log, run."""
    C.update(kw)


HIT = ("hit", "boom", "shot")


# ───────────────────────── план эффектов по сценам ─────────────────────────

def plan_all(scenes, seed):
    """Эффекты для каждой сцены. Детерминировано от seed, с лимитами на тяжёлые эффекты."""
    rnd = random.Random(seed)
    n = len(scenes)
    plans, splits, bursts = [], 0, 0
    for i, sc in enumerate(scenes):
        sfx = sc.get("sfx", "none")
        r = random.Random(f"{seed}{i}")
        p = {"cam": r.choice(["in", "in", "out", "pan_l", "pan_r"]), "roll": 0.0, "shake": False, "rack": False,
             "speed": False, "halftone": False, "burst": None, "split": False, "parallax": False,
             "tail": False, "head": False}
        if r.random() < 0.4:
            p["roll"] = r.choice([-1, 1]) * r.uniform(0.8, 1.6)
        if sfx in HIT:
            p["shake"], p["speed"] = True, True
            acc = [a for a in (sc.get("accent") or []) if a]
            if acc and bursts < 3 and not sc.get("card"):      # на карточке «взрыв» закрыл бы текст
                p["burst"] = acc[0].upper()[:12]
                bursts += 1
        elif r.random() < 0.08:
            p["speed"] = True
        p["rack"] = (i == 0) or r.random() < 0.2
        p["halftone"] = (bool(sc.get("flashback")) or r.random() < 0.12) and not C.get("PIXEL")
        text = str(sc.get("narration", "")).lower()
        if (i > 0 and splits < 2 and not sc.get("card") and not p["burst"]
                and re.search(r"(^|\s)(но|зато|вместо|а теперь|а сейчас|раньше|потом)(\s|,)", text) and r.random() < 0.7):
            p["split"] = True
            splits += 1
        plans.append(p)
    return plans


def plan_transitions(plans, scenes, seed):
    """transitions[k] - переход В сцену k (k от 1). Элемент: {"kind", "xfade", "sound"}."""
    rnd = random.Random(f"{seed}tr")
    pool = ["swipe", "swipe", "zoomblur", "zoomblur", "torn", "slide", "wipe", "fade"]
    out, prev = [None], None
    for k in range(1, len(scenes)):
        a, b = scenes[k - 1], scenes[k]
        sfx = b.get("sfx", "none")
        if sfx in HIT or plans[k]["burst"]:
            t = {"kind": "flash", "xfade": "fadewhite", "sound": "impact"}
        elif bool(a.get("flashback")) != bool(b.get("flashback")):
            t = {"kind": "grays", "xfade": "fadegrays", "sound": "whoosh"}
        elif plans[k]["split"] or plans[k - 1]["split"]:
            t = {"kind": "fade", "xfade": "fade", "sound": None}
        else:
            kind = rnd.choice(pool)
            while kind == prev:
                kind = rnd.choice(pool)
            if kind == "swipe":
                t = {"kind": kind, "xfade": rnd.choice(["slideleft", "slideright", "smoothleft", "smoothright"]),
                     "sound": "whoosh"}
            elif kind == "slide":
                t = {"kind": kind, "xfade": rnd.choice(["slideup", "slidedown", "coverleft", "coverright"]),
                     "sound": "whoosh"}
            elif kind == "wipe":
                t = {"kind": kind, "xfade": rnd.choice(["wipeleft", "wiperight", "circleopen", "diagtl"]),
                     "sound": "whoosh"}
            elif kind == "torn":
                t = {"kind": kind, "xfade": "custom", "sound": "whoosh"}
            elif kind == "zoomblur":
                t = {"kind": kind, "xfade": "fade", "sound": "whoosh"}
            else:
                t = {"kind": "fade", "xfade": "fade", "sound": None}
        prev = t["kind"]
        out.append(t)
    for k in range(1, len(out)):          # зум-размытие: конец прошлой сцены и начало этой готовятся заранее
        if out[k]["kind"] == "zoomblur":
            plans[k - 1]["tail"] = True
            plans[k]["head"] = True
    return out


def xfade_filter(t, dur, offset):
    """Строка фильтра xfade. Рваная бумага: неровная кромка с белой полоской бумаги."""
    if t["kind"] == "torn":
        ub = "(1.08*P-0.04+0.025*sin(Y/H*55)+0.014*sin(Y/H*190+1)+0.008*sin(Y/H*430))"
        u = "(X/W)"
        paper = "if(eq(PLANE,0),238,128)"
        expr = f"if(lt({u},{ub}-0.016),A,if(lt({u},{ub}),{paper},B))"
        return f"xfade=transition=custom:duration={dur}:offset={offset:.3f}:expr='{expr}'"
    return f"xfade=transition={t['xfade']}:duration={dur}:offset={offset:.3f}"


# ───────────────────────── сборка графа ffmpeg ─────────────────────────

class Chain:
    """Помощник: собирает filter_complex с автоматическими метками и входами."""

    def __init__(self):
        self.args, self.parts, self.n_in, self.n = [], [], 0, 0

    def add_input(self, *args):
        self.args += list(args)
        self.n_in += 1
        return self.n_in - 1

    def label(self):
        self.n += 1
        return f"x{self.n}"

    def f(self, src, expr, out=None):
        """[src]expr[out]; src - метка или 'N:v'."""
        out = out or self.label()
        srcs = src if isinstance(src, (list, tuple)) else [src]
        self.parts.append("".join(f"[{s}]" for s in srcs) + expr + f"[{out}]")
        return out

    def render(self):
        return ";".join(self.parts)


def _cam(cam, frames, layer=None):
    """Выражения z, x, y для zoompan. layer: None (обычный кадр), 'bg' (фон, двигается меньше) или 'fg' (герой, больше)."""
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if cam in ("in", "out"):
        s = {None: 0.10, "bg": 0.045, "fg": 0.15}[layer]
        z = f"1+{s}*on/{frames}" if cam == "in" else f"{1 + s}-{s}*on/{frames}"
        return z, cx, cy
    z = {None: 1.08, "bg": 1.05, "fg": 1.14}[layer]
    amp = {None: 0.7, "bg": 0.35, "fg": 1.0}[layer]
    hi, lo = 0.5 + amp / 2, 0.5 - amp / 2
    if cam == "pan_l":
        return f"{z}", f"(iw-iw/zoom)*({hi:.3f}-{amp}*on/{frames})", cy
    return f"{z}", f"(iw-iw/zoom)*({lo:.3f}+{amp}*on/{frames})", cy


def _sc(w, h):
    """scale; в пиксель-арте ближайший сосед, чтобы квадраты оставались резкими при увеличении."""
    return f"scale={w}:{h}" + (":flags=neighbor" if C.get("PIXEL") else "")


def base_still(ch, idx, plan, frames):
    W, H, FPS = C["W"], C["H"], C["FPS"]
    z, x, y = _cam(plan["cam"], frames)
    return ch.f(f"{idx}:v", f"{_sc(2160, 3840)},zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS}")


def base_parallax(ch, bg_i, fg_i, mask_i, plan, frames):
    """Фон и вырезанный герой двигаются с разной скоростью: герой ближе, фон дальше."""
    W, H, FPS = C["W"], C["H"], C["FPS"]
    zb, xb, yb = _cam(plan["cam"], frames, "bg")
    zf, xf, yf = _cam(plan["cam"], frames, "fg")
    bg = ch.f(f"{bg_i}:v", f"scale=2160:3840,zoompan=z='{zb}':x='{xb}':y='{yb}':d={frames}:s={W}x{H}:fps={FPS}")
    fg = ch.f(f"{fg_i}:v", f"scale=2160:3840,zoompan=z='{zf}':x='{xf}':y='{yf}':d={frames}:s={W}x{H}:fps={FPS}")
    mk = ch.f(f"{mask_i}:v", f"scale=2160:3840,zoompan=z='{zf}':x='{xf}':y='{yf}':d={frames}:s={W}x{H}:fps={FPS},format=gray")
    fa = ch.f([fg, mk], "alphamerge")
    return ch.f([bg, fa], "overlay=format=auto")


def base_split(ch, top_i, bot_i, gutter_i, frames):
    """Комикс-кадр из двух панелей: сверху прошлая сцена, снизу текущая, между ними белая рамка."""
    W, FPS = C["W"], C["FPS"]
    ph = (C["H"] - 20) // 2

    def panel(i, z):
        return ch.f(f"{i}:v", f"{_sc(2160, 3840)},crop=2160:{ph * 2}:0:{(3840 - ph * 2) // 2},"
                              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{ph}:fps={FPS}")
    top = panel(top_i, f"1.10-0.08*on/{frames}")
    bot = panel(bot_i, f"1+0.08*on/{frames}")
    g = ch.f(f"{gutter_i}:v", f"fps={FPS}")
    v = ch.f([top, g, bot], "vstack=inputs=3")
    return ch.f(v, f"drawbox=x=0:y=0:w={W}:h={ph}:color=black:t=8,drawbox=x=0:y={ph + 20}:w={W}:h={ph}:color=black:t=8")


def camera_extras(ch, src, plan, total):
    """Поворот, тряска, расфокус и зум-размытие поверх готового кадра."""
    W, H = C["W"], C["H"]
    if plan.get("roll") or plan.get("shake"):
        sw, sh = int(W * 1.16) // 2 * 2, int(H * 1.16) // 2 * 2
        r = plan.get("roll", 0.0)
        ang = f"{r:.3f}*(2*t/{total:.2f}-1)*PI/180" if r else "0"
        shx = "22*sin(t*47)*exp(-t*5.5)" if plan.get("shake") else "0"
        shy = "18*sin(t*61+1)*exp(-t*5.5)" if plan.get("shake") else "0"
        src = ch.f(src, f"scale={sw}:{sh},rotate=a='{ang}':ow={sw}:oh={sh}:c=black,"
                        f"crop={W}:{H}:x='(iw-{W})/2+{shx}':y='(ih-{H})/2+{shy}'")
    head, tail, rack = plan.get("head"), plan.get("tail"), plan.get("rack")
    if head or tail or rack:
        zoom_terms, blur_terms = [], []
        if head:
            zoom_terms.append("0.35*pow(max(0,1-t/0.25),2)")
            blur_terms.append("max(0,1-T/0.25)")           # в blend время называется T, в scale и crop - t
        elif rack:
            blur_terms.append("max(0,1-T/0.55)")
        if tail:
            zoom_terms.append(f"0.35*pow(max(0,(t-{total - 0.25:.2f})/0.25),2)")
            blur_terms.append(f"max(0,(T-{total - 0.25:.2f})/0.25)")
        if zoom_terms:
            z = "+".join(zoom_terms)
            src = ch.f(src, f"scale=w='trunc(iw*(1+{z})/2)*2':h='trunc(ih*(1+{z})/2)*2':eval=frame,crop={W}:{H}")
        s1, s2 = ch.label(), ch.label()
        ch.parts.append(f"[{src}]split=2[{s1}][{s2}]")
        bl = ch.f(s2, "gblur=sigma=20")
        o = "min(1," + "+".join(blur_terms) + ")"
        src = ch.f([s1, bl], f"blend=all_expr='A*(1-{o})+B*{o}'")
    return src


# ───────────────────────── комикс-эффекты (PNG для наложения) ─────────────────────────

def _font(size):
    from PIL import ImageFont
    return ImageFont.truetype(C["FONT"], int(size))


def make_speed_lines(path, seed):
    from PIL import Image, ImageDraw
    W, H = C["W"], C["H"]
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = random.Random(seed)
    cx, cy, R = W / 2, H * 0.45, math.hypot(W, H) * 0.62
    for _ in range(120):
        a = r.uniform(0, 2 * math.pi)
        w = r.uniform(0.004, 0.017)
        r0 = R * r.uniform(0.34, 0.62)
        pts = [(cx + r0 * math.cos(a - w * .25), cy + r0 * math.sin(a - w * .25)),
               (cx + R * math.cos(a - w), cy + R * math.sin(a - w)),
               (cx + R * math.cos(a + w), cy + R * math.sin(a + w)),
               (cx + r0 * math.cos(a + w * .25), cy + r0 * math.sin(a + w * .25))]
        d.polygon(pts, fill=(255, 255, 255, r.randint(120, 215)))
    im.save(path)


def make_halftone(path):
    from PIL import Image, ImageDraw
    W, H = C["W"], C["H"]
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    step = 30
    cx, cy = W / 2, H / 2
    maxd = math.hypot(cx, cy)
    for row, y in enumerate(range(0, H + step, step)):
        for x in range(-(step // 2) * (row % 2), W + step, step):
            k = math.hypot(x - cx, y - cy) / maxd
            if k < 0.6:                                                  # центр кадра (лица) без точек
                continue
            r = step * 0.42 * min(1.0, (k - 0.6) / 0.4) ** 1.3          # к краям точки крупнее
            if r > 1.2:
                d.ellipse((x - r, y - r, x + r, y + r), fill=(0, 0, 0, 85))
    im.save(path)


def make_burst(path, word):
    """Комикс-«взрыв»: звезда с обводкой и ключевым словом."""
    from PIL import Image, ImageDraw
    S = 720
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = random.Random(word)
    spikes = 16
    pts = []
    for k in range(spikes * 2):
        ang = math.pi * k / spikes - math.pi / 2
        rad = S * (0.48 if k % 2 == 0 else 0.33) * (1 + r.uniform(-0.06, 0.06))
        pts.append((S / 2 + rad * math.cos(ang), S / 2 + rad * math.sin(ang)))
    d.polygon(pts, fill=(255, 214, 0, 255), outline=(0, 0, 0, 255), width=14)
    inner = [(S / 2 + (x - S / 2) * 0.72, S / 2 + (y - S / 2) * 0.72) for x, y in pts]
    d.polygon(inner, fill=(224, 16, 26, 255))
    size = 120
    f = _font(size)
    while f.getlength(word) > S * 0.62 and size > 40:
        size -= 6
        f = _font(size)
    d.text((S / 2, S / 2), word, font=f, fill=(255, 255, 255, 255), anchor="mm", stroke_width=8, stroke_fill=(0, 0, 0, 255))
    im.save(path)


def add_overlays(ch, src, plan, total, tag):
    """Накладывает растр, линии скорости и «взрыв» на кадр. Файлы PNG кладутся в WORK."""
    WORK = C["WORK"]
    t = f"{total:.2f}"

    def png(p):
        return ch.add_input("-loop", "1", "-framerate", str(C["FPS"]), "-t", t, "-i", str(p))

    if plan.get("halftone"):
        p = WORK / "fx_halftone.png"
        if not p.exists():
            make_halftone(p)
        src = ch.f([src, f"{png(p)}:v"], "overlay=0:0:format=auto")
    if plan.get("speed"):
        for v in range(2):
            p = WORK / f"fx_speed{v}.png"
            if not p.exists():
                make_speed_lines(p, 100 + v)
            en = f"lt(t,1.5)*{'lt' if v == 0 else 'gte'}(mod(t,0.16),0.08)"
            src = ch.f([src, f"{png(p)}:v"], f"overlay=0:0:format=auto:enable='{en}'")
    if plan.get("burst"):
        p = WORK / f"{tag}_burst.png"
        make_burst(p, plan["burst"])
        sc = ch.f(f"{png(p)}:v", "format=rgba,scale=w='max(2,trunc(720*(0.35+0.65*min(1,t/0.16))/2)*2)':h=-2:eval=frame")
        src = ch.f([src, sc], f"overlay=x='(W-w)/2':y='H*0.30-h/2':format=auto:enable='between(t,0.04,0.72)'")
    return src


# ───────────────────────── анимация текста карточек ─────────────────────────

def _fmt_num(v, decimals):
    s = f"{v:,.{decimals}f}".replace(",", " ")
    return s.replace(".", ",")


def _find_counter(line):
    """Число в строке карточки, которое стоит «накручивать». Годы не трогаем."""
    m = re.search(r"\d[\d ]*\d(?:[.,]\d+)?|\d(?:[.,]\d+)?", line)
    if not m:
        return None
    raw = m.group(0)
    dec = len(re.split(r"[.,]", raw)[1]) if re.search(r"[.,]\d", raw) else 0
    try:
        val = float(raw.replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    if 1900 <= val <= 2100 and dec == 0:
        return None
    if val < 10 or val >= 1e12:
        return None
    return m.start(), m.end(), val, dec


def _dt(txtfile, size, color, x, y, enable, alpha=None, border=6):
    a = f":alpha='{alpha}'" if alpha else ""
    return (f"drawtext=fontfile={C['FONT']}:textfile={txtfile}:expansion=none:fontsize={size}:fontcolor={color}:"
            f"borderw={border}:bordercolor=black@0.85:shadowx=2:shadowy=3:shadowcolor=black@0.55:"
            f"x='{x}':y='{y}':enable='{enable}'{a}")


def card_filters(layout, tag, kind):
    """layout: [(текст, размер, красная?)]. kind: 'title' (первая строка набирается с курсором),
    'plain' (строки плавно выезжают). Строки с числом (не годом) считают число на глазах."""
    W, H, WORK, text_w = C["W"], C["H"], C["WORK"], C["text_w"]
    out, y = [], int(H * 0.09)
    t_line = 0.0
    for i, (line, size, is_red) in enumerate(layout):
        color = C["RED"] if is_red else "white"
        base_t = t_line
        counter = _find_counter(line)
        if kind == "title" and i == 0 and not counter:
            x0 = int((W - text_w(line, size)) / 2)
            step, per = 2, 0.055
            n_steps = (len(line) + step - 1) // step
            for k in range(1, n_steps + 1):
                pre = line[:k * step]
                f = WORK / f"{tag}_c{i}_t{k}.txt"
                f.write_text(pre, encoding="utf-8")
                a = base_t + (k - 1) * per
                b = base_t + k * per if k < n_steps else 999
                out.append(_dt(f, size, color, x0, y, f"between(t,{a:.3f},{b:.3f})"))
                if k < n_steps:                                   # курсор идёт за последней буквой
                    c = WORK / "fx_cursor.txt"
                    c.write_text("|", encoding="utf-8")
                    out.append(_dt(c, size, "white", x0 + int(text_w(pre, size)) + 2, y,
                                   f"between(t,{a:.3f},{b:.3f})"))
            end = base_t + n_steps * per
            c = WORK / "fx_cursor.txt"
            c.write_text("|", encoding="utf-8")
            out.append(_dt(c, size, "white", x0 + int(text_w(line, size)) + 2, y,
                           f"between(t,{end:.3f},{end + 0.7:.3f})*lt(mod(t,0.34),0.17)"))
            t_line = end + 0.08
        elif counter:
            s0, s1, val, dec = counter
            steps, dur = 14, 0.95
            for k in range(steps + 1):
                e = 1 - (1 - k / steps) ** 3                       # быстро в начале, плавно к концу
                cur = _fmt_num(val * e, dec)
                txt = line[:s0] + cur + line[s1:]
                f = WORK / f"{tag}_c{i}_n{k}.txt"
                f.write_text(txt, encoding="utf-8")
                x = int((W - text_w(txt, size)) / 2)
                a = base_t + k * dur / steps
                b = base_t + (k + 1) * dur / steps if k < steps else 999
                out.append(_dt(f, size, color, x, y, f"between(t,{a:.3f},{b:.3f})"))
            t_line = base_t + 0.15
        else:
            f = WORK / f"{tag}_c{i}.txt"
            f.write_text(line, encoding="utf-8")
            d0 = base_t
            out.append(_dt(f, size, color, "(w-text_w)/2",
                           f"{y}-90*pow(max(0,1-(t-{d0:.2f})/0.42),3)", f"gte(t,{d0:.2f})",
                           alpha=f"min(1,max(0,(t-{d0:.2f})/0.22))"))
            t_line = base_t + 0.14
        y += int(size * 1.22)
    return out


# ───────────────────────── звук ─────────────────────────

def _ff(*args):
    C["run"](["ffmpeg", "-y", "-loglevel", "error", *args])


def make_samples(work):
    """Короткие звуки, синтезируются ffmpeg: свист, удар, нарастание."""
    paths = {}
    p = work / "fx_whoosh.wav"
    if not p.exists():
        _ff("-f", "lavfi", "-i", "anoisesrc=d=0.6:c=pink:r=44100:a=0.7", "-af",
            "highpass=f=450,lowpass=f=6500,afade=t=in:d=0.24,afade=t=out:st=0.24:d=0.36,aformat=channel_layouts=stereo", str(p))
    paths["whoosh"] = p
    p = work / "fx_impact.wav"
    if not p.exists():
        _ff("-f", "lavfi", "-i",
            "aevalsrc='0.95*sin(2*PI*(46+110*exp(-16*t))*t)*exp(-5.5*t)+0.35*(random(0)-0.5)*exp(-38*t)':d=0.8:s=44100",
            "-af", "aformat=channel_layouts=stereo", str(p))
    paths["impact"] = p
    p = work / "fx_riser.wav"
    if not p.exists():
        _ff("-f", "lavfi", "-i",
            "aevalsrc='0.30*sin(2*PI*(170*t+250*t*t))*pow(t/1.2,2)+0.22*(random(0)-0.5)*pow(t/1.2,3)':d=1.2:s=44100",
            "-af", "lowpass=f=7500,afade=t=out:st=1.14:d=0.06,aformat=channel_layouts=stereo", str(p))
    paths["riser"] = p
    return paths


def build_sfx_track(events, total, work, out):
    """events: [(время_сек, 'whoosh'|'impact'|'riser', громкость)] -> один wav нужной длины."""
    if not events:
        return None
    paths = make_samples(work)
    cmd, fc, labels = [], [], []
    for kind in ("whoosh", "impact", "riser"):
        cmd += ["-i", str(paths[kind])]
    use = {"whoosh": 0, "impact": 1, "riser": 2}
    counts = {k: sum(1 for e in events if e[1] == k) for k in use}
    for kind, idx in use.items():
        if counts[kind] > 1:
            outs = "".join(f"[{kind}{j}]" for j in range(counts[kind]))
            fc.append(f"[{idx}:a]asplit={counts[kind]}{outs}")
        elif counts[kind] == 1:
            fc.append(f"[{idx}:a]anull[{kind}0]")
    seen = {k: 0 for k in use}
    for n, (t, kind, vol) in enumerate(events):
        j = seen[kind]
        seen[kind] += 1
        ms = max(0, int(t * 1000))
        fc.append(f"[{kind}{j}]adelay={ms}|{ms},volume={vol}[e{n}]")
        labels.append(f"[e{n}]")
    fc.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
              f"apad=whole_dur={total:.2f},atrim=0:{total:.2f}[a]")
    _ff(*cmd, "-filter_complex", ";".join(fc), "-map", "[a]", "-ar", "44100", "-ac", "2", str(out))
    return out


def sound_events(durs, scenes, plans, trans, mcuts=None):
    """Свисты на переходах, удары на вспышках, нарастание перед важным моментом."""
    OV = C["OVERLAP"]
    ev, t, risers = [], 0.0, 0
    cuts = []
    for k in range(len(durs) - 1):
        t += durs[k]
        cuts.append(t)                              # здесь начинается переход в сцену k+1
    for k in range(1, len(durs)):
        tr, tt = trans[k], cuts[k - 1]
        if tr and tr["sound"] == "whoosh":
            ev.append((max(0.0, tt - 0.12), "whoosh", 0.42))
        elif tr and tr["sound"] == "impact":
            ev.append((max(0.0, tt + OV / 2 - 0.02), "impact", 0.9))
            if risers < 3 and tt > 1.6:
                ev.append((max(0.0, tt + OV / 2 - 1.2), "riser", 0.55))
                risers += 1
    for t, style in (mcuts or []):             # смена музыки на эпичную или динамичную: нарастание и удар
        if style == "hit":
            ev.append((max(0.0, t + OV / 2 - 0.02), "impact", 0.9))
            if t > 1.4:
                ev.append((max(0.0, t + OV / 2 - 1.2), "riser", 0.55))
    ev.sort()
    out, last = [], {}
    for e in ev:                                # два одинаковых звука рядом не нужны
        if e[0] - last.get(e[1], -9) < 0.6:
            continue
        last[e[1]] = e[0]
        out.append(e)
    return out


# ───────────────────────── музыка и ритм ─────────────────────────

SYNTH_BPM = 96


def synth_music(total, work):
    """Спокойный фон с мягким пульсом: темп известен заранее, поэтому по нему можно резать кадры."""
    p = work / "fx_music.wav"
    per = 60.0 / SYNTH_BPM
    pad = ("(0.30*sin(2*PI*110*t)+0.22*sin(2*PI*164.8*t)+0.18*sin(2*PI*220.5*t)+0.12*sin(2*PI*329.6*t))"
           "*(0.75+0.25*sin(2*PI*0.12*t))")
    kick = f"0.55*sin(2*PI*(48+70*exp(-30*mod(t,{per:.4f})))*mod(t,{per:.4f}))*exp(-9*mod(t,{per:.4f}))"
    hat = f"0.10*(random(0)-0.5)*exp(-70*mod(t+{per / 2:.4f},{per:.4f}))"
    _ff("-f", "lavfi", "-i", f"aevalsrc='{pad}*0.9+{kick}+{hat}':s=44100:c=stereo:d={total:.1f}", str(p))
    return p, SYNTH_BPM, 0.0


def detect_bpm(mp3, seconds=60):
    """Темп и сдвиг первой доли по огибающей громкости (numpy, без librosa). None, если неуверенно."""
    import numpy as np
    try:
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(mp3), "-t", str(seconds), "-ac", "1", "-ar",
                              "11025", "-f", "s16le", "-"], capture_output=True, check=True).stdout
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        if len(x) < 11025 * 10:
            return None
        hop = 110                                     # 10 мс
        n = len(x) // hop
        env = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
        on = np.maximum(0, np.diff(env))
        on = on - on.mean()
        ac = np.correlate(on, on, "full")[len(on) - 1:]
        best, best_score = None, 0.0
        for bpm in np.arange(70.0, 160.01, 0.25):
            lag = 6000.0 / bpm                        # в десятках миллисекунд
            lo, hi = int(lag), int(lag) + 1
            if hi >= len(ac):
                continue
            score = ac[lo] * (hi - lag) + ac[hi] * (lag - lo)
            if score > best_score:
                best, best_score = bpm, score
        if best is None or best_score < 0.15 * ac[0]:
            return None
        period = 6000.0 / best
        head = on[:3000]                                 # фазу ищем по первым 30 секундам: дальше набегает ошибка темпа
        phases = []
        for ph in range(int(period)):
            idx = (np.arange(0, (len(head) - ph) / period) * period + ph).astype(int)
            phases.append(float(head[idx[idx < len(head)]].sum()))
        offset = int(np.argmax(phases)) / 100.0
        return round(float(best), 1), offset
    except Exception as e:  # noqa
        C["log"]("ритм музыки не определён:", str(e)[:100])
        return None


def snap_duration(d, cum, bpm, offset, max_ext=0.3):
    """Продлевает сцену на долю секунды (тишиной после фразы), чтобы смена кадра пришлась на долю такта."""
    grid = 60.0 / bpm
    while grid < 0.28:
        grid *= 2
    while grid > 0.62:
        grid /= 2
    target = cum + d + C["OVERLAP"] / 2
    k = math.ceil((target - offset) / grid - 1e-6)
    ext = offset + k * grid - target
    return d + (ext if 0 <= ext <= max_ext else 0.0)


# ───────────────────────── вырезка героя из готового кадра ─────────────────────────

_sess = {"s": None, "dead": False}


def make_cutout(img, tag, work, lo=0.08, hi=0.62):
    """Герой отдельно от фона без отдельной генерации: rembg вырезает его из готового кадра.
    Возвращает {"bg", "fg", "mask"} или None, если вырезка не вышла или это не «герой на фоне»."""
    if _sess["dead"]:
        return None
    try:
        import io
        import numpy as np
        from PIL import Image, ImageFilter
        from rembg import new_session, remove
        if _sess["s"] is None:
            _sess["s"] = new_session("u2net")
        im = Image.open(img).convert("RGB")
        cut = Image.open(io.BytesIO(remove(img.read_bytes(), session=_sess["s"]))).convert("RGBA")
        return _cutout_from_alpha(im, cut.getchannel("A"), tag, work, lo, hi)
    except Exception as e:  # noqa
        _sess["dead"] = True
        C["log"]("вырезка героя недоступна, камера без параллакса:", str(e)[:120])
        return None


def _cutout_from_alpha(im, alpha, tag, work, lo=0.08, hi=0.62):
    import numpy as np
    from PIL import Image, ImageFilter
    a = np.asarray(alpha, dtype=np.uint8)
    cover = float((a > 128).mean())
    if not lo <= cover <= hi:
        return None
    ys, xs = np.where(a > 128)
    if (ys.max() - ys.min()) < 0.35 * a.shape[0]:          # маленькое пятно: это не герой
        return None
    mask = alpha.filter(ImageFilter.GaussianBlur(1.6))
    # фон под героем заполняем размытием окружения, чтобы вокруг движущегося героя не было «призрака»
    grow = alpha.point(lambda v: 255 if v > 40 else 0).filter(ImageFilter.MaxFilter(31)).filter(ImageFilter.GaussianBlur(9))
    soft = im.filter(ImageFilter.GaussianBlur(38))
    bg = Image.composite(soft, im, grow).filter(ImageFilter.GaussianBlur(1.2))
    paths = {"bg": work / f"{tag}_bg.png", "fg": work / f"{tag}_fg.png", "mask": work / f"{tag}_mask.png"}
    bg.save(paths["bg"])
    im.save(paths["fg"])
    mask.save(paths["mask"])
    return paths


# ───────────────────────── музыка по настроению ─────────────────────────
# Шесть настроений. Для каждого берётся ваш трек из music/<настроение>/ (mp3, wav, ogg, m4a) или синтезированный
# фон (все синтезы в темпе 96 уд/мин, 16 тактов = 40 секунд, поэтому петли стыкуются без шва и ритм общий).
MOODS = ["calm", "tense", "sad", "rise", "epic", "action"]
MOOD_RU = {"calm": "спокойная", "tense": "тревожная", "sad": "грустная", "rise": "нарастающая",
           "epic": "эпичная", "action": "динамичная"}
MOOD_KEYS = {
    "epic": r"чемпион|побед|выигра|финал|кубок|трофе|титул|миллион|рекорд|мирово|золот|триумф|легенд|вершин|первое место|лучш",
    "action": r"турнир|матч|бой|сраж|стрел|катк|раунд|атак|десант|выстрел|перестрел|вышл\w* на сцен",
    "tense": r"страх|боял|угроз|опасн|проигр|пораж|провал|сомнева|не верил|давлен|критик|тревог|тёмн|темн|ставк|"
             r"последн\w+ шанс|потерял|исключ|выгнал|отказал|трудн|сложн",
    "sad": r"умер|смерт|слёз|слез|расстал|бедн|нищ|болезн|одинок|плакал|голод|тяжёл|тяжел",
    "rise": r"решил|начал|мечт|тренир|упор|шаг за шагом|набрал|подписчик|аудитор|первый|первые|поверил|цель|"
            r"получилось|раскрутил|заметили|приглас|рос\b|расти|выросл",
    "calm": r"родил|детств|семь|дом|школ|обычн|работал|жил|вырос в|в 19\d\d|в 20\d\d",
}
_MBPM, _MPER = 96.0, 60.0 / 96.0
_MBAR, _MLOOP = 4 * _MPER, 16 * 4 * _MPER


def _pick(idx, vals):
    s = str(vals[-1])
    for k in range(len(vals) - 2, -1, -1):
        s = f"if(eq({idx},{k}),{vals[k]},{s})"
    return s


def mood_expr(mood):
    """Выражение для aevalsrc: одна дорожка настроения на 4 такта в петле."""
    BI = "mod(floor(t/2.5),4)"
    BP, BT, E8, EI = "mod(t,2.5)", "mod(t,0.625)", "mod(t,0.3125)", "floor(mod(t,2.5)/0.3125)"
    env = f"min(1,{BP}/0.35)*min(1,(2.5-{BP})/0.3)"

    def pad(chords, g):
        parts = []
        for tone in range(3):
            f = _pick(BI, [c[tone] for c in chords])
            parts.append(f"{g}*(sin(2*PI*({f})*t)+0.4*sin(2*PI*2*({f})*t)+0.15*sin(2*PI*1.003*({f})*t))")
        return "(" + "+".join(parts) + f")*{env}"

    def arp(pats, g, dec=6):
        f = _pick(BI, [_pick(EI, p) for p in pats])
        return f"{g}*sin(2*PI*({f})*t)*exp(-{dec}*{E8})*(1-exp(-300*{E8}))"

    kick = lambda g, d=10: f"{g}*sin(2*PI*(48+60*exp(-30*{BT}))*{BT})*exp(-{d}*{BT})"
    hat = lambda g: f"{g}*(random(0)-0.5)*exp(-80*mod(t+0.15625,0.3125))"
    if mood == "calm":
        ch = [[261.63, 329.63, 392.0], [220.0, 261.63, 329.63], [220.0, 261.63, 349.23], [196.0, 246.94, 293.66]]
        pats = [[261.63, 329.63, 392.0, 329.63, 523.25, 392.0, 329.63, 392.0],
                [220.0, 261.63, 329.63, 261.63, 440.0, 329.63, 261.63, 329.63],
                [220.0, 261.63, 349.23, 261.63, 440.0, 349.23, 261.63, 349.23],
                [196.0, 246.94, 293.66, 246.94, 392.0, 293.66, 246.94, 293.66]]
        return f"{pad(ch, 0.16)}+{arp(pats, 0.20)}"
    if mood == "sad":
        ch = [[110.0, 220.0, 261.63], [87.31, 174.61, 220.0], [98.0, 196.0, 261.63], [98.0, 196.0, 246.94]]
        lead = _pick(BI, [329.63, 349.23, 392.0, 293.66])
        slow = "mod(t,1.25)"
        return (f"{pad(ch, 0.17)}+0.14*sin(2*PI*({lead})*t+2.5*sin(2*PI*5*t))*{env}"
                f"+0.20*sin(2*PI*2*({_pick(BI, [220.0, 174.61, 196.0, 196.0])})*t)*exp(-3*{slow})*(1-exp(-200*{slow}))")
    if mood == "tense":
        beat2 = "mod(t,1.25)"
        return ("0.30*sin(2*PI*55*t)+0.22*sin(2*PI*58.27*t)+0.16*sin(2*PI*77.78*t)"
                f"+0.55*sin(2*PI*(42+40*exp(-25*{beat2}))*{beat2})*exp(-9*{beat2})"
                f"+0.30*sin(2*PI*(42+40*exp(-25*mod(t+0.95,1.25)))*mod(t+0.95,1.25))*exp(-9*mod(t+0.95,1.25))"
                "+0.05*sin(2*PI*1480*t+6*sin(2*PI*0.7*t))*(0.5+0.5*sin(2*PI*t/10))"
                f"+0.14*(random(0)-0.5)*pow({BP}/2.5,3)")
    if mood == "rise":
        ch = [[130.81, 261.63, 329.63], [98.0, 196.0, 246.94], [110.0, 220.0, 261.63], [87.31, 174.61, 220.0]]
        pats = [[261.63, 329.63, 392.0, 523.25, 659.25, 784.0, 1046.5, 784.0],
                [196.0, 246.94, 293.66, 392.0, 493.88, 587.33, 784.0, 587.33],
                [220.0, 261.63, 329.63, 440.0, 523.25, 659.25, 880.0, 659.25],
                [174.61, 220.0, 261.63, 349.23, 440.0, 523.25, 698.46, 523.25]]
        return (f"{pad(ch, 0.14)}+{arp(pats, 0.20, 5)}+{kick(0.35)}+{hat(0.05)}"
                f"+0.05*sin(2*PI*(300*mod(t,40)+18.75*mod(t,40)*mod(t,40)))*pow(mod(t,40)/40,2)")
    if mood == "epic":
        roots = [[110.0, 164.81, 220.0], [87.31, 130.81, 174.61], [130.81, 196.0, 261.63], [98.0, 146.83, 196.0]]
        parts = []
        for tone in range(3):
            f = _pick(BI, [r[tone] for r in roots])
            parts.append("+".join(f"{0.11 / h:.3f}*sin(2*PI*{h}*({f})*t)" for h in range(1, 5)))
        bass = _pick(BI, [55.0, 43.65, 65.41, 49.0])
        lead = _pick(BI, [440.0, 349.23, 523.25, 392.0])
        return ("(" + "+".join(parts) + f")*{env}+0.30*sin(2*PI*({bass})*t)*(0.55+0.45*exp(-8*{E8}))"
                f"+{kick(0.60, 8)}+0.22*(random(0)-0.5)*exp(-22*mod(t-0.625,1.25))"
                f"+0.10*sin(2*PI*({lead})*t)*{env}")
    if mood == "action":
        root = _pick(BI, [110.0, 110.0, 87.31, 98.0])
        gate = "lt(mod(t,0.15625),0.105)"
        stab = _pick(BI, [440.0, 440.0, 349.23, 392.0])
        off = "mod(t-0.3125,0.625)"
        return (f"0.30*({gate})*(sin(2*PI*({root})*t)+0.5*sin(2*PI*2*({root})*t)+0.25*sin(2*PI*3*({root})*t))"
                f"+{kick(0.65, 9)}+0.24*(random(0)-0.5)*exp(-25*mod(t-0.625,1.25))+{hat(0.07)}"
                f"+0.13*(sin(2*PI*({stab})*t)+sin(2*PI*1.189*({stab})*t)+sin(2*PI*1.498*({stab})*t))*exp(-18*{off})*gt(t,0.3125)")
    raise ValueError(mood)


def synth_mood(mood, out):
    """Петля настроения 40 секунд (16 тактов в 96 уд/мин), одинаково громкая у всех настроений."""
    out = pathlib.Path(out)
    if out.exists() and out.stat().st_size > 100000:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    wide = "stereowiden=delay=22:feedback=0.25:crossfeed=0.2,lowpass=f=9500,aecho=0.8:0.55:70|140:0.22|0.12"
    _ff("-f", "lavfi", "-i", f"aevalsrc='{mood_expr(mood)}':s=22050:d={_MLOOP:.3f}", "-af",
        f"aformat=sample_rates=44100:channel_layouts=stereo,{wide},loudnorm=I=-23:TP=-2:LRA=7", "-ar", "44100", str(out))
    return out


# ── настроение каждой сцены

def _heur_moods(scenes):
    n = len(scenes)
    order = ["epic", "action", "tense", "sad", "rise", "calm"]
    out = []
    for i, sc in enumerate(scenes):
        text = str(sc.get("narration", "")).lower()
        sc_n = {m: len(re.findall(MOOD_KEYS[m], text)) for m in MOODS}
        best = max(order, key=lambda m: (sc_n[m], -order.index(m)))
        if sc_n[best] == 0:                          # ключевых слов нет: дуга истории по положению сцены
            pos = i / max(1, n - 1)
            best = "tense" if pos < 0.12 else "calm" if pos < 0.4 else "tense" if pos < 0.55 else "rise" if pos < 0.8 else "epic"
        out.append(best)
    return out


def scene_moods(scenes):
    """Настроение сцены: из ответа Gemini (поле mood), если оно из списка, иначе по словам и положению в истории."""
    base = _heur_moods(scenes)
    return [sc["mood"] if sc.get("mood") in MOODS else base[i] for i, sc in enumerate(scenes)]


def _merge_sections(moods, words, min_words=13, max_sections=5):
    runs = []
    for i, m in enumerate(moods):
        if runs and runs[-1]["mood"] == m:
            runs[-1]["idx"].append(i)
            runs[-1]["w"] += words[i]
        else:
            runs.append({"mood": m, "idx": [i], "w": words[i]})
    while len(runs) > 1:
        k = min(range(len(runs)), key=lambda j: runs[j]["w"])
        if runs[k]["w"] >= min_words and len(runs) <= max_sections:
            break
        nb = k - 1 if k > 0 and (k == len(runs) - 1 or runs[k - 1]["w"] >= runs[k + 1]["w"]) else k + 1
        a, b = sorted((k, nb))
        runs[a] = {"mood": runs[nb]["mood"], "idx": runs[a]["idx"] + runs[b]["idx"], "w": runs[a]["w"] + runs[b]["w"]}
        del runs[b]
        j = 0
        while j < len(runs) - 1:                      # соседние с одинаковым настроением склеиваем
            if runs[j]["mood"] == runs[j + 1]["mood"]:
                runs[j] = {"mood": runs[j]["mood"], "idx": runs[j]["idx"] + runs[j + 1]["idx"],
                           "w": runs[j]["w"] + runs[j + 1]["w"]}
                del runs[j + 1]
            else:
                j += 1
    return runs


def transition_style(prev, nxt):
    """Как музыка меняется на границе: (название, длина перекрытия в секундах)."""
    if nxt in ("epic", "action") and prev not in ("epic", "action"):
        return "hit", 0.08               # удар: нарастание перед границей и резкий вход новой музыки
    if nxt == "tense":
        return "creep", 2.2              # тревога подкрадывается долгим перекрытием
    if nxt == "sad":
        return "slow", 2.0
    if prev == "tense":
        return "release", 1.4
    return "cross", 1.6


_bpm_cache = {}


def _library_track(mood, seed, work):
    """Трек настроения: ваш файл из music/<настроение>/ или синтез. -> {"path", "bpm", "offset", "own"}"""
    folder = pathlib.Path(C["ROOT"]) / "music" / mood
    files = sorted(p for p in folder.glob("*") if p.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a")) if folder.exists() else []
    if files:
        p = random.Random(f"{seed}{mood}").choice(files)
        if str(p) not in _bpm_cache:
            _bpm_cache[str(p)] = detect_bpm(p)
        r = _bpm_cache[str(p)]
        return {"path": p, "bpm": r[0] if r else None, "offset": r[1] if r else 0.0, "own": True}
    cache = pathlib.Path(C.get("MUSIC_CACHE") or work)
    return {"path": synth_mood(mood, cache / f"mood_{mood}_v1.wav"), "bpm": _MBPM, "offset": 0.0, "own": False}


def plan_music(scenes, seed, work):
    """План музыки: настроение сцен -> секции -> трек и переход для каждой. Секции без t0, его проставит цикл сцен."""
    moods = scene_moods(scenes)
    words = [max(1, len(str(sc.get("narration", "")).split())) for sc in scenes]
    runs = _merge_sections(moods, words)
    sections, of_scene = [], [0] * len(scenes)
    for k, run_ in enumerate(runs):
        track = _library_track(run_["mood"], seed, work)
        style, x = transition_style(runs[k - 1]["mood"], run_["mood"]) if k else ("start", 0.0)
        sections.append({"mood": run_["mood"], "scenes": run_["idx"], "track": track, "style": style, "x": x, "t0": None})
        for i in run_["idx"]:
            of_scene[i] = k
    return {"sections": sections, "of_scene": of_scene, "moods": moods}


def build_music_bed(plan, total, work, out):
    """Склеивает музыку по секциям: перекрытие на границах по стилю перехода. Секция начинается на своей первой доле."""
    secs = plan["sections"]
    segs = []
    for i, sec in enumerate(secs):
        t0 = sec["t0"] if sec["t0"] is not None else 0.0
        t_end = (secs[i + 1]["t0"] + secs[i + 1]["x"]) if i + 1 < len(secs) else total + 2.5
        seg = pathlib.Path(work) / f"mus_seg{i}.wav"
        tr = sec["track"]
        _ff("-stream_loop", "-1", "-ss", f"{tr['offset']:.3f}", "-i", str(tr["path"]), "-t", f"{t_end - t0:.3f}",
            "-af", "loudnorm=I=-23:TP=-2:LRA=7", "-ar", "44100", "-ac", "2", str(seg))
        segs.append(seg)
    if len(segs) == 1:
        pathlib.Path(out).write_bytes(segs[0].read_bytes())
        return out
    cmd, fc, prev = [], [], "0:a"
    for s_ in segs:
        cmd += ["-i", str(s_)]
    for i in range(1, len(segs)):
        curve = "qsin" if secs[i]["style"] in ("creep", "slow") else "tri"
        fc.append(f"[{prev}][{i}:a]acrossfade=d={secs[i]['x']:.3f}:c1={curve}:c2={curve}[m{i}]")
        prev = f"m{i}"
    _ff(*cmd, "-filter_complex", ";".join(fc), "-map", f"[{prev}]", "-ar", "44100", "-ac", "2", str(out))
    return out


def music_cuts(plan):
    """[(время, стиль)] границ секций: по ним добавляются нарастание и удар."""
    return [(s["t0"], s["style"]) for s in plan["sections"][1:] if s["t0"] is not None]
