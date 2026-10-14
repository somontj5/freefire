#!/usr/bin/env python3
"""Local regression tests: synthetic data only; NO real account, captcha or paid generation."""
import base64
import contextlib
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('GENSPARK_HEADLESS','1')
import runtime_settings as config
import genspark_client as gc
import runner_state as state
import remote_control as control
import main


class LogicTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.old=(config.PATH,gc.PRIVATE,gc.SESSIONS,state.ROOT,state.PRIVATE,control.PRIVATE)
        config.PATH=self.root/'data/settings.json';gc.PRIVATE=self.root/'private-runtime';gc.SESSIONS=gc.PRIVATE/'sessions'
        state.ROOT=self.root;state.PRIVATE=gc.PRIVATE;control.PRIVATE=gc.PRIVATE
        self.env=patch.dict(os.environ,{'SESSION_BACKUP_KEY':'TestOnlyHighEntropySecret0000000000000000000','GENSPARK_ACCOUNTS':'1'},clear=False)
        self.env.start();gc._state={'next':0,'blocked':set()}
    def tearDown(self):
        gc.close();self.env.stop()
        config.PATH,gc.PRIVATE,gc.SESSIONS,state.ROOT,state.PRIVATE,control.PRIVATE=self.old
        self.tmp.cleanup()
    def test_defaults_and_configuration_validation(self):
        self.assertEqual(gc.anim_duration_s(),3)
        for key,val in [('GENSPARK_ACCOUNTS','27'),('GENSPARK_MANUAL_WAIT','600'),('GENSPARK_ANIM_DURATION','3')]:config.save(key,val)
        self.assertEqual(config.get('GENSPARK_ACCOUNTS'),'27')
        for key,val in [('TG_TOKEN','private'),('RESUME','../../etc/passwd'),('GENSPARK_URL','https://attacker.test'),('GENSPARK_VIDEO_MODE','auto')]:
            with self.assertRaises(ValueError):config.save(key,val)
    def test_cookie_names_never_printed_and_non_genspark_filtered(self):
        bad={'name':'www.genspark.ai\tFALSE\t/\tTRUE\t123\tSECRET','domain':'.genspark.ai','value':'sensitive'}
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture),self.assertRaises(ValueError):gc._sanitize_cookie(bad)
        self.assertNotIn('SECRET',capture.getvalue());self.assertNotIn('sensitive',capture.getvalue())
        self.assertIsNone(gc._sanitize_cookie({'name':'PREF','value':'cookie','domain':'.youtube.com'}))
    def test_no_account_slot_limit(self):
        st={'cookies':[],'origins':[]}
        with patch.dict(os.environ,{'GENSPARK_STORAGE_700':json.dumps(st),'GENSPARK_STORAGES':''}):
            self.assertIn('account-700',[n for n,_ in gc.accounts_all()])
    def test_round_robin_and_quota_failover(self):
        seq=[]
        with patch.object(gc,'accounts',return_value=[('a',{}),('b',{})]):
            for _ in range(3):gc._rotate(lambda n,s:seq.append(n) or n)
            self.assertEqual(seq,['a','b','a'])
            gc._state['next']=0
            def request(n,s):
                if n=='a':raise gc.AccountUnavailable()
                return n
            self.assertEqual(gc._rotate(request),'b');self.assertIn('a',gc._state['blocked'])
    def test_manual_pause_not_retried_on_another_account(self):
        calls=[]
        with patch.object(gc,'accounts',return_value=[('a',{}),('b',{})]):
            def fn(n,s):calls.append(n);raise gc.ManualRequired('test')
            with self.assertRaises(gc.ManualRequired):gc._rotate(fn)
        self.assertEqual(calls,['a'])
    def test_encrypted_backup_roundtrip_and_wrong_key(self):
        secret='SYNTHETIC_SESSION_SECRET'
        control.atomic(gc.SESSIONS/'account-1.json',{'cookies':[{'value':secret}]})
        job=self.root/'data/jobs/20261014-080000';job.mkdir(parents=True)
        (job/'state.json').write_text('{"status":"paused"}')
        state.snapshot();data=(self.root/'state-backup/state.enc').read_bytes()
        self.assertNotIn(secret.encode(),data)
        (job/'state.json').unlink();state.unpack(data)
        self.assertTrue((job/'state.json').exists())
        with patch.dict(os.environ,{'SESSION_BACKUP_KEY':'DifferentTestOnlySecret00000000000000000000'}),self.assertRaises(RuntimeError):state.unpack(data)
    def test_remote_approval_requires_matching_nonce(self):
        import requests
        srv=http.server.ThreadingHTTPServer(('127.0.0.1',0),control.Handler)
        t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
        try:
            url=f'http://127.0.0.1:{srv.server_port}/'
            control.atomic(gc.PRIVATE/'request.json',{'id':'test-nonce','message':'manual test'})
            self.assertEqual(requests.get(url).status_code,200)
            self.assertEqual(requests.post(url,data={'id':'wrong','action':'continue'}).status_code,409)
            requests.post(url,data={'id':'test-nonce','action':'continue'})
            self.assertEqual(control.read(gc.PRIVATE/'approval.json')['action'],'continue')
        finally:srv.shutdown();srv.server_close()
    def test_bot_all_settings_and_authorization(self):
        messages=[]
        with patch.object(main,'TG_CHAT','111'),patch.object(main,'tg',side_effect=lambda m,**kw:messages.append(kw)):
            main.handle_message({'chat':{'id':111},'text':'/параметр GENSPARK_ACCOUNTS 19'})
            main.handle_message({'chat':{'id':222},'text':'/параметр GENSPARK_ACCOUNTS 30'})
            self.assertEqual(config.get('GENSPARK_ACCOUNTS'),'19')
            main.handle_message({'chat':{'id':111},'text':'/параметр TG_TOKEN secret'})
            self.assertNotIn('TG_TOKEN',config.load())
    def test_add_account_after_existing_session(self):
        control.atomic(gc.SESSIONS/'account-1.json',{'cookies':[],'origins':[]})
        with patch.dict(os.environ,{'GENSPARK_ACCOUNTS':'2','GENSPARK_REMOTE':'1','GENSPARK_REMOTE_CONTROL':'https://test.ts.net:8443/'}):
            names=[n for n,_ in gc.accounts()]
            self.assertEqual(names,['account-1','account-2'])
    def test_small_valid_media_not_discarded(self):
        p=self.root/'tiny.png'
        from PIL import Image
        Image.new('RGB',(8,8)).save(p)
        self.assertLess(p.stat().st_size,5000)
        self.assertTrue(main._solid(p,5000))
    def test_json_arrays_supported(self):
        self.assertEqual(gc._parse_json('```json\n[{"ok":true}]\n```'),[{'ok':True}])
    def test_latest_response_selected_in_chromium(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b=pw.chromium.launch(headless=True)
            try:
                p=b.new_page();p.set_content('<div class="prose">old long response</div><div class="prose">new</div>')
                self.assertEqual(gc._answer_text(p),'new')
                p.set_content('<body>Performing security verification</body>')
                self.assertTrue(gc.challenge(p))
            finally:b.close()
    def test_checkpoint_metadata_and_corruption(self):
        old_jobs=main.JOBS;old_ck=main.CK
        main.JOBS=self.root/'data/jobs';main.CK={'vid':'20261014-080000','scenes':{}}
        try:
            main.ck_put('20261014-080000',story={'scenes':[]},facts='must survive',sources=[{'url':'test'}])
            main.ck_put('20261014-080000',i=0)
            st=main.ck_load('20261014-080000');self.assertEqual(st['facts'],'must survive')
            self.assertEqual(st['sources'],[{'url':'test'}])
            p=main.JOBS/'20261014-080000/s00_anim.mp4';p.write_bytes(b'not a video'*10000)
            self.assertIsNone(main.ck_artifact('20261014-080000',0,'anim'))
        finally:main.JOBS=old_jobs;main.CK=old_ck
    def test_video_budget_validation_real_ffmpeg(self):
        p=self.root/'test.mp4'
        subprocess.run(['ffmpeg','-y','-f','lavfi','-i','color=c=blue:s=480x854:r=24:d=3',
                        '-c:v','libx264','-preset','ultrafast',str(p)],check=True,capture_output=True)
        self.assertTrue(gc.inspect_video(p))
        with patch.dict(os.environ,{'GENSPARK_ANIM_DURATION':'4'}),self.assertRaises(gc.BudgetMismatch):gc.inspect_video(p)


if __name__=='__main__':unittest.main(verbosity=2)
