#!/usr/bin/env python3
"""Клиент сайта genspark.ai без API: Playwright открывает чат, отправляет промпт, забирает ответ.

Разметка сверена с сохранённой страницей genspark.ai:
  поле ввода  - textarea[name="query"] с хуком j-search-input (плейсхолдер «Спросите что угодно…»);
  вход        - не отдельная страница /login, а диалог (LoginDialog) и кнопка «Войти»/«Sign in»;
  ответы      - рендерятся в блоки с markdown-классами уже на странице чата.

Аккаунты БЕЗ ОГРАНИЧЕНИЯ по количеству, два способа задать:
  1) отдельные секреты GENSPARK_STORAGE_1, GENSPARK_STORAGE_2, ... - сканируются подряд
     без жёсткого предела (останов через 5 пустых номеров подряд, так что нумеровать можно с любыми пропусками);
  2) один общий секрет GENSPARK_STORAGES - JSON-массив из base64-строк (или готовых storageState),
     туда влезает любое число аккаунтов.
Переменная GENSPARK_ACCOUNTS (по умолчанию 1) задаёт, сколько найденных аккаунтов в ротации
("all" или 0 = все найденные); GENSPARK_ROUNDS - сколько кругов делать по всем аккаунтам,
прежде чем сдаться (по умолчанию 2). Сбойный аккаунт пропускается. Если все не ответили -
исключение, и main.py падает на запасной путь (Gemini API), как раньше.
"""
import base64
import json
import os
import re
import time
import traceback

# Селекторы можно переопределить переменными, если сайт поменяет разметку
INPUT_SEL = os.environ.get("GENSPARK_INPUT_SELECTOR") or ""
ANSWER_SEL = os.environ.get("GENSPARK_ANSWER_SELECTOR") or ""

# По разметке genspark.ai: сначала точные (name="query", хук j-search-input), потом общие
INPUT_SELECTORS = ([INPUT_SEL] if INPUT_SEL else []) + [
    "textarea[name='query']", ".j-search-input", "textarea.search-input",
    "textarea[placeholder*='спрос' i]", "textarea[placeholder*='ask' i]",
    "textarea[placeholder*='chat' i]", "textarea#chat-input", "textarea",
    "div[contenteditable='true']", "[contenteditable='true']",
]
# Блоки с ответом ассистента (страница чата рендерит markdown-блоки)
ANSWER_SELECTORS = ([ANSWER_SEL] if ANSWER_SEL else []) + [
    "[data-testid='chat-answer']", "[data-testid*='message' i]", "[class*='markdown']",
    ".chat-answer", ".markdown-body", ".prose", ".message-content", ".chat-message-content",
]
# Кнопка отправки (появляется после ввода текста)
SEND_SELECTORS = [
    "button[type='submit']", "[aria-label*='отправ' i]", "[aria-label*='send' i]",
    "[class*='send' i][role='button']", ".j-send-btn",
]
# Признаки, что сессия не принята: диалог входа или кнопка «Войти»/«Sign in»
LOGIN_SELECTORS = [
    "[class*='LoginDialog' i]", "[class*='login-dialog' i]", "[class*='AuthToolsPopup' i]",
    "[data-testid*='login' i]",
]

_state = {"next": 0, "dead": set()}


def base_url():
    return os.environ.get("GENSPARK_URL") or "https://www.genspark.ai"


def timeout_s():
    return int(os.environ.get("GENSPARK_TIMEOUT") or 900)          # секунд на один ответ


def retries_n():
    return int(os.environ.get("GENSPARK_RETRIES") or 2)            # попыток на аккаунт за раунд


def rounds_n():
    return int(os.environ.get("GENSPARK_ROUNDS") or 2)             # кругов ротации по аккаунтам


def page_timeout_ms():
    return int(os.environ.get("GENSPARK_PAGE_TIMEOUT") or 60_000)  # мс на загрузку страниц


def video_timeout_s():
    return int(os.environ.get("GENSPARK_VIDEO_TIMEOUT") or 1800)   # секунд на генерацию видео


def video_url():
    return os.environ.get("GENSPARK_VIDEO_URL") or base_url()      # страница видео-агента сайта


