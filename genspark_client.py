#!/usr/bin/env python3
"""Server-side browser with HUMAN approval, no stealth/captcha-solving/paid auto retries."""
import atexit
import base64
import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
import runtime_settings as settings
from remote_control import atomic,read

settings.apply()
PRIVATE=Path(__file__).resolve().parent/'private-runtime'
SESSIONS=PRIVATE/'sessions'
_state={'next':0,'blocked':set()}
_pool={};_pw=None
INPUT_SELECTORS=["textarea[name='query']",'.j-search-input','textarea','[contenteditable=true]']
ANSWER_SELECTORS=['[data-message-role=assistant]','[data-role=assistant]',"[data-testid='chat-answer']",'.markdown-body','.markdown-prose','.prose',
                  "[class*='markdown']",'.message-content','.chat-message-content','.chat-answer']
SEND_SELECTORS=["button[type='submit']","[aria-label*='send' i]","[aria-label*='отправ' i]",'.j-send-btn',
                "[class*='send' i][role='button']","[class*='send' i]:is(button,div,span)"]
DIAG=Path(__file__).resolve().parent/'work'/'diag'
# Запасной поиск ответа, если Genspark поменял разметку: берём последний «блок сообщения»,
# поднимаемся к самому внешнему такому блоку, который НЕ содержит наш промпт (это эхо пользователя).
_GENERIC_ANSWER_JS='''(head) => {
  const rx=/markdown|answer|assistant|message|bubble|response|reply|prose|content/i;
  const norm=s=>(s||'').replace(/\\s+/g,' ').trim();
  const bad=el=>el.closest('nav,aside,header,footer,form,textarea,[contenteditable=true]')||el.querySelector('textarea,input,[contenteditable=true]');
  const ok=el=>{const c=(typeof el.className==='string'?el.className:'')+' '+(el.getAttribute('data-testid')||'')+' '+(el.getAttribute('data-role')||'');
    if(!rx.test(c)||bad(el))return false; const r=el.getBoundingClientRect(); if(!r.width||!r.height)return false;
    const t=norm(el.innerText); return t.length>1 && !(head && t.includes(head));};
  const all=Array.from(document.querySelectorAll('body *')).filter(ok);
  if(!all.length)return '';
  let cur=all[all.length-1];
  for(let p=cur.parentElement;p&&p!==document.body;p=p.parentElement){ if(ok(p))cur=p; }
  return (cur.innerText||'').trim();
}'''

class ManualRequired(RuntimeError):pass
class UserStopped(ManualRequired):pass # Мягкий стоп от пользователя: сохранить и не продолжать.
class WaitTimeout(ManualRequired):pass # Время ручного подтверждения истекло — пользователь не успел.
class PageDead(ManualRequired):pass # Страница аккаунта не открылась — виним слот, а не весь прогон.
class AnswerTimeout(ManualRequired):pass # Ответ не пришёл за лимит времени. Это НЕ запрос подтверждения.
class AccountUnavailable(RuntimeError):pass
class BrowserLayoutError(RuntimeError):pass
class BudgetMismatch(RuntimeError):pass


def log(*args):print(*args,flush=True)
def base_url():return os.environ.get('GENSPARK_URL') or settings.DEFAULTS['GENSPARK_URL']
def video_url():return os.environ.get('GENSPARK_VIDEO_URL') or base_url()
def number(k):return int(os.environ.get(k) or settings.DEFAULTS[k])
def timeout_s():return number('GENSPARK_TIMEOUT')
def retries_n():return number('GENSPARK_RETRIES')
def rounds_n():return number('GENSPARK_ROUNDS')
def page_timeout_ms():return number('GENSPARK_PAGE_TIMEOUT')
def video_timeout_s():return number('GENSPARK_VIDEO_TIMEOUT')
def anim_model():return os.environ.get('GENSPARK_ANIM_MODEL') or 'Grok lite'
def anim_duration_s():
    try:return max(1,min(15,int(os.environ.get('GENSPARK_ANIM_DURATION') or '3')))
    except ValueError:return 3

def anim_quality():return os.environ.get('GENSPARK_ANIM_QUALITY') or '480p'
def remote():return os.environ.get('GENSPARK_REMOTE')=='1' and bool(os.environ.get('GENSPARK_REMOTE_CONTROL'))


def _parse_storage(raw,label='session'):
    # Never print raw values, cookie names or parsing exception details.
    try:
        if isinstance(raw,dict):d=raw
        else:
            try:d=json.loads(raw)
            except ValueError:d=json.loads(base64.b64decode(raw))
        if not isinstance(d,dict) or not isinstance(d.get('cookies',[]),list):raise ValueError()
        return d
    except Exception:
        raise ValueError(f'{label}: неверный формат секрета. Используй JSON storageState или base64.') from None


