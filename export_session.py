#!/usr/bin/env python3
"""Optional LOCAL desktop login, never prints base64 or cookies."""
import argparse
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('-o','--out',default='private-runtime/sessions/account-1.json')
    args=ap.parse_args()
    with sync_playwright() as pw:
        b=pw.chromium.launch(headless=False)
        try:
            ctx=b.new_context()
            p=ctx.new_page();p.goto('https://www.genspark.ai',wait_until='domcontentloaded')
            input('Войди вручную и пройди проверку. Затем Enter для сохранения (значения не печатаются): ')
            target=Path(args.out);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            target.write_text(json.dumps(ctx.storage_state()),encoding='utf8');os.chmod(target,0o600)
            print('Сессия сохранена локально. Не публикуй файл.')
        finally:b.close()
if __name__=='__main__':main()