def anim_model():
    """Какая модель оживления запрашивается на сайте. По умолчанию Grok lite - самая дешёвая
    (сверено с каталогом Grok Imagine Video: lite поддерживает 480p и 4 с)."""
    return (os.environ.get("GENSPARK_ANIM_MODEL") or "Grok lite").strip() or "Grok lite"


def anim_duration_s():
    """Длительность оживления в секундах. По умолчанию 3 - самая дешёвая
    (Grok Imagine Video поддерживает 1-15 с; короткий ролик зациклится под длину фразы)."""
    try:
        v = int(os.environ.get("GENSPARK_ANIM_DURATION") or "3")
    except ValueError:  # noqa
        v = 4
    return max(3, min(10, v))


def anim_quality():
    """Качество оживления: 480p (максимальная экономия, по умолчанию) или 720p."""
    q = (os.environ.get("GENSPARK_ANIM_QUALITY") or "480p").strip().lower()
    return q if q in ("480p", "720p") else "480p"


def _parse_storage(raw, label):
    """base64 от storageState (или чистый JSON storageState) -> dict. None - не получилось."""
    try:
        return json.loads(base64.b64decode(raw))
    except Exception:  # noqa
        try:
            return json.loads(raw)
        except Exception:  # noqa
            log(f"{label}: не удалось прочитать (это должен быть base64 от storageState)")
            return None


def accounts_count():
    """Сколько найденных аккаунтов использовать. По умолчанию 1.
    'all' или 0 = все найденные без ограничения."""
    v = (os.environ.get("GENSPARK_ACCOUNTS") or "1").strip().lower()
    if v in ("all", "0", "-"):
        return len(accounts_all())
    try:
        return max(1, int(v or 1))
    except ValueError:  # noqa
        return 1


def accounts_all():
    """ВСЕ найденные аккаунты (имя, storageState) - без ограничения по количеству.
    Источники: общий секрет GENSPARK_STORAGES (массив) + отдельные GENSPARK_STORAGE_1..N.
    Отдельные сканируются подряд; останов, если 5 номеров подряд пусты."""
    out = []
    blob = (os.environ.get("GENSPARK_STORAGES") or "").strip()
    if blob:
        try:
            arr = json.loads(blob)
            if isinstance(arr, str):
                arr = [arr]
            for k, item in enumerate(arr if isinstance(arr, list) else [], 1):
                s = item if isinstance(item, str) else json.dumps(item)
                data = item if isinstance(item, dict) else _parse_storage(s, f"GENSPARK_STORAGES[{k}]")
                if data:
                    out.append((f"аккаунт {k} (общий)", data))
        except Exception as e:  # noqa
            log(f"GENSPARK_STORAGES: не удалось разобрать ({str(e)[:80]})")
    empty, i = 0, 0
    while empty < 5 and i < 500:                      # предохранитель от бесконечного цикла
        i += 1
        raw = (os.environ.get(f"GENSPARK_STORAGE_{i}") or "").strip()
        if not raw:
            empty += 1
            continue
        empty = 0
        data = _parse_storage(raw, f"GENSPARK_STORAGE_{i}")
        if data:
            out.append((f"аккаунт {i}", data))
    return out


def accounts():
    """Аккаунты в ротации: первые N из найденных (N = GENSPARK_ACCOUNTS, по умолчанию 1)."""
    return accounts_all()[:accounts_count()]


def log(*a):
    print(*a, flush=True)


def _first(page, selectors, timeout_ms=8000):
    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible():
                    return el
            except Exception:  # noqa
                pass
        time.sleep(0.5)
    return None


def _login_visible(page):
    """Сессия не принята: редирект на вход, диалог входа или кнопка «Войти»/«Sign in» на странице."""
    try:
        u = page.url.lower()
        if any(x in u for x in ("/login", "signin", "sign-in", "accounts.google", "/auth")):
            return True
    except Exception:  # noqa
        pass
    for sel in LOGIN_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                return True
        except Exception:  # noqa
            pass
    try:
        texts = page.evaluate(
            "() => Array.from(document.querySelectorAll('button,a,[role=button]'))"
            ".map(e => (e.innerText || '').trim()).filter(t => t && t.length < 30)")
        for t in texts:
            tl = t.lower()
            if tl in ("войти", "вход", "log in", "login", "sign in") or tl.startswith("sign in "):
                return True
    except Exception:  # noqa
        pass
    return False