def accounts_all():
    out=[];blob=os.environ.get('GENSPARK_STORAGES','').strip()
    if blob:
        try:arr=json.loads(blob)
        except ValueError:raise ValueError('GENSPARK_STORAGES: нужен JSON-массив') from None
        if not isinstance(arr,list):raise ValueError('GENSPARK_STORAGES: нужен JSON-массив')
        for i,item in enumerate(arr,1):out.append((f'account-{i}',_parse_storage(item,f'account-{i}')))
    for k in sorted((k for k in os.environ if re.fullmatch(r'GENSPARK_STORAGE_\d+',k)),key=lambda k:int(k.rsplit('_',1)[1])):
        if os.environ[k].strip():out.append((f'account-{k.rsplit("_",1)[1]}',_parse_storage(os.environ[k],k)))
    # Include encrypted restored sessions and empty accounts for manual server-side login.
    # Свежие файлы сессий важнее старых секретов GENSPARK_STORAGE_N: при совпадении имени берём восстановленный файл.
    if SESSIONS.exists():
        restored={p.stem:read(p) for p in sorted(SESSIONS.glob('account-*.json'))}
        out=[(n,restored[n]) if n in restored else (n,st) for n,st in out]
        known={name for name,_ in out}
        for n,st in restored.items():
            if n not in known:out.append((n,st))
    if not out and remote():
        n=os.environ.get('GENSPARK_ACCOUNTS') or '1'
        if n=='all':n='1'
        out=[(f'account-{i}',{'cookies':[],'origins':[]}) for i in range(1,int(n)+1)]
    unique={}
    for name,st in sorted(out,key=lambda x:_acc_num(x[0])):unique.setdefault(name,st)
    # Adding an account must work when previous accounts already have saved sessions.
    if remote():
        requested=os.environ.get('GENSPARK_ACCOUNTS') or '1'
        if requested not in ('all','0'):
            idx=1
            while len(unique)<int(requested):
                name=f'account-{idx}'
                unique.setdefault(name,{'cookies':[],'origins':[]})
                idx+=1
    return sorted(unique.items(),key=lambda x:_acc_num(x[0]))


def _acc_num(name):
    # Числовой порядок: account-1, 2, ... 10, 11 (а не 1, 10, 11 ...), чтобы ротация начиналась с account-1.
    m=re.search(r'(\d+)$',name);return int(m.group(1)) if m else 10**9


def accounts_count():
    v=os.environ.get('GENSPARK_ACCOUNTS') or '1'
    return len(accounts_all()) if v in ('all','0') else max(1,int(v))
def accounts():
    acc=accounts_all()[:accounts_count()]
    if os.environ.get('GENSPARK_TASK')=='create':
        # В сборке участвуют только вошедшие аккаунты: «30 слотов, вошли в 3» — ротация крутит эти 3,
        # а пустые слоты не требуют входа посреди генерации. Вход (TASK=login) видит все слоты.
        live=[(n,st) for n,st in acc if st.get('cookies')]
        if live:acc=live
        elif acc:raise ManualRequired('Ни один аккаунт не вошёл (все слоты пустые — restore не вернул сессии). '
                                        'Запусти «Вход в аккаунты» и войди хотя бы в один.')
    return acc


def _sanitize_cookie(c):
    if not isinstance(c,dict):return None
    name=c.get('name','');domain=c.get('domain','')
    if not isinstance(name,str) or any(ord(ch)<33 or ch in ';,' for ch in name) or not name:
        raise ValueError('Повреждённый экспорт: имя cookie содержит табы/пробелы. Обнови секрет через JSON-export.')
    if not isinstance(domain,str):return None
    host=domain.lstrip('.')
    if host!='genspark.ai' and not host.endswith('.genspark.ai'):return None
    value=c.get('value','')
    if not isinstance(value,str) or any(ord(ch)<32 for ch in value):raise ValueError('Недопустимые поля cookie; нужен свежий JSON-export.')
    path=c.get('path') or '/'
    if not isinstance(path,str) or not path.startswith('/'):raise ValueError('Недопустимый путь cookie')
    ss=c.get('sameSite','Lax'); ss={'no_restriction':'None','unspecified':'Lax'}.get(ss,ss)
    if ss not in ('None','Lax','Strict'):ss='Lax'
    exp=float(c.get('expires',c.get('expirationDate',-1)) or -1)
    if not math.isfinite(exp):raise ValueError('Недопустимый срок cookie')
    if exp not in (-1,0) and exp<time.time():return None
    if exp==0:exp=-1
    secure=bool(c.get('secure',True))
    if ss=='None' or name.startswith('__Secure-'):secure=True
    if name.startswith('__Host-'):secure=True;path='/';domain=domain.lstrip('.')
    return dict(name=name,value=value,domain=domain,path=path,expires=exp,secure=secure,
                httpOnly=bool(c.get('httpOnly',False)),sameSite=ss)


def _clean(storage):
    cookies=[v for v in (_sanitize_cookie(c) for c in storage.get('cookies',[])) if v]
    origins=[]
    for o in storage.get('origins',[]):
        host=urlparse(o.get('origin','')).hostname or ''
        if host=='genspark.ai' or host.endswith('.genspark.ai'):origins.append(o)
    return {'cookies':cookies,'origins':origins}


def _context(name,storage):
    global _pw
    if name in _pool:return _pool[name][1]
    # Limit RAM on small GitHub runners: keep only the active account browser.
    for other,(browser,context) in list(_pool.items()):
        _persist(other,context);browser.close();del _pool[other]
    from playwright.sync_api import sync_playwright
    if _pw is None:_pw=sync_playwright().start()
    path=SESSIONS/f'{name}.json'
    actual=read(path) if path.exists() else storage
    try:clean=_clean(actual)
    except ValueError:
        if not remote():raise
        log(f'{name}: старый экспорт повреждён; открываю пустую сессию для ручного входа, без ремонта токенов.')
        clean={'cookies':[],'origins':[]}
    b=_pw.chromium.launch(headless=(os.environ.get('GENSPARK_HEADLESS','0')=='1'),
                          args=['--window-size=1280,800'])
    try:ctx=b.new_context(storage_state=clean,viewport={'width':1240,'height':700},locale='ru-RU',accept_downloads=True)
    except Exception:
        b.close();raise ValueError('Chromium отклонил cookie. Не пропускаю неизвестную cookie авторизации; обнови JSON-export.') from None
    _pool[name]=(b,ctx)
    return ctx


