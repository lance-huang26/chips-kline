#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""append-only 歷史檔的讀寫。

  data/history/chips.csv    date,foreign_fut,top10_trader,top10_specific,foreign_opt,dealer_opt
  data/history/prices.csv   date,code,open,high,low,close,change,volume

用 CSV 是因為每天只 append 一行，git diff 一眼看得出當天抓到什麼。
一年約 240 個交易日、三檔股票約 720 行，十年也還是小檔案，不需要輪替。

三個原則：
  * 冪等：同一個 key（chips 是 date，prices 是 date+code）預設不覆寫，
    所以補跑、手動重跑、兩個排程同時跑，結果都一樣。
  * 原子寫入：先寫暫存檔再 os.replace，中途失敗不會留下寫到一半的歷史檔。
  * 永遠保持日期排序，diff 才不會整個檔案跳動。
"""

import csv
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HISTORY = os.path.join(DATA, "history")

CHIPS_CSV = os.path.join(HISTORY, "chips.csv")
PRICES_CSV = os.path.join(HISTORY, "prices.csv")

CHIP_COLS = ["date", "foreign_fut", "top10_trader", "top10_specific",
             "foreign_opt", "dealer_opt"]
PRICE_COLS = ["date", "code", "open", "high", "low", "close", "change", "volume"]


def _read(path, cols):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        missing = [c for c in cols if c not in r]
        if missing:
            raise ValueError("%s 少了欄位 %s" % (path, missing))
    return rows


def _write_atomic(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow({c: r[c] for c in cols})
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ------------------------------------------------------------------ 籌碼
def read_chips():
    rows = _read(CHIPS_CSV, CHIP_COLS)
    for r in rows:
        for c in CHIP_COLS[1:]:
            r[c] = int(r[c])
    return sorted(rows, key=lambda r: r["date"])


def upsert_chips(new_rows, force=False):
    """回傳 (新增筆數, 覆寫筆數, 跳過筆數)。"""
    cur = {r["date"]: r for r in read_chips()}
    added = updated = skipped = 0
    for r in new_rows:
        row = {c: r[c] for c in CHIP_COLS}
        d = row["date"]
        if d not in cur:
            cur[d] = row
            added += 1
        elif force and cur[d] != row:
            cur[d] = row
            updated += 1
        else:
            skipped += 1
    _write_atomic(CHIPS_CSV, CHIP_COLS,
                  [cur[d] for d in sorted(cur)])
    return added, updated, skipped


# ------------------------------------------------------------------ 股價
def read_prices():
    rows = _read(PRICES_CSV, PRICE_COLS)
    for r in rows:
        for c in ("open", "high", "low", "close", "change", "volume"):
            r[c] = None if r[c] == "" else float(r[c])
    return sorted(rows, key=lambda r: (r["date"], r["code"]))


def upsert_prices(new_rows, force=False):
    cur = {(r["date"], r["code"]): r for r in read_prices()}
    added = updated = skipped = 0
    for r in new_rows:
        row = {c: r.get(c) for c in PRICE_COLS}
        k = (row["date"], row["code"])
        if k not in cur:
            cur[k] = row
            added += 1
        elif force and cur[k] != row:
            cur[k] = row
            updated += 1
        else:
            skipped += 1
    out = []
    for k in sorted(cur):
        r = dict(cur[k])
        for c in ("open", "high", "low", "close", "change", "volume"):
            r[c] = "" if r[c] is None else _num(r[c])
        out.append(r)
    _write_atomic(PRICES_CSV, PRICE_COLS, out)
    return added, updated, skipped


def _num(v):
    f = float(v)
    return str(int(f)) if f == int(f) else repr(f)


def latest_chip_date():
    rows = read_chips()
    return rows[-1]["date"] if rows else None


def latest_price_date():
    rows = read_prices()
    return rows[-1]["date"] if rows else None