def _answer_text(page):
    """Текст последнего ответа ассистента. Пустая строка, если ответа ещё нет."""
    best = ""
    for sel in ANSWER_SELECTORS:
        try:
            for el in page.locator(sel).all():
                try:
                    t = (el.inner_text(timeout=2000) or "").strip()
                    if len(t) > len(best):
                        best = t
                except Exception:  # noqa
                    pass
        except Exception:  # noqa
            pass
    return best


def _wait_answer(ctx, page, before, deadline):
    """Ждём, пока ответ допишется: текст растёт и потом не меняется 4 секунды (стриминг закончился).
    Если сайт открыл диалог в новой вкладке - читаем ответ там."""
    last, stable = "", 0.0
    while time.time() < deadline:
        time.sleep(2)
        cur_page = ctx.pages[-1] if len(ctx.pages) > 1 else page   # новая вкладка с диалогом
        cur = _answer_text(cur_page)
        if cur and cur != before:
            if cur == last:
                stable += 2
                if stable >= 4 and len(cur) > 0:
                    return cur
            else:
                stable, last = 0.0, cur
    return ""


def _submitted(page, box, before):
    """Признаки того, что промпт ушёл: поле очистилось, URL сменился (переход на страницу чата)
    или начал рендериться ответ."""
    try:
        if _answer_text(page) != before:
            return True
        u = page.url.lower()
        if "/agents/" in u or "/chat" in u:      # главная перебросила на страницу диалога
            return True
        if box and box.evaluate("el => (el.value || el.innerText || '').trim()") == "":
            return True                           # сайт очистил поле после отправки
    except Exception:  # noqa
        pass
    return False


def _submit(page, box, before):
    """Отправка промпта: Enter; если за 8 с нет признаков отправки - клик по кнопке отправки."""
    page.keyboard.press("Enter")
    t0 = time.time()
    while time.time() < t0 + 8:
        time.sleep(1)
        if _submitted(page, box, before):
            return
    try:
        btn = _first(page, SEND_SELECTORS, 1500)
        if btn:
            btn.click()
    except Exception:  # noqa
        pass


def _chat_one(account, storage, prompt, shots_dir):
    """Один запрос одним аккаунтом. Возвращает текст ответа или бросает исключение."""
    from playwright.sync_api import sync_playwright
    errs = []
    for attempt in range(retries_n()):
        with sync_playwright() as pw:
            browser = ctx = page = None
            try:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(storage_state=storage, viewport={"width": 1366, "height": 900},
                                          locale="en-US")
                page = ctx.new_page()
                page.set_default_timeout(page_timeout_ms())
                page.goto(base_url(), wait_until="domcontentloaded")
                page.wait_for_timeout(2500)                 # даём SPA отрисоваться
                if _login_visible(page):
                    page.screenshot(path=f"{shots_dir}/gs_fail_login.png")
                    raise RuntimeError("сессия не принята: сайт показывает форму входа (gs_fail_login.png)")
                box = _first(page, INPUT_SELECTORS, 20_000)
                if box is None:
                    page.screenshot(path=f"{shots_dir}/gs_fail_no_input.png")
                    raise RuntimeError("не нашёл поле ввода чата (см. артефакт gs_fail_no_input.png)")
                box.click()
                if box.evaluate("el => el.tagName") == "TEXTAREA":
                    box.fill(prompt[:8000])
                else:
                    box.type(prompt[:8000], delay=8)
                before = _answer_text(page)
                _submit(page, box, before)
                answer = _wait_answer(ctx, page, before, time.time() + timeout_s())
                if not answer.strip():
                    shot_page = ctx.pages[-1] if len(ctx.pages) > 1 else page
                    shot_page.screenshot(path=f"{shots_dir}/gs_fail_no_answer.png")
                    try:  # текст страницы в артефакт: видно, что сайт реально вернул
                        with open(f"{shots_dir}/gs_fail_no_answer.txt", "w") as f:
                            f.write(shot_page.evaluate("() => document.body.innerText")[:20000])
                    except Exception:  # noqa
                        pass
                    raise RuntimeError(f"ответ не пришёл за {timeout_s()} с (см. gs_fail_no_answer.png/txt)")
                page.screenshot(path=f"{shots_dir}/gs_ok.png")
                return answer.strip()
            except Exception as e:  # noqa
                errs.append(str(e)[:180])
                try:
                    if page:
                        page.screenshot(path=f"{shots_dir}/gs_fail_a{attempt}.png")
                except Exception:  # noqa
                    pass
                log(f"genspark.ai {account}, попытка {attempt + 1}/{retries_n()} не удалась: {errs[-1]}")
            finally:
                try:
                    if ctx:
                        ctx.close()
                    if browser:
                        browser.close()
                except Exception:  # noqa
                    pass
    raise RuntimeError("; ".join(errs[-2:]) or "неизвестная ошибка")


