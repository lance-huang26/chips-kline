#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTPS 憑證設定的回歸測試。

重點只有兩件事：
  1. 不管走哪條路，驗證都不能被關掉（關掉 = 接受任何人冒充期交所回假資料）。
  2. 本機信任庫是空的時候（macOS python.org 版沒跑過 Install Certificates
     就是這個狀態），要自動改用 certifi，而不是直接死掉。

另外用 grep 釘住「每個 urlopen 都有帶 context=」——新增抓取來源時很容易忘，
忘了的話在 GitHub Actions 上照樣過，只有本機會炸，很難查。

跑法：python3 tests/test_netssl.py
"""
import os
import re
import ssl
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import netssl  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def reset():
    netssl._CTX = None
    netssl._SOURCE = None


def assert_verifying(ctx, where):
    check(ctx.verify_mode == ssl.CERT_REQUIRED, "%s：verify_mode 被關掉了" % where)
    check(ctx.check_hostname is True, "%s：check_hostname 被關掉了" % where)


# ---------------------------------------------------------------- 1. 正常機器
reset()
ctx = netssl.context()
assert_verifying(ctx, "系統信任庫")
check(netssl.context() is ctx, "context() 應該有快取，不要每次重建")
print("  這台機器的憑證來源：%s（%d 張根憑證）"
      % (netssl.source(), len(ctx.get_ca_certs())))

# ---------------------------------------------------------------- 2. 信任庫是空的
_real = ssl.create_default_context


def _empty(*a, **kw):
    c = _real(*a, **kw)
    if not kw.get("cafile"):
        c.get_ca_certs = lambda binary_form=False: []      # 假裝一張都沒載到
    return c


ssl.create_default_context = _empty
try:
    reset()
    ctx2 = netssl.context()
    assert_verifying(ctx2, "certifi 後備")
    try:
        import certifi  # noqa: F401
        check(netssl.source() == "certifi",
              "信任庫空的時候應該改用 certifi，實得 %r" % netssl.source())
        check(len(ctx2.get_ca_certs()) > 0, "certifi 那份應該真的載得到憑證")
        print("  信任庫空的時候：自動改用 certifi（%d 張），驗證仍然開著"
              % len(ctx2.get_ca_certs()))
    except ImportError:
        check(netssl.source() == "empty",
              "沒有 certifi 時應該回 empty 讓它撞牆，而不是關掉驗證")
        print("  信任庫空又沒有 certifi：維持驗證、讓它失敗（不會偷偷放行）")
finally:
    ssl.create_default_context = _real
    reset()

# ---------------------------------------------------------------- 3. 錯誤訊息
err = ("<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify "
       "failed: unable to get local issuer certificate (_ssl.c:1129)>")
hint = netssl.explain(err)
check("Install Certificates.command" in hint and "certifi" in hint,
      "憑證錯誤時應該附上修法，實得 %r" % hint)
check(netssl.explain("<urlopen error timed out>") == "",
      "一般連線錯誤不該亂加憑證說明")
print("  憑證錯誤會附上修法；一般連線錯誤不會")

# ---------------------------------------------------------------- 4. 每個 urlopen 都要帶 context
CALL = re.compile(r"urlopen\((.*?)\)\s*as\b", re.S)
for name in ("taifex.py", "taifut.py", "fetch_prices.py", "netssl.py"):
    src = open(os.path.join(ROOT, name), encoding="utf-8").read()
    calls = CALL.findall(src)
    check(calls, "%s 裡找不到 urlopen(...) as ...，這個測試的寫法要跟著改" % name)
    for c in calls:
        check("context=" in c,
              "%s 有一個 urlopen 沒帶 context=netssl.context()：%r"
              % (name, " ".join(c.split())))
check("verify_mode" not in open(os.path.join(ROOT, "netssl.py"),
                                encoding="utf-8").read(),
      "netssl.py 不應該去動 verify_mode")
print("  四個檔案裡的 urlopen 都有帶 context=")

print()
if fails:
    print("❌ 失敗 %d 項：" % len(fails))
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 憑證設定通過（驗證永遠開著、信任庫空時改用 certifi、urlopen 都有帶 context）")
