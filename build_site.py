#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 append-only 歷史檔 build 成前端吃的 data/site.json（與 file:// 用的 site.js）。

前端只讀這一支檔案。裡面放的是「全部歷史」而不是只有視窗內的資料，因為：
  * 區間切換（20 / 60 / 120 / 全部）要能立刻切，不該再回頭載檔案；
  * MA20 需要視窗開始日之前的收盤價，放全部歷史就自然有暖身資料，
    不必再額外維護一份 warmup。
一年約 240 個交易日、三檔股票，檔案量級是幾百 KB，載入成本可以接受。

結算日（台指期）預設取每月第三個星期三；那天不是交易日就順延到下一個交易日。
要手動指定某個月就寫進 config.json 的 settlementOverrides，例如 {"2026-08": "2026/08/19"}。
"""

import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import store  # noqa: E402

CONFIG = os.path.join(HERE, "config.json")
SITE_JSON = os.path.join(store.DATA, "site.json")
SITE_JS = os.path.join(store.DATA, "site.js")


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def third_wednesday(year, month):
    d = dt.date(year, month, 1)
    # weekday(): 週一=0 … 週三=2
    first_wed = d + dt.timedelta(days=(2 - d.weekday()) % 7)
    return first_wed + dt.timedelta(days=14)


def settlements(dates, overrides):
    """dates 是升序的交易日字串（YYYY/MM/DD）。"""
    if not dates:
        return []
    have = set(dates)
    months = sorted({(int(d[:4]), int(d[5:7])) for d in dates})
    out = []
    for y, m in months:
        key = "%04d-%02d" % (y, m)
        if key in overrides:
            out.append(overrides[key])
            continue
        target = third_wednesday(y, m)
        s = target.strftime("%Y/%m/%d")
        if s not in have:
            # 第三個星期三放假 → 順延到之後第一個交易日
            later = [d for d in dates if d > s]
            if not later:
                continue
            s = later[0]
        if s in have:
            out.append(s)
    return sorted(set(out))


def build(quiet=False):
    cfg = load_config()
    chips = store.read_chips()
    prices = store.read_prices()

    if not chips:
        raise SystemExit("data/history/chips.csv 是空的，沒有東西可以 build")
    if not prices:
        raise SystemExit("data/history/prices.csv 是空的，沒有東西可以 build")

    names = {s["code"]: s["name"] for s in cfg["stocks"]}
    codes = [s["code"] for s in cfg["stocks"]]

    by_code = {c: [] for c in codes}
    for r in prices:
        if r["code"] not in by_code:
            continue          # 歷史檔裡有但 config 已經拿掉的股票，略過
        valid = None not in (r["open"], r["high"], r["low"], r["close"])
        by_code[r["code"]].append({
            "date": r["date"],
            "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
            "change": r["change"], "volume": r["volume"],
            "valid": valid,
        })

    chip_dates = [c["date"] for c in chips]

    site = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "maPeriod": cfg["maPeriod"],
        "biasAlert": cfg["biasAlert"],
        "defaultThreshold": cfg["defaultThreshold"],
        "thresholdPresets": cfg["thresholdPresets"],
        "defaultWindow": cfg["defaultWindow"],
        "windowOptions": cfg["windowOptions"],
        "settlements": settlements(chip_dates, cfg.get("settlementOverrides", {})),
        "chips": [{k: c[k] for k in store.CHIP_COLS} for c in chips],
        "stocks": [{"code": c, "name": names[c], "rows": by_code[c]} for c in codes],
    }

    with open(SITE_JSON, "w", encoding="utf-8") as f:
        json.dump(site, f, ensure_ascii=False, separators=(",", ":"))
    # file:// 直開時 fetch 會被 CORS 擋掉，附一份 <script> 版備援
    with open(SITE_JS, "w", encoding="utf-8") as f:
        f.write("window.__SITE__ = ")
        json.dump(site, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    warn = []
    for s in site["stocks"]:
        have = {r["date"] for r in s["rows"]}
        missing = [d for d in chip_dates if d not in have]
        if missing:
            warn.append("%s 缺 %d 天的股價：%s%s" % (
                s["code"], len(missing), missing[:5],
                "…" if len(missing) > 5 else ""))

    if not quiet:
        span = "%s ~ %s" % (chip_dates[0], chip_dates[-1])
        print("  籌碼 %d 天（%s）" % (len(chips), span))
        for s in site["stocks"]:
            print("  %s %s：股價 %d 筆（%s ~ %s）" % (
                s["code"], s["name"], len(s["rows"]),
                s["rows"][0]["date"], s["rows"][-1]["date"]))
        print("  結算日 %d 個：%s" % (len(site["settlements"]),
                                    ", ".join(site["settlements"])))
        print("  已寫出 data/site.json（%.0f KB）與 data/site.js"
              % (os.path.getsize(SITE_JSON) / 1024))
    for w in warn:
        print("  [注意] " + w, file=sys.stderr)
    return site


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    build(quiet=ap.parse_args().quiet)
