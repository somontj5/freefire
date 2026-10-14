"""Small Telegram inline menu. Navigation edits one message; no secret input."""
import json
import os
from pathlib import Path
import requests
import runtime_settings as config

STATE=Path(__file__).resolve().parent/'data/telegram-ui.json'


def _state():
    try:return json.loads(STATE.read_text())
    except (OSError,ValueError):return {}


def _save(d):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    tmp=STATE.with_suffix('.tmp');tmp.write_text(json.dumps(d,ensure_ascii=False));tmp.replace(STATE)


def allowed(api,cq):
    message=cq.get('message',cq)
    chat=message.get('chat',{})
    if str(chat.get('id'))!=str(api.TG_CHAT):return False
    if chat.get('type') in ('group','supergroup'):
        return str(cq.get('from',{}).get('id',''))==os.environ.get('TG_ADMIN_ID','') and bool(os.environ.get('TG_ADMIN_ID'))
    return True


def button(text,data):return {'text':text,'callback_data':data}
def keyboard(rows):return {'inline_keyboard':rows}


def render(api,chat,mid,text,rows):
    if mid:
        api.tg_safe('editMessageText',chat_id=chat,message_id=mid,text=text,reply_markup=keyboard(rows))
    else:api.tg('sendMessage',chat_id=chat,text=text,reply_markup=keyboard(rows))


def home(api,chat,mid=None):
    render(api,chat,mid,'🎬 Freefire — управление\nВсё обрабатывается на серверах GitHub. Телефон — только пульт.',[
        [button('🎬 Создать / продолжить','m:make'),button('📝 Свой сценарий','m:script')],
        [button('💡 Тема','m:topic'),button('🔐 Вход в аккаунты','m:login')],
        [button('⚙️ Настройки','m:settings'),button('📊 Статус','m:status')],
        [button('📈 Статистика','m:stats')]])


PICKS={
 'GENSPARK_ACCOUNTS':['1','2','3','4','all'], 'GENSPARK_ANIM_DURATION':['3','4','5'],
 'GENSPARK_ANIM_QUALITY':['480p','720p'], 'GENSPARK_ANIM_MODEL':['Grok lite'],
 'MAX_ANIMATE':['3','6','10','20'], 'ANIMATE':['1','0'],
 'GENSPARK_MANUAL_WAIT':['300','600','900'], 'GENSPARK_VIDEO_MODE':['agent','manual'],
 'GENSPARK_FINAL_EDITOR':['genspark','local'], 'GENSPARK_ALLOW_FALLBACK':['0','1'],
}
LABELS={'GENSPARK_ACCOUNTS':'Аккаунты','GENSPARK_ANIM_DURATION':'Секунды','GENSPARK_ANIM_QUALITY':'Разрешение',
        'GENSPARK_ANIM_MODEL':'Модель видео','MAX_ANIMATE':'Оживляемых сцен','ANIMATE':'Оживление',
        'GENSPARK_MANUAL_WAIT':'Ожидание подтверждения','GENSPARK_VIDEO_MODE':'Запуск видео',
        'GENSPARK_FINAL_EDITOR':'Монтаж','GENSPARK_ALLOW_FALLBACK':'API-запасной путь'}


def settings_menu(api,chat,mid):
    rows=[[button(f'{label}: {config.get(key)}','k:'+key)] for key,label in LABELS.items()]
    rows += [[button('🛠 Все параметры','m:advanced:0')],[button('⬅️ Главное меню','m:home')]]
    render(api,chat,mid,'⚙️ Настройки следующего запуска\nЭкономный профиль: Grok lite · 3 с · 480p',rows)


def ask_text(api,chat,mid,kind,key=None):
    d=_state();d[str(chat)]={'message_id':mid,'kind':kind,'key':key};_save(d)
    prompts={'script':'Пришли готовый сценарий обычным сообщением.', 'topic':'Пришли тему обычным сообщением.',
             'setting':f'Пришли новое значение для {key}. Пароли и куки здесь не принимаются.'}
    render(api,chat,mid,prompts[kind],[[button('Отмена','m:home')]])


