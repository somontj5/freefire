"""Final editing bridge: staged server-side assets -> Genspark -> verified final MP4.
No automatic CAPTCHA interaction; manual confirmation before one site submission.
The real Genspark editor DOM must be checked in the owner's first live session.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import zipfile

import genspark_client as gc
from remote_control import atomic,read


def prepare(job_dir,story,audios,durations):
    job_dir=Path(job_dir); folder=job_dir/'editor-inputs';folder.mkdir(exist_ok=True)
    total=sum(durations)
    assets=[]
    for i,sc in enumerate(story['scenes']):
        source=job_dir/f's{i:02d}_anim.mp4'
        if not source.exists():source=job_dir/f's{i:02d}_img.png'
        if not source.exists():raise ValueError(f'Нет сохранённого материала сцены {i+1}')
        target=folder/source.name;shutil.copyfile(source,target)
        assets.append({'scene':i+1,'file':target.name,'narration':sc['narration'],
                       'duration':durations[i], 'motion_prompt':sc.get('motion_prompt',''),
                       'card':sc.get('card'), 'sfx':sc.get('sfx','none')})
    # Normalize different saved voice containers to WAV before concat.
    voices=[]
    for i,source in enumerate(audios):
        target=folder/f'voice-{i:02d}.wav'
        subprocess.run(['ffmpeg','-y','-i',str(source),'-ac','1','-ar','44100',str(target)],
                       check=True,capture_output=True)
        voices.append(target)
    listing=folder/'voice-concat.txt'
    listing.write_text(''.join(f"file '{p.resolve()}'\n" for p in voices))
    voice=folder/'narration.wav'
    subprocess.run(['ffmpeg','-y','-f','concat','-safe','0','-i',str(listing),'-c','copy',str(voice)],
                   check=True,capture_output=True)
    listing.unlink()
    for p in voices:p.unlink()
    manifest={'title':story['title'],'caption':story.get('caption',''),'total_duration':total,
              'narration':'narration.wav','scenes':assets,
              'editing_instructions':{'music':'Generate instrumental music matching scene moods; quiet under narration',
                                      'subtitles':'Russian, word-synced to narration; no invented speech',
                                      'sfx':'Subtle whooshes/impacts at motivated transitions',
                                      'motion_design':'Animate title/cards, graphics and diagrams when useful; preserve character identity',
                                      'format':'vertical 9:16; export ready MP4 with audio',
                                      'budget':'Use existing clips/images; do not generate paid replacement video clips without approval'}}
    (folder/'project.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    archive=job_dir/'editor-inputs.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in folder.iterdir():
            if p.is_file():z.write(p,p.name)
    return folder,archive,manifest


def verify_final(path,expected_duration):
    r=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],
                     check=True,capture_output=True,text=True)
    d=json.loads(r.stdout);videos=[s for s in d['streams'] if s['codec_type']=='video']
    audio=[s for s in d['streams'] if s['codec_type']=='audio']
    if not videos or not audio:raise gc.BudgetMismatch('В финальном MP4 должны быть видео и звук; материал сохранён')
    v=videos[0];duration=float(d['format']['duration'])
    if abs(v['width']/v['height']-9/16)>.04:
        raise gc.BudgetMismatch('Финальный ролик не вертикальный 9:16; исправь экспорт')
    if abs(duration-expected_duration)>max(2,expected_duration*.2):
        raise gc.BudgetMismatch('Длительность финала сильно отличается от дикторской дорожки; исправь монтаж')
    return d


def _download(page,url,target):
    tmp=target.with_suffix('.part.mp4')
    if url.startswith('blob:'):
        import base64
        data=page.evaluate('''async url=>{const bytes=new Uint8Array(await(await fetch(url)).arrayBuffer());
        let text='';for(let i=0;i<bytes.length;i+=8192)text+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(text);}''',url)
        tmp.write_bytes(base64.b64decode(data))
    else:
        response=page.context.request.get(url,timeout=120000)
        if not response.ok:raise gc.ManualRequired('Готовый финал не скачался; не создавай новую задачу монтажа')
        tmp.write_bytes(response.body())
    tmp.replace(target)


def _edit_one(name,storage,folder,archive,manifest,ledger,target):
    ctx=gc._context(name,storage);pending=read(ledger)
    page=gc._ready(name,ctx,pending.get('url') or gc.base_url());page.bring_to_front()
    files=[p for p in folder.iterdir() if p.is_file()]
    instructions=(f'Используй инструменты Genspark для монтажа видео, генерации музыки, субтитров и моушен-дизайна. '
                  f'Смонтируй вертикальный ролик по приложенным материалам и project.json. '
                  f'Озвучку narration.wav сохрани целиком. Длительность около {manifest["total_duration"]:.1f} с. '
                  'Нарежь готовые клипы под диктора, добавь музыку по настроению, русские субтитры по словам, '
                  'уместные SFX и моушен-дизайн: анимированные заголовки, карточки и графику. '
                  'Не меняй факты и голос диктора. При необходимости можно подготовить отдельные картинки '
                  'для графики только после согласования расходов. Не перегенерируй готовые видео. '
                  'Экспортируй готовый MP4 со звуком, 9:16. Это итоговый монтаж, не отдельная сцена.')
    if not pending:
        fi=page.locator('input[type=file]').first
        if fi.count():
            try:
                if fi.get_attribute('multiple') is not None:fi.set_input_files([str(p) for p in files])
                else:fi.set_input_files(str(archive))
            except Exception:gc.log('Автоприкрепление недоступно; материалы остаются на сервере для ручного выбора.')
        inp=gc._first(page,gc.INPUT_SELECTORS,3000)
        if inp is not None:inp.fill(instructions) # fill only, no surprise paid submission
        atomic(ledger,{'account':name,'url':page.url,'status':'awaiting-approval'})
    gc.human(f'{name}: ФИНАЛЬНЫЙ МОНТАЖ В GENSPARK. '
             f'Проверь, что открыт нужный редактор и прикреплены все материалы из {folder.resolve()} '
             f'или архив {archive.resolve()}. Инструкция: {instructions}\n'
             'Запусти монтаж один раз и дождись готового экспорта. Если задача уже существует — продолжи её. '
             'Только после готовности нажми «Выполнено». Отдельные платные ассеты подтверждай в интерфейсе сайта.',
             on_tick=lambda:atomic(ledger,{'account':name,'url':ctx.pages[-1].url,'status':'awaiting-human'}))
    page=ctx.pages[-1]
    if gc.quota(page):
        ledger.unlink(missing_ok=True);raise gc.AccountUnavailable()
    end=time.monotonic()+gc.number('GENSPARK_EDITOR_TIMEOUT')
    while time.monotonic()<end:
        # Prefer final export download, NOT an arbitrary scene preview <video>.
        selector=os.environ.get('GENSPARK_FINAL_DOWNLOAD_SELECTOR')
        link=page.locator(selector).last if selector else page.locator('a[download][href]').last
        if link.count() and link.is_visible():
            url=link.get_attribute('href')
            if url:
                from urllib.parse import urljoin
                _download(page,urljoin(page.url,url),target)
                verify_final(target,manifest['total_duration'])
                atomic(ledger,{'account':name,'url':page.url,'status':'complete'})
                gc._persist(name,ctx);return target
        page.wait_for_timeout(1000)
    raise gc.ManualRequired('Готовая ссылка экспорта не найдена; задача монтажа сохранена, дубль не запускается. Укажи GENSPARK_FINAL_DOWNLOAD_SELECTOR для реального UI.')


def edit(job_dir,story,audios,durations):
    job_dir=Path(job_dir);target=job_dir/'genspark-final.mp4'
    if target.exists():verify_final(target,sum(durations));return target
    folder,archive,manifest=prepare(job_dir,story,audios,durations)
    ledger=job_dir/'editor-state.json';pending=read(ledger)
    if pending.get('account'):
        owner=next(((n,st) for n,st in gc.accounts_all() if n==pending['account']),None)
        if owner is None:raise gc.ManualRequired('Аккаунт начатого монтажа недоступен; новая задача не создаётся')
        return _edit_one(*owner,folder,archive,manifest,ledger,target)
    return gc._rotate(lambda n,st:_edit_one(n,st,folder,archive,manifest,ledger,target))
