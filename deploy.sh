#!/usr/bin/env bash
# 一鍵推上 GitHub 並開啟 Pages。
# 用法：./deploy.sh <你的GitHub帳號> [repo名稱]
set -euo pipefail

USER_NAME="${1:-}"
REPO="${2:-chips-kline}"
if [ -z "$USER_NAME" ]; then
  echo "用法：./deploy.sh <你的GitHub帳號> [repo名稱]"; exit 1
fi

cd "$(dirname "$0")"

if command -v gh >/dev/null 2>&1; then
  # 有 gh CLI：連 repo 建立與 Pages 設定都一起做掉
  gh repo view "$USER_NAME/$REPO" >/dev/null 2>&1 \
    || gh repo create "$USER_NAME/$REPO" --public --source=. --remote=origin --push
  git remote get-url origin >/dev/null 2>&1 \
    || git remote add origin "https://github.com/$USER_NAME/$REPO.git"
  git push -u origin main
  gh api -X POST "repos/$USER_NAME/$REPO/pages" \
    -f 'source[branch]=main' -f 'source[path]=/' >/dev/null 2>&1 \
    || gh api -X PUT "repos/$USER_NAME/$REPO/pages" \
         -f 'source[branch]=main' -f 'source[path]=/' >/dev/null 2>&1 || true
  echo "完成 → https://$USER_NAME.github.io/$REPO/"
else
  # 沒有 gh：repo 要先在 github.com 上手動開好（public、不要勾 README）
  git remote get-url origin >/dev/null 2>&1 \
    || git remote add origin "https://github.com/$USER_NAME/$REPO.git"
  git push -u origin main
  echo
  echo "已推上去了。最後一步請到網頁上開 Pages："
  echo "  https://github.com/$USER_NAME/$REPO/settings/pages"
  echo "  Source 選 Deploy from a branch → Branch: main → 資料夾: / (root) → Save"
  echo
  echo "約 1 分鐘後網址會是：https://$USER_NAME.github.io/$REPO/"
fi