def callback(api,cq):
    data=cq.get('data','')
    if not data.startswith(('m:','k:','v:','r:','cf:')):return False
    qid=cq['id']
    if not allowed(api,cq):
        api.tg_safe('answerCallbackQuery',callback_query_id=qid,text='Нет доступа');return True
    chat=str(cq['message']['chat']['id']);mid=cq['message']['message_id']
    if data.startswith('cf:'):
        from remote_control import PRIVATE,read,atomic
        parts=data.split(':');request=read(PRIVATE/'request.json')
        if len(parts)!=3 or request.get('id')!=parts[1] or parts[2] not in ('continue','pause'):
            api.tg_safe('answerCallbackQuery',callback_query_id=qid,text='Подтверждение уже закрыто');return True
        atomic(PRIVATE/'approval.json',{'id':parts[1],'action':parts[2]})
        api.tg_safe('answerCallbackQuery',callback_query_id=qid,text='Принято')
        api.tg_safe('editMessageReplyMarkup',chat_id=chat,message_id=mid,reply_markup=keyboard([]));return True
    api.tg_safe('answerCallbackQuery',callback_query_id=qid)
    try:
        if data=='m:home':
            d=_state();d.pop(chat,None);_save(d);home(api,chat,mid)
        elif data=='m:settings':settings_menu(api,chat,mid)
        elif data in ('m:script','m:topic'):ask_text(api,chat,mid,data.split(':')[1])
        elif data=='m:make':
            api.dispatch_create();render(api,chat,mid,'▶️ Запуск отправлен. При проверке придут кнопки прямо сюда.',[[button('⬅️ Меню','m:home')]])
        elif data=='m:login':
            dispatch_login(api);render(api,chat,mid,'🔐 Запущен серверный вход. Жди кнопку «Открыть браузер» здесь, в Telegram.',[[button('⬅️ Меню','m:home')]])
        elif data=='m:status':
            vs=api.load_videos();render(api,chat,mid,f'📊 Роликов: {len(vs)}\nОжидают решения: {sum(v.get("status")=="pending" for v in vs)}',[[button('⬅️ Меню','m:home')]])
        elif data=='m:stats':render(api,chat,mid,api.stats_report()[:3800],[[button('⬅️ Меню','m:home')]])
        elif data.startswith('m:advanced:'):
            page=int(data.rsplit(':',1)[1]);keys=list(config.DEFAULTS);subset=keys[page*6:page*6+6]
            rows=[[button(f'{k}: {config.get(k) or "по умолчанию"}','k:'+k)] for k in subset]
            nav=[]
            if page:nav.append(button('◀️',f'm:advanced:{page-1}'))
            if (page+1)*6<len(keys):nav.append(button('▶️',f'm:advanced:{page+1}'))
            if nav:rows.append(nav)
            rows.append([button('⬅️ Настройки','m:settings')]);render(api,chat,mid,'🛠 Все несекретные параметры',rows)
        elif data.startswith('k:'):
            key=data[2:]
            if key not in config.DEFAULTS:raise ValueError('Неизвестный параметр')
            rows=[[button(value,f'v:{key}:{value}')] for value in PICKS.get(key,[])]
            rows += [[button('✏️ Своё значение','m:custom:'+key)], [button('↩️ Сбросить','r:'+key)],[button('⬅️ Настройки','m:settings')]]
            render(api,chat,mid,f'{LABELS.get(key,key)}\nСейчас: {config.get(key)}',rows)
        elif data.startswith('m:custom:'):ask_text(api,chat,mid,'setting',data.split(':',2)[2])
        elif data.startswith('v:'):
            _,key,value=data.split(':',2);config.save(key,value);settings_menu(api,chat,mid)
        elif data.startswith('r:'):config.reset(data[2:]);settings_menu(api,chat,mid)
    except Exception:
        render(api,chat,mid,'Не удалось выполнить действие. Проверь настройку или повтори после текущей задачи.',[[button('⬅️ Меню','m:home')]])
    return True


def text(api,m):
    if not allowed(api,m):return False
    chat=str(m['chat']['id']);d=_state();pending=d.get(chat)
    value=(m.get('text') or '').strip()
    if not pending or not value or value.startswith('/'):return False
    mid=pending['message_id']
    try:
        kind=pending['kind']
        if kind=='setting':config.save(pending['key'],value);settings_menu(api,chat,mid)
        elif kind=='script':
            if len(value)<80:raise ValueError('Пришли сценарий минимум из пары предложений (80 символов).')
            api.dispatch_create(script=value);render(api,chat,mid,'▶️ Запущено по сценарию. Жди уведомления здесь.',[[button('⬅️ Меню','m:home')]])
        else:api.dispatch_create(topic=value);render(api,chat,mid,'▶️ Запущено по теме.',[[button('⬅️ Меню','m:home')]])
    except ValueError as e:
        render(api,chat,mid,str(e),[[button('Отмена','m:home')]]);return True
    except Exception:
        render(api,chat,mid,'Не удалось запустить. Проверь доступ workflow.',[[button('⬅️ Меню','m:home')]])
    d.pop(chat,None);_save(d);return True


def dispatch_login(api):
    if os.environ.get('GITHUB_ACTIONS')=='true':
        import subprocess
        subprocess.run(['bash','save_data.sh'],check=True)
    repo=os.environ.get('GITHUB_REPOSITORY','');token=os.environ.get('GITHUB_TOKEN','')
    r=requests.post(f'https://api.github.com/repos/{repo}/actions/workflows/export-session.yml/dispatches',
                    headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json'},
                    json={'ref':os.environ.get('GITHUB_REF_NAME','main')},timeout=30)
    if r.status_code!=204:raise RuntimeError('Не удалось запустить вход')


def browser_running():
    if os.environ.get('GITHUB_WORKFLOW')!='bot':return False
    repo=os.environ.get('GITHUB_REPOSITORY');token=os.environ.get('GITHUB_TOKEN')
    if not repo or not token:return False
    try:
        r=requests.get(f'https://api.github.com/repos/{repo}/actions/runs',params={'status':'in_progress','per_page':100},
                       headers={'Authorization':f'Bearer {token}'},timeout=10)
        r.raise_for_status()
        return any(x.get('name') in ('create','export-session') and x.get('head_branch')==os.environ.get('GITHUB_REF_NAME','main') for x in r.json().get('workflow_runs',[]))
    except requests.RequestException:
        return True # don't compete with a possible running browser poller on API failure
