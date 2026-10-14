#!/usr/bin/env python3
"""Разовый экспорт сессии genspark.ai: открывает сайт в видимом браузере,
ты логинишься руками, скрипт сохраняет storageState и печатает base64 для секрета.

Запуск (на своём компьютере, не в CI):
  pip install playwright && python -m playwright install chromium
  python export_session.py                 # сохранит файл и напечатает base64
  python export_session.py -o sess2.json   # второй аккаунт, другой профиль

Порядок для нового аккаунта:
  1) запусти скрипт, в открывшемся окне войди в genspark.ai;
  2) увидишь «СЕССИЯ СОХРАНЕНА» - окно можно закрывать;
  3) скопируй напечатанный base64 (весь блок целиком) в секрет GENSPARK_STORAGE_1
     (второй аккаунт - GENSPARK_STORAGE_2 и так далее, можно без ограничения по количеству),
     либо собери все в один секрет GENSPARK_STORAGES - JSON-массив из base64-строк;
  4) поставь переменную GENSPARK_ACCOUNTS = сколько аккаунтов в ротации ("all" = все).

Признак входа: с главной страницы пропадает кнопка «Войти»/«Sign in» (у genspark вход - это
диалог на той же странице, а не отдельный /login), и появляется рабочее поле чата.
"""
import argparse
import base64
import sys
import time
import pathlib

URL = "https://www.genspark.ai"


def logged_in(page):
    """True: кнопки «Войти»/«Sign in» нет, поле чата есть."""
    try:
        texts = page.evaluate(
            "() => Array.from(document.querySelectorAll('button,a,[role=button]'))"
            ".map(e => (e.innerText || '').trim()).filter(t => t && t.length < 30)")
        for t in texts:
            tl = t.lower()
            if tl in ("войти", "вход", "log in", "login", "sign in") or tl.startswith("sign in "):
                return False
        box = page.locator("textarea[name='query'], .j-search-input, textarea").first
        return bool(box.count() and box.is_visible())
    except Exception:  # noqa
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="genspark_session.json", help="файл для storageState")
    ap.add_argument("--timeout", type=int, default=300, help="сколько секунд ждать входа (по умолчанию 300)")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        print("\n>>> Войди в genspark.ai в открывшемся окне браузера. <<<")
        print(f">>> Жду до {args.timeout} секунд... <<<\n")
        ok, stable = False, 0
        t0 = time.time()
        while time.time() - t0 < args.timeout:
            time.sleep(3)
            try:
                page.mouse.move(640, 400)          # держим страницу активной
                page.mouse.move(645, 405)
                ok = logged_in(page)
                stable = stable + 1 if ok else 0
                if stable >= 2:                    # два подряд опроса без кнопки входа
                    ok = True
                    break
            except Exception:  # noqa
                pass
        state = ctx.storage_state()
        pathlib.Path(args.out).write_text(state, encoding="utf-8")
        b64 = base64.b64encode(state.encode()).decode()
        print(f"\n{'СЕССИЯ СОХРАНЕНА' if stable >= 2 else 'ВРЕМЯ ВЫШЛО - сохраняю как есть (вход, возможно, не завершён)'}")
        print(f"файл: {args.out}\n")
        print("=== НАЧАЛО base64 (скопируй весь блок целиком в секрет GENSPARK_STORAGE_N) ===")
        for i in range(0, len(b64), 100):
            print(b64[i:i + 100])
        print("=== КОНЕЦ base64 ===")
        # быстрый самотест: открываем новый контекст с этой сессией и проверяем, что вход держится
        ctx2 = browser.new_context(storage_state=state)
        p2 = ctx2.new_page()
        p2.goto(URL, wait_until="domcontentloaded")
        p2.wait_for_timeout(3000)
        print("Проверка: сессия", "работает - форма входа не показывается"
              if logged_in(p2) else "НЕ подтверждена - проверь вручную")
        input("Нажми Enter, чтобы закрыть браузер...")
        browser.close()
    sys.exit(0 if stable >= 2 else 1)


if __name__ == "__main__":
    main()
