#!/usr/bin/env python3
"""Encrypted Actions checkpoints. Never store plaintext sessions in Git/artifacts/logs."""
import hashlib
import base64
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path
import requests
from cryptography.fernet import Fernet,InvalidToken

ROOT=Path(__file__).resolve().parent
PRIVATE=ROOT/'private-runtime'
ARTIFACT='freefire-state'


def cipher():
    key=os.environ.get('SESSION_BACKUP_KEY','').strip()
    if not key:raise RuntimeError('Добавь SESSION_BACKUP_KEY в GitHub Secrets (ключ Fernet)')
    try:return Fernet(key.encode())
    except ValueError:
        if len(key)<32:raise RuntimeError('SESSION_BACKUP_KEY: ключ Fernet или случайный секрет длиной >=32 символов') from None
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def snapshot():
    f=cipher()
    members=[]
    for folder in (ROOT/'data/jobs',PRIVATE/'sessions'):
        if folder.exists():members.extend(p for p in folder.rglob('*') if p.is_file() and not p.is_symlink())
    if not members:return
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w:gz') as tar:
        for p in members:tar.add(p,arcname=str(p.relative_to(ROOT)),recursive=False)
    out=ROOT/'state-backup';out.mkdir(exist_ok=True)
    (out/'state.enc').write_bytes(f.encrypt(buf.getvalue()))
    print('Зашифрованный резерв прогресса готов; сессии в лог не выводятся.')


def unpack(data):
    try:raw=cipher().decrypt(data)
    except InvalidToken:raise RuntimeError('Резерв не расшифрован: неверный SESSION_BACKUP_KEY') from None
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tar:
        for m in tar.getmembers():
            name=Path(m.name)
            if m.isdir():continue
            if not m.isfile() or name.is_absolute() or '..' in name.parts:
                raise RuntimeError('Недопустимый путь в резерве')
            if not (m.name.startswith('data/jobs/') or m.name.startswith('private-runtime/sessions/')):
                raise RuntimeError('Недопустимое содержимое резерва')
            p=ROOT/name;p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            p.write_bytes(tar.extractfile(m).read());os.chmod(p,0o600)


def restore():
    cipher() # validate before doing network work
    repo=os.environ.get('GITHUB_REPOSITORY',''); token=os.environ.get('GITHUB_TOKEN','')
    if not repo or not token:
        print('Вне Actions: используется локальное состояние.');return
    headers={'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json'}
    branch=os.environ.get('GITHUB_REF_NAME','main')
    chosen=None
    for page in range(1,11):
        r=requests.get(f'https://api.github.com/repos/{repo}/actions/artifacts',headers=headers,
                       params={'per_page':100,'page':page},timeout=60)
        r.raise_for_status(); arr=r.json().get('artifacts',[])
        for a in arr:
            if a.get('name','').startswith(ARTIFACT+'-') and a.get('workflow_run',{}).get('head_branch') == branch:
                chosen=a;break
        if chosen or len(arr)<100:break
    if not chosen:
        print('Предыдущего резерва в этой ветке нет — первый запуск.');return
    if chosen.get('expired'):
        raise RuntimeError('Последний резерв истёк. Нельзя обещать возобновление; нужен новый ролик.')
    r=requests.get(f'https://api.github.com/repos/{repo}/actions/artifacts/{chosen["id"]}/zip',
                   headers=headers,timeout=120)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        if 'state.enc' not in z.namelist():raise RuntimeError('Не найден state.enc')
        unpack(z.read('state.enc'))
    print('Прогресс и зашифрованные сессии восстановлены из предыдущего запуска.')


def vnc_password():
    from cryptography.hazmat.primitives.ciphers import Cipher,modes
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    pw=os.environ.get('REMOTE_DESKTOP_PASSWORD','')
    if len(pw)<8 or not pw.isascii():
        raise RuntimeError('REMOTE_DESKTOP_PASSWORD: минимум 8 ASCII-символов')
    key=bytes(int(f'{v:08b}'[::-1],2) for v in [23,82,107,6,35,78,88,7])
    data=Cipher(TripleDES(key*3),modes.ECB()).encryptor().update(pw.encode()[:8])
    PRIVATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    p=PRIVATE/'vnc.pass';p.write_bytes(data);os.chmod(p,0o600)


def remote_links():
    import subprocess
    d=json.loads(subprocess.check_output(['tailscale','status','--json']))
    host=d['Self']['DNSName'].rstrip('.')
    if not host.endswith('.ts.net'):raise RuntimeError('Не получено приватное DNS-имя Tailscale')
    screen=f'https://{host}/vnc.html?autoconnect=1&resize=scale&quality=3&compression=9'
    control=f'https://{host}:8443/'
    env=os.environ.get('GITHUB_ENV')
    if env:
        with open(env,'a') as f:f.write(f'GENSPARK_REMOTE_SCREEN={screen}\nGENSPARK_REMOTE_CONTROL={control}\n')
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary,'a') as f:
            f.write(f'## Ручной вход (только внутри Tailscale)\n[Экран]({screen}) · [Продолжить / остановить]({control})\n')
    print('Приватные ссылки добавлены в Summary запуска; пароль только в Secret.')


if __name__=='__main__':
    cmd=sys.argv[1] if len(sys.argv)>1 else ''
    {'restore':restore,'snapshot':snapshot,'remote-links':remote_links,'validate-key':cipher,'vnc-password':vnc_password}[cmd]()
