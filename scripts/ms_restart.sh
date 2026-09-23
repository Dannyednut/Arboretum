#!/bin/bash
# Restart multisession collection after daemon death (Sep 2 00:07 UTC).
# 1) immediate asia-night window W03_00u_asia (00:20 still in 00-07 session)
# 2) daemon for remaining windows: W04 04u asia, W05 08u eu, W06 12u eu
cd /home/z/my-project/scripts
echo "=== restart $(date -u +%FT%TZ) ===" >> ms_daemon.log
python3 depth_multisession.py now W03_00u_asia >> ms_daemon.log 2>&1
python3 depth_multisession.py daemon 4 4,8,12 >> ms_daemon.log 2>&1