def _persist(name,ctx):
    # encrypted backup is built at job end; plaintext stays in ignored, private directory only
    st=ctx.storage_state()
    atomic(SESSIONS/f'{name}.json',_clean(st))


def close():
    global _pw
    for name,(b,ctx) in list(_pool.items()):
        try:_persist(name,ctx)
        except Exception:pass
        try:b.close()
        except Exception:pass
    _pool.clear()
    if _pw:
        try:_pw.stop()
        except Exception:pass
        _pw=None
atexit.register(close)


def _first(page,selectors,timeout_ms=8000):
    end=time.monotonic()+timeout_ms/1000
    while time.monotonic()<end:
        for sel in selectors:
            for el in page.locator(sel).all():
                if el.is_visible():return el
        page.wait_for_timeout(250)
    return None


def body_text(page,timeout_ms=3000):
    # Пустая/закрытая/падшая вкладка не должна ронять задачу — просто считаем, что текста нет.
    try:return page.locator('body').inner_text(timeout=timeout_ms).lower()
    except Exception:return ''


def _login_visible(page):
    try:
        u=page.url.lower()
        if any(x in u for x in ('/login','/signin','accounts.google.com')):return True
        for label in ('Войти','Sign in','Log in'):
            if page.get_by_text(label,exact=True).first.is_visible():return True
    except Exception:return False
    return False


def challenge(page):
    text=body_text(page)
    return any(s in text for s in ('performing security verification','verify you are human','проверка безопасности','checking your browser'))


def quota(page):
    text=body_text(page)
    return any(s in text for s in ('insufficient credits','not enough credits','daily limit reached','недостаточно кредитов','дневной лимит исчерпан',
                                   'used all your credits','used up all your credits','out of credits','credits exhausted','credits have run out',
                                   'использовали все свои кредиты','использовали все кредиты','кредиты закончились','кредиты исчерпаны'))


def notify(message,markup=None):
    if os.environ.get('TG_TOKEN') and os.environ.get('TG_CHAT_ID'):
        import requests
        try:
            requests.post('https://api.telegram.org/bot'+os.environ['TG_TOKEN']+'/sendMessage',
                          json={'chat_id':os.environ['TG_CHAT_ID'],'text':message,**({'reply_markup':markup} if markup else {})},timeout=15)
        except Exception:log('Не удалось отправить уведомление; ссылки в Summary Actions.')


_telegram_offset=None
_telegram_live_polling=False


def _telegram_tick():
    global _telegram_offset
    if _telegram_live_polling:
        return # the background service is the only getUpdates consumer for this job
    if not os.environ.get('TG_TOKEN') or not os.environ.get('TG_CHAT_ID'):return
    import requests
    import main as api
    base='https://api.telegram.org/bot'+os.environ['TG_TOKEN']+'/'
    params={'timeout':0,'allowed_updates':'["message","callback_query"]'}
    if _telegram_offset is not None:params['offset']=_telegram_offset
    try:
        result=requests.get(base+'getUpdates',params=params,timeout=8).json()
        if not result.get('ok'):return
        for update in result.get('result',[]):
            _telegram_offset=update['update_id']+1
            if 'callback_query' in update:api.handle_callback(update['callback_query'])
            elif 'message' in update:api.handle_message(update['message'])
        if result.get('result'):
            requests.get(base+'getUpdates',params={'offset':_telegram_offset,'timeout':0},timeout=8)
    except Exception:log('Telegram временно недоступен; резервная панель остаётся по приватной ссылке.')


def human(message,on_tick=None):
    if not remote():raise ManualRequired('Нужно ручное подтверждение; запусти workflow с Tailscale.')
    rid=uuid.uuid4().hex
    atomic(PRIVATE/'request.json',{'id':rid,'message':message})
    control=os.environ['GENSPARK_REMOTE_CONTROL']; screen=os.environ.get('GENSPARK_REMOTE_SCREEN','')
    rows=[]
    if screen:rows.append([{'text':'🌐 Открыть браузер','url':screen}])
    rows.append([{'text':'✅ Выполнено — продолжить','callback_data':f'cf:{rid}:continue'},
                 {'text':'⏸ Сохранить и остановить','callback_data':f'cf:{rid}:pause'}])
    rows.append([{'text':'⚙️ Меню','callback_data':'m:home'}])
    notify(f'🖥 {message}\nОжидание: {number("GENSPARK_MANUAL_WAIT")} с. '
           'Открой браузер кнопкой, подтверди вручную, затем нажми «Выполнено». GitHub/Summary открывать не нужно.',
           {'inline_keyboard':rows})
    log('Ожидаю ручное действие — кнопки отправлены в Telegram.')
    end=time.monotonic()+number('GENSPARK_MANUAL_WAIT')
    next_poll=0
    while time.monotonic()<end:
        if on_tick:on_tick()
        if time.monotonic()>=next_poll:
            _telegram_tick()
            next_poll=time.monotonic()+2
        d=read(PRIVATE/'approval.json')
        if d.get('id')==rid:
            atomic(PRIVATE/'request.json',{'message':'Действие принято'})
            if d.get('action')=='pause':raise UserStopped('Ты остановил задачу; прогресс сохранён.')
            return
        time.sleep(.5)
    raise WaitTimeout('Время ручного подтверждения истекло; прогресс сохранён.')


