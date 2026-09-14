#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 chips.csv 裡缺的近月十大欄位補回來。

2026/09 才發現十大交易人／十大特定人有兩種口徑：

    所有契約（999999）  含遠月，本專案原本用的
    近月（最小的真實到期月份）  市面上看盤 App 顯示的

兩者差很多，2026/09/14 甚至方向相反（所有契約 +4,557、近月 +549；
特定人 +1,705 對 -1,790）。現在兩個都存，這支腳本負責把加欄位之前的
歷史補上 `top10_trader_front` / `top10_specific_front`。

只補這兩欄。當初抓到的其他欄位一個位元組都不動——歷史檔的意義就是
「那天實際抓到什麼」，回頭重寫會讓它失去對帳的價值。

用法：
    python3 backfill_large.py            # 補所有缺的日期
    python3 backfill_large.py --dry-run  # 只看會補什麼
    python3 backfill_large.py --force    # 連已經有值的日期也重抓覆寫
"""

import argparse
import calendar
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import store    # noqa: E402
import taifex   # noqa: E402

FIELDS = ("top10_trader_front", "top10_specific_front")


def months_of(dates):
    """要抓哪幾個月。端點可以跨日查，所以按月抓，一個月一次請求。"""
    out = []
    for d in dates:
        m = d[:4] + d[5:7]
        if m not in out:
            out.append(m)
    return sorted(out)


def month_bounds(month):
    y, m = int(month[:4]), int(month[4:])
    return ("%04d/%02d/01" % (y, m),
            "%04d/%02d/%02d" % (y, m, calendar.monthrange(y, m)[1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="已經有值的日期也重抓（正常情況不需要）")
    ap.add_argument("--sleep", type=float, default=3.0)
    args = ap.parse_args()

    rows = store.read_chips()
    if not rows:
        print("chips.csv 是空的，沒事可做。")
        return

    todo = [r["date"] for r in rows
            if args.force or any(r.get(f) is None for f in FIELDS)]
    if not todo:
        print("每一天都已經有近月欄位了，沒事可做。")
        return

    months = months_of(todo)
    print("要補 %d 天（%s ~ %s），分 %d 個月抓：%s\n"
          % (len(todo), todo[0], todo[-1], len(months), "、".join(months)))

    want = set(todo)
    patches, seen = [], set()
    for i, m in enumerate(months):
        a, b = month_bounds(m)
        csv_text = taifex.fetch_large_range(a, b)
        got = taifex.parse_large_range(csv_text)
        hit = sorted(set(got) & want)
        for d in hit:
            patches.append({"date": d,
                            "top10_trader_front": got[d]["top10_trader_front"],
                            "top10_specific_front": got[d]["top10_specific_front"]})
            seen.add(d)
        print("  %s：回傳 %d 天，其中 %d 天是我們要的" % (m, len(got), len(hit)))
        if i < len(months) - 1:
            time.sleep(args.sleep)

    lost = sorted(want - seen)
    if lost:
        # 歷史檔有這天、期交所卻沒有這天的大額交易人資料，代表哪裡對不上，
        # 寧可整批不寫也不要補一半。
        print("\n[失敗] 這些日期在期交所的大額交易人資料裡找不到：%s"
              % "、".join(lost), file=sys.stderr)
        print("       沒有寫入任何資料。", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("\n（dry-run，沒有寫檔）前 5 筆：")
        for p in patches[:5]:
            print("   ", p)
        return

    changed, missing = store.patch_chips(patches)
    print("\n補上 %d 天（找不到 %d 天）。" % (changed, missing))
    print("記得跑 python3 build_site.py 重建畫面資料。")


if __name__ == "__main__":
    main()