def _video_src(page):
    """Адрес готового видео на странице: <video src> или <source src>.
    blob: забираем прямо из страницы в base64 (так у сайта стримятся клипы)."""
    try:
        return page.evaluate(
            """async () => {
                const v = document.querySelector('video');
                if (!v) return null;
                const src = v.currentSrc || v.src || (v.querySelector('source') && v.querySelector('source').src);
                if (!src) return null;
                if (!src.startsWith('blob:')) return src;
                const buf = await (await fetch(src)).arrayBuffer();
                let bin = '';
                const u8 = new Uint8Array(buf);
                for (let i = 0; i < u8.length; i += 8192)
                    bin += String.fromCharCode(...u8.subarray(i, i + 8192));
                return 'data:video/mp4;base64,' + btoa(bin);
            }""")
    except Exception:  # noqa
        return None


def _animate_one(account, storage, image_path, prompt, shots, out_path, deadline_s):
    """Оживление одной картинки через сайт genspark.ai: прикрепляем фото, шлём промт,
    ждём <video>, скачиваем mp4. Возвращает путь файла или бросает исключение."""
    import pathlib
    from playwright.sync_api import sync_playwright
    pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    errs = []
    for attempt in range(retries_n()):
        with sync_playwright() as pw:
            browser = ctx = page = None
            try:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(storage_state=storage, viewport={"width": 1366, "height": 900},
                                          locale="en-US")
                page = ctx.new_page()
                page.set_default_timeout(page_timeout_ms())
                page.goto(video_url(), wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                if _login_visible(page):
                    page.screenshot(path=f"{shots}/gv_fail_login.png")
                    raise RuntimeError("сессия не принята (gv_fail_login.png)")
                try:                      # прикрепляем картинку, если есть приёмник файлов
                    fi = page.locator("input[type='file']").first
                    if fi.count():
                        fi.set_input_files(str(image_path))
                        page.wait_for_timeout(1500)
                except Exception:  # noqa
                    log("кнопка загрузки файла не найдена - промт уйдёт без картинки")
                box = _first(page, INPUT_SELECTORS, 20_000)
                if box is None:
                    page.screenshot(path=f"{shots}/gv_fail_no_input.png")
                    raise RuntimeError("не нашёл поле ввода (gv_fail_no_input.png)")
                box.click()
                if box.evaluate("el => el.tagName") == "TEXTAREA":
                    box.fill(prompt[:4000])
                else:
                    box.type(prompt[:4000], delay=8)
                page.keyboard.press("Enter")
                vsrc, dl = None, time.time() + deadline_s
                while time.time() < dl:   # видео может рендериться на открывшейся вкладке агента
                    time.sleep(5)
                    for p in (ctx.pages[-1:] or [page]):
                        vsrc = _video_src(p)
                        if vsrc:
                            break
                    if vsrc:
                        break
                if not vsrc:
                    (ctx.pages[-1] if len(ctx.pages) > 1 else page).screenshot(
                        path=f"{shots}/gv_fail_no_video.png")
                    raise RuntimeError(f"видео не появилось за {deadline_s} с (gv_fail_no_video.png)")
                if vsrc.startswith("data:"):
                    pathlib.Path(out_path).write_bytes(base64.b64decode(vsrc.split(",", 1)[1]))
                else:
                    resp = ctx.request.get(vsrc)
                    if not resp.ok:
                        raise RuntimeError(f"скачивание видео не удалось: HTTP {resp.status}")
                    pathlib.Path(out_path).write_bytes(resp.body())
                if pathlib.Path(out_path).stat().st_size < 10_000:
                    raise RuntimeError("файл видео подозрительно мал, похоже на заглушку")
                return out_path
            except Exception as e:  # noqa
                errs.append(str(e)[:180])
                try:
                    if page:
                        page.screenshot(path=f"{shots}/gv_fail_a{attempt}.png")
                except Exception:  # noqa
                    pass
                log(f"genspark видео {account}, попытка {attempt + 1}/{retries_n()} не удалась: {errs[-1]}")
            finally:
                try:
                    if ctx:
                        ctx.close()
                    if browser:
                        browser.close()
                except Exception:  # noqa
                    pass
    raise RuntimeError("; ".join(errs[-2:]) or "неизвестная ошибка")


def animate(image_path, prompt, out_path=None):
    """Оживить картинку в видео через сайт genspark.ai (image-to-video).
    Та же ротация аккаунтов, что и в chat(): сбойный пропускается; если все не ответили -
    исключение, и main.py монтирует сцену статичным кадром с камерой, как раньше."""
    accs = accounts()
    if not accs:
        raise RuntimeError("нет аккаунтов: секреты GENSPARK_STORAGES / GENSPARK_STORAGE_N не заданы")
    if _state["dead"]:
        raise RuntimeError("genspark.ai уже не ответил в этом запуске")
    shots = os.environ.get("GENSPARK_SHOTS") or "work/genspark"
    import pathlib
    pathlib.Path(shots).mkdir(parents=True, exist_ok=True)
    out = out_path or "work/genspark/anim.mp4"
    body = (f"Оживи картинку в короткое видео моделью {anim_model()}. "
            f"Длительность ролика ровно {anim_duration_s()} секунд, качество {anim_quality()}. "
            f"Движение камеры и действие строго по описанию, без новых объектов и текста в кадре: {prompt}")
    n = len(accs)
    for rnd in range(rounds_n()):
        for k in range(n):
            i = (_state["next"] + k) % n
            name, storage = accs[i]
            try:
                log(f"оживление картинки ({name}, раунд {rnd + 1}/{rounds_n()})...")
                return _animate_one(name, storage, image_path, body, shots, out, video_timeout_s())
            except Exception as e:  # noqa
                log(f"{name} не оживил: {str(e)[:160]}")
    raise RuntimeError("видео не получилось ни на одном аккаунте genspark.ai")


def chat(prompt, as_json=True, search=False):
    """Спросить сайт genspark.ai. Ответ - текст (или разобранный JSON при as_json=True).
    Ротация без ограничения: кругами обходим все задействованные аккаунты, сбойный пропускается.
    Когда сайт окончательно недоступен, main.py сам возвращается на Gemini API."""
    accs = accounts()
    if not accs:
        raise RuntimeError("нет аккаунтов: секреты GENSPARK_STORAGE_1..N не заданы")
    if _state["dead"]:
        raise RuntimeError("genspark.ai уже не ответил в этом запуске")
    shots = os.environ.get("GENSPARK_SHOTS") or "work/genspark"
    import pathlib
    pathlib.Path(shots).mkdir(parents=True, exist_ok=True)
    task = ("Найди в интернете и ответь, прикладывай ссылки на источники."
            if search else "Ответь строго валидным JSON без пояснений." if as_json else "")
    body = f"{prompt}\n\n{task}".strip() if task else prompt
    n = len(accs)
    for rnd in range(rounds_n()):
        for k in range(n):
            i = (_state["next"] + k) % n
            name, storage = accs[i]
            try:
                log(f"запрос к genspark.ai ({name}, раунд {rnd + 1}/{rounds_n()}, позиция {i + 1}/{n})...")
                text = _chat_one(name, storage, body, shots)
                _state["next"] = i                     # следующий запрос начнёт с этого же аккаунта
                if as_json:
                    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
                    a, b = t.find("{"), t.rfind("}")
                    if a >= 0 and b > a:
                        t = t[a:b + 1]
                    return json.loads(t)
                return text
            except Exception as e:  # noqa
                log(f"{name} не ответил: {str(e)[:160]}")
    _state["dead"] = True
    traceback.print_exc()
    raise RuntimeError("ни один аккаунт genspark.ai не ответил")


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Скажи привет одним предложением."
    print(chat(q, as_json=False))
