#!/usr/bin/env python3
import sys
import genspark_client as gc
import runner_state

try:
    if len(sys.argv)>1 and sys.argv[1]=='login':gc.login_accounts()
    else:
        import main
        main.cmd_create()
finally:
    gc.close()
    runner_state.snapshot()