def _ready(name,ctx,url):
    for attempt in (1,2):
        pg=ctx.new_page();pg.set_default_timeout(page_timeout_ms())
        try:pg.goto(url,wait_until='domcontentloaded')
        except Exception as e:
            log(f'{name}: загрузка не завершилась ({type(e).__name__}); попытка {attempt}/2.')
            try:pg.close()
            except Exception:pass
            continue
        pg.wait_for_timeout(1500)
        if not body_text(pg):
            # Вкладка открылась пустой (страница не догрузилась или рендер упал) — пересоздаю вкладку.
            log(f'{name}: вкладка открылась пустой; попытка {attempt}/2.')
            try:pg.close()
            except Exception:pass
            continue
        if _login_visible(pg) and os.environ.get('GENSPARK_TASK')=='create':
            # Слот без входа (пропущен при регистрации): не просим вход посреди сборки, просто берём следующий.
            try:pg.close()
            except Exception:pass
            raise AccountUnavailable('не выполнен вход')
        if challenge(pg) or _login_visible(pg):
            pg.bring_to_front()
            hint='\nХватит аккаунтов — жми «Сохранить и остановить»: уже вошедшие сохранятся.' if os.environ.get('GENSPARK_TASK')=='login' else ''
            while True:
                human(f'{name}: пройди проверку Cloudflare и войди в Genspark вручную. Затем нажми «Выполнено».{hint}')
                pg.wait_for_timeout(1000)
                if not challenge(pg) and not _login_visible(pg):break
                log(f'{name}: вход ещё не завершён; открой экран снова и продолжай — задача не падает.')
        _persist(name,ctx)
        return pg
    raise PageDead('Страница Genspark не открылась после двух попыток; прогресс сохранён.')


def _prompt_head(prompt):
    # Начало промпта без префикса: по нему отличаем эхо нашего сообщения от ответа ассистента.
    body=prompt.split('\n',1)[1] if prompt.startswith('Текстовый этап') and '\n' in prompt else prompt
    return ' '.join(body.split())[:60]


def _answer_text(page,head=''):
    override=os.environ.get('GENSPARK_ANSWER_SELECTOR')
    selectors=[override] if override else ANSWER_SELECTORS
    for sel in selectors:
        try:els=page.locator(sel).all()
        except Exception:continue
        for e in reversed(els):
            try:
                if not e.is_visible():continue
                t=e.inner_text(timeout=2000).strip()
            except Exception:continue
            if t and not (head and head in ' '.join(t.split())):return t
    if override:return ''
    try:return (page.evaluate(_GENERIC_ANSWER_JS,head) or '').strip()
    except Exception:return ''


def _input_value(el):
    try:return el.evaluate("el => (el.value ?? el.innerText ?? '').trim()")
    except Exception:return '' # поле исчезло — значит, страница ушла в диалог


def _submit(pg,inp):
    # Enter один раз. Если через 8 с текст всё ещё в поле и URL не сменился — запрос НЕ ушёл,
    # тогда (и только тогда) жмём кнопку отправки. Дубля не будет: отправленный запрос очищает поле.
    url0=pg.url;inp.press('Enter')
    for _ in range(16):
        pg.wait_for_timeout(500)
        if pg.url!=url0 or not _input_value(inp):return True
    btn=_first(pg,SEND_SELECTORS,2000)
    if btn is not None:
        log('Enter не отправил запрос; нажимаю кнопку отправки.')
        try:btn.click()
        except Exception:pass
        for _ in range(16):
            pg.wait_for_timeout(500)
            if pg.url!=url0 or not _input_value(inp):return True
    return False


def _diagnose(page,name,reason):
    # Скриншот + текст + HTML страницы в work/diag и фото в Telegram: видно, что реально показал Genspark.
    DIAG.mkdir(parents=True,exist_ok=True);stamp=time.strftime('%Y%m%d-%H%M%S')
    shot=DIAG/f'{name}-{stamp}.png'
    try:page.screenshot(path=str(shot),full_page=False)
    except Exception:shot=None
    try:(DIAG/f'{name}-{stamp}.txt').write_text(page.locator('body').inner_text(timeout=5000)[:30000],encoding='utf8')
    except Exception:pass
    try:(DIAG/f'{name}-{stamp}.html').write_text(page.content()[:2_000_000],encoding='utf8')
    except Exception:pass
    try:
        classes=page.evaluate('''() => {const m={};document.querySelectorAll('body *').forEach(e=>{
          if(typeof e.className!=='string')return; e.className.split(/\\s+/).forEach(c=>{if(c&&/markdown|answer|assist|message|bubble|response|reply|prose|chat/i.test(c))m[c]=(m[c]||0)+1;});});
          return Object.entries(m).sort((a,b)=>b[1]-a[1]).slice(0,25).map(([k,v])=>k+':'+v).join(' ');}''')
        log(f'{name}: диагностика — URL {page.url[:120]}; классы-кандидаты: {classes or "нет"}')
    except Exception:pass
    if shot and os.environ.get('TG_TOKEN') and os.environ.get('TG_CHAT_ID'):
        import requests
        try:
            with open(shot,'rb') as f:
                requests.post('https://api.telegram.org/bot'+os.environ['TG_TOKEN']+'/sendPhoto',
                              data={'chat_id':os.environ['TG_CHAT_ID'],'caption':f'🔍 {name}: {reason}'[:1000]},
                              files={'photo':f},timeout=30)
        except Exception:log('Скриншот диагностики не отправился в Telegram; он лежит в work/diag.')


