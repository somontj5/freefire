#!/usr/bin/env python3
"""Optional LOCAL conversion of cookie export; secrets are saved to a private file, never printed.\nSupports JSON storageState, Cookie Editor JSON and Netscape cookies.\nFor phone users prefer manual server login in export-session."""
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
    ss = {"no_restriction": "None", "none": "None", "lax": "Lax",
          "strict": "Strict", "unspecified": "Lax"}.get(ss, "Lax")
    exp = c.get("expirationDate") or c.get("expires") or -1
    try:
        exp = float(exp)
    except Exception:  # noqa
        exp = -1
    if exp != -1 and exp <= 0:
        exp = -1                       # expires=0 Chromium считает битым
    secure = bool(c.get("secure", False))
    if ss == "None":
        secure = True                  # SameSite=None без Secure Chromium отвергает
    name = str(c.get("name") or "")
    if name.startswith("__Secure-"):
        secure = True
    path, domain = str(c.get("path") or "/"), str(c.get("domain") or DEFAULT_DOMAIN)
    if name.startswith("__Host-"):
        secure, path, domain = True, "/", domain.lstrip(".")   # правила префикса __Host-
    return {"name": name, "value": str(c.get("value", "")),
            "domain": domain, "path": path or "/", "expires": exp,
            "httpOnly": bool(c.get("httpOnly", False)), "secure": secure,
            "sameSite": ss}


def netscape_cookies(text):
    """Формат Netscape (кнопка «Export» в Cookie Editor): 7 полей через таб.
    domain<TAB>flag<TAB>path<TAB>secure<TAB>expiration<TAB>name<TAB>value"""
    out = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        httponly=line.startswith('#HttpOnly_')
        if httponly:line=line[len('#HttpOnly_'):]
        if not line or line.startswith('#') or '\t' not in line:
            continue
        f = line.split("\t",6)
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
                    "httpOnly": httponly, "secure": secure.strip().upper() == "TRUE",
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
    if "\t" in text and not text.lstrip().startswith(("{", "[")):
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
    ap=argparse.ArgumentParser(description='Локальное преобразование; не печатает секреты')
    ap.add_argument('cookies_file')
    ap.add_argument('-o','--out',default='private-runtime/sessions/account-1.json')
    args=ap.parse_args()
    import os
    import genspark_client as gc
    raw=pathlib.Path(args.cookies_file).read_text(encoding='utf8')
    st=gc._clean({'cookies':parse_cookies(raw),'origins':[]})
    target=pathlib.Path(args.out);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    target.write_text(json.dumps(st),encoding='utf8');os.chmod(target,0o600)
    print('Файл сессии сохранён локально. Значения в лог не выводятся.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
