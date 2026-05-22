#!/bin/zsh

export HTTPS_PROXY="http://127.0.0.1:7897"
export HTTP_PROXY="http://127.0.0.1:7897"
export ALL_PROXY="http://127.0.0.1:7897"
export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"

/Users/sabrina0x/accumulation-radar/.venv/bin/python \
  /Users/sabrina0x/accumulation-radar/scripts/local_status_monitor.py \
  >> /Users/sabrina0x/accumulation-radar/monitor_status.log 2>&1