def _text_task_path(prompt):
    namespace=os.environ.get('GENSPARK_JOB_ID') or os.environ.get('GITHUB_RUN_ID') or 'local'
    digest=hashlib.sha256((namespace+'\n'+prompt).encode()).hexdigest()
    return SESSIONS/f'text-{digest}.json'


def _chat_one(name,storage,prompt,shots_dir=None):
    taskpath=_text_task_path(prompt);pending=read(taskpath)
    if pending.get('status')=='complete':return pending['reply']
    if pending.get('account') not in (None,name):raise ManualRequired('Текстовый запрос уже отправлен другим аккаунтом; не создаю дубль')
    ctx=_context(name,storage); pg=_ready(name,ctx,pending.get('url') or base_url())
    head=_prompt_head(prompt)
    if pending:
        before=''
        human(f'{name}: продолжение уже отправленного текстового запроса. Дождись ответа в серверном браузере, затем «Выполнено». '
              'Не отправляй запрос снова, если диалог уже существует.')
    else:
        inp=_first(pg,[os.environ['GENSPARK_INPUT_SELECTOR']] if os.environ.get('GENSPARK_INPUT_SELECTOR') else INPUT_SELECTORS)
        if inp is None:raise BrowserLayoutError('Не найдено поле чата после входа. Нужна проверка UI, не повтор генерации.')
        before=_answer_text(pg,head);inp.fill(prompt)
        atomic(taskpath,{'account':name,'url':pg.url,'status':'submitted'})
        # Send exactly once. No duplicate Enter, no silent 8000-character truncation.
        if _submit(pg,inp):log(f'{name}: запрос отправлен; жду ответ до {timeout_s()} с.')
        else:
            # Запрос так и не ушёл — денег не потрачено, можно честно сбросить задачу и сказать почему.
            _diagnose(pg,name,'запрос не отправился: текст остался в поле ввода')
            taskpath.unlink(missing_ok=True)
            raise BrowserLayoutError('Запрос не отправился: ни Enter, ни кнопка отправки не сработали. '
                                     'Скриншот отправлен; проверь UI Genspark (поле ввода/кнопку).')
    end=time.monotonic()+timeout_s();last='';stable=0;next_beat=time.monotonic()+60
    seen=0;busy=False
    while time.monotonic()<end:
        pg.wait_for_timeout(1000)
        page=ctx.pages[-1]
        atomic(taskpath,{'account':name,'url':page.url,'status':'submitted'})
        if time.monotonic()>=next_beat:
            log(f'{name}: ответ генерируется ({int(timeout_s()-(end-time.monotonic()))}/{timeout_s()} с, '
                f'видно текста: {seen} симв., вкладок: {len(ctx.pages)}, URL: {page.url[:100]})...')
            next_beat=time.monotonic()+60
        if quota(page):
            taskpath.unlink(missing_ok=True)
            raise AccountUnavailable('На этом аккаунте недостаточно кредитов')
        if challenge(page):
            # Проверка посреди ответа: даём кнопки, а не молчаливую паузу.
            pg.bring_to_front()
            human(f'{name}: появилась проверка Cloudflare во время ответа. Пройди её в серверном браузере, затем «Выполнено» — ответ продолжит собираться.')
            if challenge(page):raise ManualRequired('Проверка не пройдена; прогресс сохранён')
        cur=_answer_text(page,head)
        if cur!=before:seen=max(seen,len(cur))
        try:busy=page.locator('[aria-busy=true]').count()>0 or page.get_by_role('button',name=re.compile('Stop|Остановить',re.I)).count()>0
        except Exception:busy=False
        if cur and cur!=before:
            stable=stable+1 if cur==last and not busy else 0;last=cur
            if stable>=8:
                atomic(taskpath,{'account':name,'url':page.url,'status':'complete','reply':cur})
                _persist(name,ctx)
                for old in ctx.pages[:-1]:old.close()
                return cur
    page=ctx.pages[-1]
    state_word='генерация ещё идёт' if busy else 'активности генерации не видно'
    _diagnose(page,name,f'нет ответа за {timeout_s()} с ({state_word}, текста {seen} симв.)')
    suggested=min(1800,timeout_s()*2)
    if seen==0 and not busy:
        # Поднимать лимит бесполезно: бот не видит ответ вообще — скорее всего, сменилась разметка.
        hint=('Бот не нашёл блок ответа на странице — увеличение тайм-аута не поможет. '
              'Скриншот страницы отправлен выше; если ответ на нём виден — пришли его, '
              'либо задай селектор: /параметр GENSPARK_ANSWER_SELECTOR <css>.')
    elif suggested>timeout_s():hint=f'Подними лимит: /параметр GENSPARK_TIMEOUT {suggested} и снова /make.'
    else:hint='Лимит уже максимальный (1800 с). Скриншот страницы отправлен выше.'
    raise AnswerTimeout(f'Ответ не пришёл за {timeout_s()} с (видел текст: {seen} симв., {state_word}). {hint} '
                        'Прогресс сохранён, запрос повторно не отправляется.')


def _parse_json(text):
    cleaned=re.sub(r'^```(?:json)?\s*|\s*```$','',text.strip())
    try:return json.loads(cleaned)
    except ValueError:
        decoder=json.JSONDecoder()
        for m in re.finditer(r'[\[{]',cleaned):
            try:return decoder.raw_decode(cleaned[m.start():])[0]
            except ValueError:continue
    raise BrowserLayoutError('Ответ не является JSON; повторный запрос автоматически не отправлен')


