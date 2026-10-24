"""Final editing bridge: staged server-side assets -> Genspark -> verified final MP4.
No automatic CAPTCHA interaction; the bot submits the edit exactly once and waits for the export.
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
    if pending.get('status')!='submitted':
        # Задача ещё не отправлена (или прошлый запуск ждал ручных действий) — бот отправляет сам, ровно один раз.
        if gc.quota(page):ledger.unlink(missing_ok=True);raise gc.AccountUnavailable('кредиты закончились (видно до отправки)')
        # Genspark принимает несколько файлов; если нет — архив одним файлом.
        if not (gc._attach(page,files) or gc._attach(page,archive)):
            gc._diagnose(page,name,'монтаж: не удалось прикрепить материалы')
            ledger.unlink(missing_ok=True)
            raise gc.ManualRequired('Не удалось прикрепить материалы монтажа. Скриншот отправлен; кредиты не потрачены.')
        inp=gc._first(page,gc.INPUT_SELECTORS,5000)
        if inp is None:raise gc.BrowserLayoutError('Не найдено поле ввода для монтажа; ничего не отправлено')
        inp.fill(instructions)
        before=set(_media_links(page))
        atomic(ledger,{'account':name,'url':page.url,'status':'submitted','before':sorted(before)})
        if not gc._submit(page,inp):
            gc._diagnose(page,name,'монтаж: запрос не отправился')
            ledger.unlink(missing_ok=True)
            raise gc.BrowserLayoutError('Задание монтажа не отправилось (текст остался в поле). Скриншот отправлен; кредиты не потрачены.')
        gc.log(f'{name}: монтаж отправлен один раз; жду экспорт до {gc.number("GENSPARK_EDITOR_TIMEOUT")} с.')
        atomic(ledger,{'account':name,'url':page.url,'status':'submitted','before':sorted(before)})
    else:
        before=set(pending.get('before') or [])
        gc.log(f'{name}: монтаж уже отправлен ранее — жду результат, повторно не отправляю.')
    page=ctx.pages[-1]
    if gc.quota(page):
        ledger.unlink(missing_ok=True);raise gc.AccountUnavailable('кредиты закончились')
    end=time.monotonic()+gc.number('GENSPARK_EDITOR_TIMEOUT')
    next_beat=time.monotonic()+120
    while time.monotonic()<end:
        page=ctx.pages[-1]
        atomic(ledger,{'account':name,'url':page.url,'status':'submitted','before':sorted(before)})
        if gc.challenge(page) or gc._login_visible(page):
            page.bring_to_front();gc.human('Пройди проверку / вход кнопкой «Открыть браузер», затем «Выполнено». Монтаж повторно не отправляется.')
        if gc.quota(page):ledger.unlink(missing_ok=True);raise gc.AccountUnavailable('кредиты закончились')
        if time.monotonic()>=next_beat:
            gc.log(f'{name}: монтаж ещё идёт ({int(gc.number("GENSPARK_EDITOR_TIMEOUT")-(end-time.monotonic()))} с)...')
            next_beat=time.monotonic()+120
        # Готовый экспорт = новая ссылка на видео, которой не было до отправки (прикреплённые клипы не считаются).
        fresh=[u for u in _media_links(page) if u not in before]
        if fresh:
            from urllib.parse import urljoin
            try:
                _download(page,urljoin(page.url,fresh[-1]),target)
                verify_final(target,manifest['total_duration'])
                atomic(ledger,{'account':name,'url':page.url,'status':'complete'})
                gc._persist(name,ctx);return target
            except Exception as e:
                # Это не финал (наш прикреплённый клип, wav, zip, промежуточное видео) — ждём дальше, не перезаказываем.
                gc.log(f'{name}: найден файл, но это не финал ({str(e)[:120]}); жду дальше.')
                before.add(fresh[-1]);target.unlink(missing_ok=True)
        page.wait_for_timeout(3000)
    gc._diagnose(ctx.pages[-1],name,'монтаж: экспорт не найден за отведённое время')
    reply=' '.join(gc._answer_text(ctx.pages[-1]).split())[-400:]
    raise gc.ManualRequired('Готовый экспорт монтажа не найден; задача сохранена, дубль не запускается.'
                            +(f' Ответ агента: «{reply}»' if reply else '')+f' Ссылка: {ctx.pages[-1].url}')


_MEDIA_JS=r"""() => {
  const out=[];const add=u=>{if(u&&!u.startsWith('data:')&&!out.includes(u))out.push(u);};
  const sel=window.__finalSel;
  if(sel)document.querySelectorAll(sel).forEach(e=>add(e.href||e.src||''));
  document.querySelectorAll('video').forEach(v=>add(v.currentSrc||v.src||v.querySelector('source')?.src||''));
  document.querySelectorAll('a[href],source[src]').forEach(a=>{const u=a.href||a.src||'';if(/\.(mp4|webm|mov)(\?|#|$)/i.test(u)||a.hasAttribute('download'))add(u);});
  (document.documentElement.innerHTML.match(/https?:\/\/[^"'\s<>]+?\.(?:mp4|webm|mov)(?:\?[^"'\s<>]*)?/gi)||[]).forEach(u=>add(u.replace(/&amp;/g,'&')));
  return out;
}"""


def _media_links(page):
    try:
        sel=os.environ.get('GENSPARK_FINAL_DOWNLOAD_SELECTOR') or ''
        if sel:page.evaluate('s=>{window.__finalSel=s}',sel)
        return page.evaluate(_MEDIA_JS) or []
    except Exception:return []


def edit(job_dir,story,audios,durations):
    job_dir=Path(job_dir);target=job_dir/'genspark-final.mp4'
    if target.exists():verify_final(target,sum(durations));return target
    folder,archive,manifest=prepare(job_dir,story,audios,durations)
    ledger=job_dir/'editor-state.json';pending=read(ledger)
    if pending.get('status')!='submitted':
        ledger.unlink(missing_ok=True);pending={} # не отправленная задача не держит аккаунт
    if pending.get('account'):
        owner=next(((n,st) for n,st in gc.accounts_all() if n==pending['account']),None)
        if owner is None:raise gc.ManualRequired('Аккаунт начатого монтажа недоступен; новая задача не создаётся')
        try:return _edit_one(*owner,folder,archive,manifest,ledger,target)
        except gc.AccountUnavailable as e:
            gc._state['blocked'].add(owner[0]);gc.log(f'{owner[0]}: {e}; монтаж уходит на следующий аккаунт.')
    return gc._rotate(lambda n,st:_edit_one(n,st,folder,archive,manifest,ledger,target))
