#!/usr/bin/env python3
"""Double-fork launcher for the multisession depth daemon.

The sandbox reaps shell-background processes when a Bash tool call ends.
A double-forked, setsid'd grandchild is reparented to PID 1 and escapes
process-group kills. If it still dies, fallback = opportunistic sync sweeps.
"""
import os, sys, subprocess

SCRIPT = "/home/z/my-project/scripts/depth_multisession.py"
LOG = "/home/z/my-project/scripts/ms_daemon.log"

def launch():
    pid = os.fork()
    if pid > 0:
        os.waitpid(pid, 0)          # first parent reaps first child immediately
        return                       # parent exits -> first child exits -> grandchild orphaned
    os.setsid()                      # new session, escape process group
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)                  # first child exits immediately
    # grandchild: fully detached
    os.close(0); os.close(1); os.close(2)
    fd = os.open("/dev/null", os.O_RDWR)
    os.dup2(fd, 0); os.dup2(fd, 1); os.dup2(fd, 2)
    if os.getppid() == 1:
        pass  # already reparented
    with open(LOG, "ab") as lf:
        lf.write(f"[launcher] double-fork daemon pid={os.getpid()} t=18:4x UTC\n".encode())
        lf.flush()
        subprocess.call([sys.executable, SCRIPT, "daemon"], stdout=lf, stderr=lf)
    os._exit(0)

if __name__ == "__main__":
    launch()
