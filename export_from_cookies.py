#!/usr/bin/env python3
"""Собирает storageState из куков genspark.ai и печатает base64 для секрета GENSPARK_STORAGE_N.

Способ для тех, у кого только телефон: куки снимаются в мобильном браузере расширением
Cookie Editor (Firefox/Chrome на Android), а сам скрипт запускается прямо на GitHub Actions
(workflow export-session). На компьютере тоже работает.

Куки в файле в любом из форматов:
  - JSON-массив (экспорт Cookie Editor): [{"name": "...", "value": "...", ...}, ...]
  - готовый storageState: {"cookies": [...]}
  - строка: name=value; name2=value2  (домен возьмётся .genspark.ai)
"""
import argparse
import base64
import json
import pathlib
import sys

DEFAULT_DOMAIN = ".genspark.ai"
SAME_SITE = {"no_restriction": "None", "none": "None", "lax": "Lax",
             "strict": "Strict", "unspecified": "Lax"}


def normalize(c):
    ss = str(c.get("sameSite") or "").lower()
    exp = c.get("expirationDate") or c.get("expires") or -1
    try:
        exp = float(exp)
    except Exception:  # noqa
        exp = -1
    return {"name": str(c["name"]), "value": str(c.get("value", "")),
            "domain": str(c.get("domain") or DEFAULT_DOMAIN),
            "path": str(c.get("path") or "/"), "expires": exp,
            "httpOnly": bool(c.get("httpOnly", False)), "secure": bool(c.get("secure", True)),
            "sameSite": SAME_SITE.get(ss, "Lax")}


def netscape_cookies(text):
    """Формат Netscape (кнопка «Export» в Cookie Editor): 7 полей через таб.
    domain<TAB>flag<TAB>path<TAB>secure<TAB>expiration<TAB>name<TAB>value"""
    out = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "\t" not in line:
            continue
        f = line.split("\t")
        if len(f) < 7 or not f[5].strip():
            continue
        domain, _flag, path, secure, exp, name, value = f[:7]
        try:
            expires = float(exp)
        except Exception:  # noqa
            expires = -1.0
        out.append({"name": name.strip(), "value": value.strip(),
                    "domain": domain.strip() or DEFAULT_DOMAIN,
                    "path": path.strip() or "/", "expires": expires,
                    "httpOnly": False, "secure": secure.strip().upper() == "TRUE",
                    "sameSite": "Lax"})
    return out


def string_cookies(text):
    out = []
    for part in text.replace("\n", ";").split(";"):     # и через точку с запятой, и построчно
        part = part.strip()
        if "=" in part:
            n, v = part.split("=", 1)
            if n.strip():
                out.append(normalize({"name": n.strip(), "value": v.strip()}))
    return out


def parse_cookies(text):
    text = text.strip()
    if not text:
        sys.exit("Файл с куками пуст")
    # формат Netscape (кнопка «Export»): табы между полями - разбираем сразу, до попытки JSON
    if "\t" in text and "{" not in text[:200]:
        ns = netscape_cookies(text)
        if ns:
            return ns
    try:
        data = json.loads(text)
    except Exception:  # noqa
        cs = string_cookies(text)
        if not cs:
            sys.exit("Не понял формат: нужен JSON-массив куков, storageState или строки name=value")
        return cs
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        return [normalize(c) for c in data["cookies"] if c.get("name")]
    if isinstance(data, list):
        if data and all(isinstance(x, str) for x in data):     # массив строк name=value
            return string_cookies("; ".join(data))
        return [normalize(c) for c in data if isinstance(c, dict) and c.get("name")]
    sys.exit("Не понял формат: нужен JSON-массив куков, storageState или строки name=value")


def verify(state):
    """Проверка: открываем genspark.ai с этими куками и смотрим, показывает ли сайт форму входа."""
    import genspark_client as gc
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        try:
            ctx = b.new_context(storage_state=state, viewport={"width": 1366, "height": 900},
                                locale="en-US")
            pg = ctx.new_page()
            pg.goto(gc.base_url(), wait_until="domcontentloaded")
            pg.wait_for_timeout(3000)
            login = gc._login_visible(pg)
            box = gc._first(pg, gc.INPUT_SELECTORS, 10_000)
            return bool(box) and not login
        finally:
            b.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cookies_file", help="файл с куками (JSON Cookie Editor или строки name=value)")
    ap.add_argument("-o", "--out", default="genspark_session.json", help="куда сохранить storageState")
    ap.add_argument("--verify", action="store_true", help="проверить сессию на настоящем genspark.ai")
    args = ap.parse_args()

    cs = parse_cookies(pathlib.Path(args.cookies_file).read_text(encoding="utf-8", errors="ignore"))
    state = {"cookies": cs, "origins": []}
    pathlib.Path(args.out).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    b64 = base64.b64encode(json.dumps(state, ensure_ascii=False).encode()).decode()

    print(f"куков собрано: {len(cs)}: " + ", ".join(c["name"] for c in cs[:12]) +
          (" ..." if len(cs) > 12 else ""))
    print(f"storageState сохранён в файл: {args.out}\n")
    print("=== НАЧАЛО base64 (скопируй весь блок целиком в секрет GENSPARK_STORAGE_N) ===")
    for i in range(0, len(b64), 100):
        print(b64[i:i + 100])
    print("=== КОНЕЦ base64 ===")

    if not args.verify:
        return 0
    try:
        ok = verify(state)
        print("Проверка на сайте:", "СЕССИЯ РАБОТАЕТ - форма входа не показывается"
              if ok else "СЕССИЯ НЕ ПОДТВЕРЖДЕНА - сайт показывает вход, обнови куки")
        return 0 if ok else 1
    except Exception as e:  # noqa
        print(f"Проверка не удалась: {str(e)[:200]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
