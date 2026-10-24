"""Validated non-secret configuration shared by bot, runner and browser."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PATH = ROOT / 'data/settings.json'
DEFAULTS = {
    'ANIMATE': '1', 'MAX_ANIMATE': '6', 'GENSPARK_FIRST': '1',
    'GENSPARK_ACCOUNTS': '1', 'GENSPARK_ROUNDS': '1', 'GENSPARK_RETRIES': '1',
    'GENSPARK_TIMEOUT': '300', 'GENSPARK_PAGE_TIMEOUT': '60000',
    'GENSPARK_VIDEO_TIMEOUT': '600', 'GENSPARK_MANUAL_WAIT': '600',
    'GENSPARK_HEADLESS': '0', 'GENSPARK_REMOTE': '1', 'GENSPARK_ALLOW_FALLBACK': '0',
    'GENSPARK_ANIM_MODEL': 'Grok lite', 'GENSPARK_ANIM_DURATION': '3',
    'GENSPARK_ANIM_QUALITY': '480p', 'GENSPARK_VIDEO_MODE': 'agent',
    'GENSPARK_FINAL_EDITOR': 'genspark', 'GENSPARK_EDITOR_TIMEOUT': '900', 'GENSPARK_EDITOR_CHUNK': '3',
    'GENSPARK_FINAL_DOWNLOAD_SELECTOR': '',
    'GENSPARK_URL': 'https://www.genspark.ai',
    'GENSPARK_VIDEO_URL': 'https://www.genspark.ai',
    'GENSPARK_INPUT_SELECTOR': '', 'GENSPARK_ANSWER_SELECTOR': '',
    'RESUME': '',
}
LIMITS = {'MAX_ANIMATE': (0,45), 'GENSPARK_ROUNDS': (1,3), 'GENSPARK_RETRIES': (1,3),
          'GENSPARK_TIMEOUT': (30,1800), 'GENSPARK_PAGE_TIMEOUT': (5000,120000),
          'GENSPARK_VIDEO_TIMEOUT': (30,1800), 'GENSPARK_MANUAL_WAIT': (30,1800),
          'GENSPARK_ANIM_DURATION': (1,15), 'GENSPARK_EDITOR_TIMEOUT': (30,1800), 'GENSPARK_EDITOR_CHUNK': (1,20)}
BOOLS = {'ANIMATE','GENSPARK_FIRST','GENSPARK_HEADLESS','GENSPARK_REMOTE','GENSPARK_ALLOW_FALLBACK'}


def validate(key,value):
    if key not in DEFAULTS:
        raise ValueError('Неизвестная или секретная настройка')
    v = str(value).strip()
    if key in BOOLS:
        if v.lower() not in ('0','1','on','off','вкл','выкл','true','false'):
            raise ValueError('Нужно 0 или 1')
        return '1' if v.lower() in ('1','on','вкл','true') else '0'
    if key in LIMITS:
        low,high=LIMITS[key]
        if not v.isdigit() or not low <= int(v) <= high:
            raise ValueError(f'Диапазон {low}–{high}')
        return str(int(v))
    if key == 'GENSPARK_ACCOUNTS':
        if v.lower() in ('all','0'):
            return 'all'
        if not v.isdigit() or int(v)<1:
            raise ValueError('Положительное число или all')
        return str(int(v))
    if key == 'GENSPARK_ANIM_QUALITY' and v not in ('480p','720p'):
        raise ValueError('480p или 720p')
    if key == 'GENSPARK_FINAL_EDITOR' and v not in ('genspark','local'):
        raise ValueError('genspark или local')
    if key == 'GENSPARK_VIDEO_MODE' and v not in ('agent','manual'):
        raise ValueError('agent или manual')
    if key.endswith('_URL'):
        u=urlparse(v)
        if u.scheme != 'https' or u.hostname not in ('genspark.ai','www.genspark.ai') or u.username:
            raise ValueError('Только https://www.genspark.ai/...')
    if key == 'RESUME' and v and v != 'new' and not re.fullmatch(r'\d{8}-\d{6}',v):
        raise ValueError('new, пусто или идентификатор YYYYMMDD-HHMMSS')
    if len(v)>500 or any(ord(c)<32 for c in v):
        raise ValueError('Недопустимое значение')
    return v


def load():
    if not PATH.exists():
        return {}
    d=json.loads(PATH.read_text(encoding='utf8'))
    if not isinstance(d,dict):
        raise ValueError('settings.json должен содержать объект')
    return {k:validate(k,v) for k,v in d.items() if k in DEFAULTS}


def apply():
    for k,v in load().items():
        os.environ[k]=v


def get(key):
    return load().get(key) or os.environ.get(key) or DEFAULTS.get(key,'')


def save(key,value):
    v=validate(key,value)
    d=load(); d[key]=v
    PATH.parent.mkdir(parents=True,exist_ok=True)
    tmp=PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf8')
    tmp.replace(PATH)
    return v


def summary():
    return '⚙️ Настройки (следующий запуск)\n'+'\n'.join(f'{k}={get(k)}' for k in DEFAULTS)+ '\n\n/параметр ИМЯ значение\n/параметр RESUME new — новый ролик\nСекреты через бот не принимаются.'


def reset(key):
    if key not in DEFAULTS:raise ValueError('Неизвестная настройка')
    d=load();d.pop(key,None)
    PATH.parent.mkdir(parents=True,exist_ok=True)
    tmp=PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf8');tmp.replace(PATH)
