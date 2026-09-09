#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 tests/test_taifex.py
python3 tests/test_taifut.py
python3 tests/test_update_daily.py
python3 tests/verify.py