def _rotate(fn):
    acc=accounts()
    if not acc:raise ManualRequired('Нет сессий. Запусти export-session и войди через удалённый экран.')
    start=_state['next']%len(acc)
    for k in range(len(acc)):
        i=(start+k)%len(acc); name,st=acc[i]
        if name in _state['blocked']:continue
        try:
            out=fn(name,st);_state['next']=(i+1)%len(acc);return out
        except AccountUnavailable as e:
            _state['blocked'].add(name);_state['next']=(i+1)%len(acc)
            log(f'{name}: {e}; переход к следующему аккаунту.')
    raise ManualRequired('Во всех аккаунтах закончились кредиты или не выполнен вход. Прогресс сохранён.')


def chat(prompt,as_json=True,search=False):
    task='\nВерни только валидный JSON.' if as_json else ''
    if search:task+='\nНайди факты в интернете и приложи ссылки на источники.'
    prefix=('Текстовый этап: используй Claude для анализа/сценария/промптов. '
            'Не запускай генерацию картинок, видео или музыки в этом текстовом запросе.\n')
    body=prefix+prompt+task;pending=read(_text_task_path(body))
    if pending.get('status')=='complete':text=pending['reply']
    elif pending.get('account'):
        acc=next(((n,s) for n,s in accounts_all() if n==pending['account']),None)
        if not acc:raise ManualRequired('Аккаунт незавершённого текста недоступен; запрос повторно не отправлен')
        text=_chat_one(*acc,body)
    else:text=_rotate(lambda name,st:_chat_one(name,st,body))
    return _parse_json(text) if as_json else text


def inspect_video(path):
    r=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],capture_output=True,text=True,check=True)
    d=json.loads(r.stdout);v=next(s for s in d['streams'] if s['codec_type']=='video')
    duration=float(d['format'].get('duration',v.get('duration',0)))
    if duration<=0:raise BudgetMismatch('Видео пустое')
    if abs(duration-anim_duration_s())>.35 or min(v['width'],v['height'])!=int(anim_quality()[:-1]):
        # Клип уже оплачен: монтаж сам подгонит длину (цикл/обрезка) и масштаб. Не останавливаемся и не перегенерируем.
        log(f"Клип {duration:.1f} с, {v['width']}x{v['height']} отличается от заказа "
            f"({anim_duration_s()} с, {anim_quality()}); использую как есть, повтор не запускаю.")
    return d


def _attach(pg,image):
    # Genspark 6.0: «+» (aria-label «Добавить файлы и другое») → меню → «Просмотреть локальные файлы» → диалог.
    # Разметка меню меняется, поэтому несколько бесплатных способов подряд (ничего не отправляется, кредиты не тратятся).
    # image может быть списком файлов (финальный монтаж): прикрепляем все разом.
    many=isinstance(image,(list,tuple))
    image=[str(x) for x in image] if many else str(image)
    wait=3000+(len(image)*2000 if many else 0) # загрузка нескольких клипов дольше
    def via_input():
        fi=pg.locator('input[type=file]')
        if fi.count():
            el=fi.last
            if many and el.get_attribute('multiple') is None:return False
            el.set_input_files(image);pg.wait_for_timeout(wait);return True
        return False
    def click_chooser(el,timeout=6000):
        try:
            with pg.expect_file_chooser(timeout=timeout) as fc:el.click()
            if many and not fc.value.is_multiple():return False
            fc.value.set_files(image);pg.wait_for_timeout(wait);return True
        except Exception:return False
    if via_input():return True
    plus=_first(pg,['.add-entry-btn',"[aria-label='Добавить файлы и другое']","[aria-label*='Добавить файлы' i]",
                    "[aria-label*='Add files' i]","[aria-label*='attach' i]"],5000)
    if plus is not None:
        # 1) «+» сам может открыть диалог; 2) иначе открылось меню — ищем пункт по тексту.
        if click_chooser(plus,3000):return True
        if via_input():return True
        rx=re.compile('локальн|с компьютера|с устройства|загруз|файл|local|upload|computer|device|browse',re.I)
        for _ in range(2):
            pg.wait_for_timeout(800)
            items=[]
            for sel in ['.add-entry-option-item','[role=menuitem]','[role=option]','li','[class*=option]','[class*=item]','button','div']:
                try:
                    for el in pg.locator(sel).filter(has_text=rx).all()[-15:]:
                        try:
                            if el.is_visible() and len(el.inner_text(timeout=500))<60:items.append(el)
                        except Exception:pass
                except Exception:pass
                if items:break
            try:log('меню «+»: '+' | '.join(t.strip() for t in pg.locator('[role=menu],[class*=popover],[class*=dropdown],[class*=option]').all_inner_texts() if t.strip())[:300])
            except Exception:pass
            for el in items:
                if click_chooser(el):return True
                if via_input():return True
            if not items:
                try:plus.click()
                except Exception:pass
    if many:return False # paste/drop ниже — только для одной картинки
    # Запасной путь: вставка картинки из буфера (paste) и drop прямо в поле ввода.
    inp=_first(pg,INPUT_SELECTORS,2000)
    if inp is None:return False
    data=base64.b64encode(Path(image).read_bytes()).decode()
    inp.evaluate('''(el,[b64,name]) => { const bin=atob(b64),u=new Uint8Array(bin.length);
        for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);
        const f=new File([u],name,{type:'image/png'}),dt=new DataTransfer();dt.items.add(f);el.focus();
        el.dispatchEvent(new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}));
        return true;}''',[data,Path(image).name])
    pg.wait_for_timeout(3000)
    if pg.locator('img[src^="blob:"],img[src^="data:"],[class*=attach] img,[class*=file] img,[class*=upload] img').count():return True
    inp.evaluate('''(el,[b64,name]) => { const bin=atob(b64),u=new Uint8Array(bin.length);
        for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);
        const f=new File([u],name,{type:'image/png'}),dt=new DataTransfer();dt.items.add(f);
        for(const t of ['dragenter','dragover','drop'])el.dispatchEvent(new DragEvent(t,{dataTransfer:dt,bubbles:true,cancelable:true}));
        return true;}''',[data,Path(image).name])
    pg.wait_for_timeout(3000)
    return bool(pg.locator('img[src^="blob:"],img[src^="data:"],[class*=attach] img,[class*=file] img,[class*=upload] img').count())


