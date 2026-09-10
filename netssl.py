#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共用的 HTTPS 憑證設定。

為什麼需要這個檔案：macOS 上用 python.org 安裝的 Python **不吃系統鑰匙圈**，
它要讀自己那份 CA 檔，而那份檔案只有在安裝後手動跑過
`Install Certificates.command` 才會建立。沒跑過的話，任何 https 請求都會是

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

——證交所和期交所的憑證本身沒問題，是本機沒有任何根憑證可以拿來驗。
GitHub Actions（Ubuntu）不會遇到，所以排程一直是好的，只有本機會炸。

處理方式：預設信任庫是空的時候，改用 certifi 那份（pip 自己也是用這份）。
**任何情況下都不會關掉驗證**——關掉就等於接受任何人冒充期交所回假資料，
那比抓不到資料糟得多。兩者都沒有時直接讓它失敗，並在訊息裡寫清楚怎麼修。
"""

import ssl

_CTX = None
_SOURCE = None

HINT = (
    "本機沒有可用的 CA 憑證，所以無法驗證 https 連線（不是來源網站的問題）。\n"
    "       macOS 用 python.org 版 Python 的話，兩種修法擇一：\n"
    '         1) 跑一次 "/Applications/Python 3.x/Install Certificates.command"'
    "（把 3.x 換成你的版本）\n"
    "         2) pip3 install certifi        ← 本專案會自動改用它\n"
    "       （Homebrew 版 Python 與 GitHub Actions 不會有這個問題。）"
)


def context():
    """回傳要給 urlopen 用的 SSLContext。快取，整個行程只算一次。"""
    global _CTX, _SOURCE
    if _CTX is not None:
        return _CTX

    ctx = ssl.create_default_context()
    if ctx.get_ca_certs():                 # 系統信任庫有東西，就用它
        _CTX, _SOURCE = ctx, "system"
        return _CTX

    try:
        import certifi
    except ImportError:
        _CTX, _SOURCE = ctx, "empty"       # 讓它去撞牆，但錯誤訊息會補上 HINT
        return _CTX

    _CTX, _SOURCE = ssl.create_default_context(cafile=certifi.where()), "certifi"
    return _CTX


def source():
    """"system" / "certifi" / "empty"，給診斷訊息用。"""
    if _CTX is None:
        context()
    return _SOURCE


def explain(err):
    """連線錯誤 → 要附加在錯誤訊息後面的說明（沒話說就回空字串）。"""
    if "CERTIFICATE_VERIFY_FAILED" in str(err):
        return "\n       " + HINT
    return ""


if __name__ == "__main__":
    import urllib.request
    print("憑證來源：%s" % source())
    for url in ("https://www.twse.com.tw/", "https://www.taifex.com.tw/"):
        try:
            with urllib.request.urlopen(url, timeout=20, context=context()) as r:
                print("  OK  %-32s HTTP %s" % (url, r.status))
        except Exception as e:                                   # noqa: BLE001
            print("  失敗 %-32s %s%s" % (url, e, explain(e)))