_VIDEO_JS=r"""() => {
  const vids=[...document.querySelectorAll('video')].reverse();
  for(const v of vids){const u=v.currentSrc||v.src||v.querySelector('source')?.src||'';if(u)return u;}
  const links=[...document.querySelectorAll('a[href],source[src]')].reverse();
  for(const a of links){const u=a.href||a.src||'';if(/\.(mp4|webm|mov)(\?|#|$)/i.test(u))return u;}
  const m=document.documentElement.innerHTML.match(/https?:\/\/[^"'\s<>]+?\.(?:mp4|webm|mov)(?:\?[^"'\s<>]*)?/gi);
  return m?m[m.length-1].replace(/&amp;/g,'&'):'';
}"""


def _find_video_src(page):
    # Genspark может показать клип не плеером <video>, а ссылкой/превью — ищем все варианты.
    try:return page.evaluate(_VIDEO_JS) or ''
    except Exception:return ''


def _animate_one(name,storage,image,prompt,shots,out_path,deadline_s):
    # Manual UI selection is intentional: home-page HTML does NOT establish video generator selectors.
    ctx=_context(name,storage)
    key=hashlib.sha256(Path(image).read_bytes()+prompt.encode()).hexdigest()
    taskpath=SESSIONS/f'task-{key}.json';pending=read(taskpath)
    mode=os.environ.get('GENSPARK_VIDEO_MODE') or 'agent'
    if mode=='agent' and pending and pending.get('status')!='submitted':
        # Прошлый запуск не отправил задачу (статус awaiting-human) — это не дубль: бот отправит её сам.
        taskpath.unlink(missing_ok=True);pending={}
    if pending.get('account') not in (None,name):raise ManualRequired('Эта сцена уже отправлена на другом аккаунте; дождись её результата, не создавай дубль.')
    pg=_ready(name,ctx,pending.get('url') or video_url())
    pg.bring_to_front()
    if mode!='agent':
        atomic(taskpath,{'account':name,'url':pg.url,'status':'awaiting-human','settings':{'model':anim_model(),'duration':anim_duration_s(),'quality':anim_quality()}})
    instructions=(f'Используй инструмент генерации видео: модель {anim_model()}, ровно {anim_duration_s()} секунд, '
                  f'{anim_quality()}, один image-to-video клип из приложенной картинки. '
                  'Не подменяй модель более дорогой и не делай вариации. '
                  'Если указанные параметры недоступны, остановись и сообщи, не трать кредиты. '
                  f'Движение: {prompt}')
    if mode=='agent' and not pending:
        if quota(pg):raise AccountUnavailable('кредиты закончились (видно до отправки)')
        if not _attach(pg,image):
            _diagnose(pg,name,'не удалось прикрепить фото через «+» → «Просмотреть локальные файлы»')
            taskpath.unlink(missing_ok=True)
            raise ManualRequired('Не удалось прикрепить фото («+» → «Просмотреть локальные файлы»). Скриншот отправлен; кредиты не потрачены.')
        inp=_first(pg,INPUT_SELECTORS)
        if inp is None:raise BrowserLayoutError('Не найдено поле агента')
        inp.fill(instructions)
        atomic(taskpath,{'account':name,'url':pg.url,'status':'submitted'})
        _submit(pg,inp) # exactly one submission, no expensive automatic retries
    elif mode=='agent' and pending.get('status')=='submitted':
        pass # task already sent: wait, don't submit twice
    else:
        if not pending:
            try:_attach(pg,image)
            except Exception:pass
        inp=_first(pg,INPUT_SELECTORS,2000)
        if inp is not None and not pending:inp.fill(instructions)
        human(f'{name}: видео. Задание заполнено на главном экране: {instructions}\n'
              f'Фото на сервере: {Path(image).resolve()}. Запусти один раз, дождись готовности, нажми «Выполнено».',
              on_tick=lambda:atomic(taskpath,{'account':name,'url':ctx.pages[-1].url,'status':'awaiting-human'}))
    page=ctx.pages[-1];atomic(taskpath,{'account':name,'url':page.url,'status':'submitted'})
    if quota(page):taskpath.unlink(missing_ok=True);raise AccountUnavailable('кредиты закончились')
    end=time.monotonic()+deadline_s
    while time.monotonic()<end:
        page=ctx.pages[-1]
        atomic(taskpath,{'account':name,'url':page.url,'status':'submitted'})
        if challenge(page) or _login_visible(page):
            page.bring_to_front();human('Пройди проверку / вход кнопкой «Открыть браузер», затем «Выполнено».')
        if quota(page):taskpath.unlink(missing_ok=True);raise AccountUnavailable('кредиты закончились')
        src=_find_video_src(page)
        if src:
            target=Path(out_path);target.parent.mkdir(parents=True,exist_ok=True)
            tmp=target.with_suffix('.part.mp4')
            if src.startswith('blob:'):
                data=page.evaluate('''async src => { const b=await (await fetch(src)).arrayBuffer();
                  let x=''; const u=new Uint8Array(b); for(let i=0;i<u.length;i+=8192)x+=String.fromCharCode(...u.subarray(i,i+8192));return btoa(x);}''',src)
                tmp.write_bytes(base64.b64decode(data))
            else:
                resp=ctx.request.get(src,timeout=60000)
                if not resp.ok:raise ManualRequired('Не удалось скачать готовый клип; генерацию не повторяю')
                tmp.write_bytes(resp.body())
            tmp.replace(target) # keep download even when budget mismatch
            inspect_video(target)
            taskpath.unlink(missing_ok=True);_persist(name,ctx)
            return target
        page.wait_for_timeout(1000)
    # Клип не нашёлся: скриншот + ответ агента в Telegram (бесплатно), чтобы было видно, что случилось.
    page=ctx.pages[-1]
    _diagnose(page,name,'клип не найден за отведённое время')
    reply=' '.join(_answer_text(page).split())[-400:]
    raise ManualRequired('Готовый клип не найден. Задача сохранена, платную генерацию повторно не запускаю.'
                         +(f' Ответ агента: «{reply}»' if reply else '')
                         +f' Ссылка: {page.url}')


def animate(image_path,prompt,out_path=None):
    target=Path(out_path or 'work/anim.mp4')
    if target.exists():inspect_video(target);return target
    key=hashlib.sha256(Path(image_path).read_bytes()+prompt.encode()).hexdigest()
    pending=read(SESSIONS/f'task-{key}.json')
    owner=pending.get('account')
    if (os.environ.get('GENSPARK_VIDEO_MODE') or 'agent')=='agent' and pending.get('status')!='submitted':
        owner=None # неотправленная задача не держит аккаунт: ротация с account-1
    if owner:
        match=next(((n,s) for n,s in accounts_all() if n==owner),None)
        if not match:raise ManualRequired('Аккаунт начатого клипа недоступен; не отправляю сцену другому аккаунту повторно.')
        try:return _animate_one(*match,image_path,prompt,None,target,video_timeout_s())
        except AccountUnavailable as e:
            # Genspark ответил «кредиты закончились» — клип не делался, значит повтор на другом аккаунте не дубль.
            _state['blocked'].add(owner);log(f'{owner}: {e}; сцена уходит на следующий аккаунт.')
    return _rotate(lambda name,st:_animate_one(name,st,image_path,prompt,None,target,video_timeout_s()))


def login_accounts():
    acc=accounts()
    with_sessions=sum(1 for _,st in acc if st.get('cookies'))
    log(f'Слотов аккаунтов: {len(acc)}; с сохранёнными сессиями: {with_sessions}; '
        f'GENSPARK_ACCOUNTS={os.environ.get("GENSPARK_ACCOUNTS") or "(не задано — использован 1)"} '
        '(значение приходит из Telegram, data/settings.json). '
        'Пустые слоты по очереди откроют браузер для ручного входа; остановиться можно в любой момент.')
    saved=[];failed=[];stop_note='';requested=len(acc);no_reply=0
    for name,st in acc:
        got=None;reason='';user_stopped=False
        for attempt in range(1,retries_n()+2):
            try:
                ctx=_context(name,st);pg=_ready(name,ctx,base_url());got=(ctx,pg);break
            except UserStopped:
                user_stopped=True;break
            except (PageDead,WaitTimeout) as m:
                reason=str(m);break
            except Exception as e:
                reason=type(e).__name__
                log(f'{name}: вкладка не открылась ({reason}); попытка {attempt}/{retries_n()+1}.')
        if got:
            _persist(name,got[0]);saved.append(name);no_reply=0
            log(f'{name}: сессия жива и сохранена.')
        elif user_stopped:
            stop_note=f'Остановились на {name} по твоей кнопке. Уже вошедшие сохранены.'
            log(stop_note)
            break
        else:
            failed.append((name,reason or 'неизвестная ошибка'))
            log(f'{name}: {reason or "не удалось"}; перехожу к следующему аккаунту.')
            if not st.get('cookies') and reason.startswith('Время ручного'):
                no_reply+=1
                if no_reply>=2:
                    stop_note=(f'Два подтверждения подряд остались без ответа — остановился на {name}, '
                               'чтобы не жечь время runner. Продолжить — снова «Вход в аккаунты»: вошедшие пропустятся автоматически.')
                    log(stop_note)
                    break
    report=f'🔐 Вход: слотов {requested}, сохранено сессий {len(saved)}.'
    if failed:report+='\nНе удалось: '+'; '.join(f'{n} — {r}' for n,r in failed)+'.'
    if stop_note:report+='\n'+stop_note
    elif failed:report+='\nЧтобы добрать недошедшие — снова «Вход в аккаунты»: вошедшие пропустятся автоматически.'
    log(report);notify(report)
    print('Сессии сохранены приватно; резерв будет зашифрован. Сами значения не выводятся.')
    if not saved:raise ManualRequired('Ни одна сессия не сохранена; смотри логи выше.')


if __name__=='__main__':
    import sys
    if len(sys.argv)>1 and sys.argv[1]=='login':login_accounts()
    else:print(chat(' '.join(sys.argv[1:]) or 'Скажи привет.',as_json=False))
