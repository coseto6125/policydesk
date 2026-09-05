#!/usr/bin/env bash
# 本機 → OCI 的 Claude 訂閱 token 同步（到期時間驅動）。
#
# 部署主機上沒有 Claude Code，沒辦法自己刷新 token；本機的 Claude Code 會在背景
# 刷新 ~/.claude/.credentials.json。這支腳本把本機那份推給 OCI，讓 policydesk 的
# AnthropicProvider 不會在 token 過期後開始 401——而 401 的樣子是櫃台說它接不到
# 模型，不是崩潰，所以沒人會注意到，直到有人抱怨。
#
# 設計沿用 digital-oasis 的 tasker-autobid/sync-token.sh，那支是踩過事故長出來的：
# 舊版在本機 token 也快過期時只印一句「請開一下 Claude Code」就放棄，服務停擺到
# 剛好有人開了 Claude Code 才活過來。復原條件押在「剛好有人在用電腦」上就等於沒有。
#
# 搭配本機 cron（每 30 分鐘檢查一次）：
#   */30 * * * * /home/enor/enor_agi/policydesk/scripts/sync-token.sh >> ~/.cache/policydesk-token-sync.log 2>&1
set -euo pipefail

# 路徑留成可覆寫，是為了能拿假憑證演練失敗分支而不動到真的那份。
LOCAL_CREDS="${SYNC_LOCAL_CREDS:-$HOME/.claude/.credentials.json}"
OCI_HOST="${SYNC_OCI_HOST:-enoract-168}"
# 憑證住在部署目錄「外面」。放在裡面的時候，一次 `rsync -az --delete` 把它刪了——它
# 不在 repo 裡，所以 --delete 認定它是多餘的。Docker 接著在同名位置建了一個空目錄
# （bind mount 找不到來源就會這樣），容器讀不到，provider 靜靜退回 codex-cli，而
# 唯一的線索是 `reason=no credentials`。
# shellcheck disable=SC2088  # `~` 是要留給遠端 shell 展開的，不是本機路徑
OCI_CREDS="~/policydesk-secrets/anthropic-token.json"
# 容器裡的 uid。Dockerfile 是 `useradd --uid 10001 desk`，程序不是 root，所以 600 的
# 檔案它讀不到——本機的 600 是對的，掛進容器就不是。
DESK_UID="${SYNC_DESK_UID:-10001}"
SSH_KEY="$HOME/.ssh/oci_enoract"
CLAUDE_BIN="${SYNC_CLAUDE_BIN:-$HOME/.local/bin/claude}"  # cron 的 PATH 沒有它
THRESHOLD=$((60 * 60))  # token 剩不到 1 小時就該動作（OCI 要推、本機要刷新）

ts() { date '+%Y-%m-%d %H:%M:%S'; }

local_expiry() {
  python3 -c "import json;print(json.load(open('$LOCAL_CREDS'))['claudeAiOauth']['expiresAt']//1000)" 2>/dev/null || echo 0
}

[ -f "$LOCAL_CREDS" ] || { echo "[$(ts)] ✘ 本機無 $LOCAL_CREDS"; exit 1; }

now=$(date +%s)
local_exp=$(local_expiry)

# 本機這份也快過期 → 先讓 Claude Code 自己刷新。`auth status` 不呼叫模型，是最便宜
# 的一條；它若沒觸發刷新，才用 `-p` 走一次真正的請求（那一定會刷）。兩條都試完仍沒
# 變新就明講失敗，不留「看起來有做事」的假象。
if [ $(( local_exp - now )) -lt "$THRESHOLD" ]; then
  for refresh in "auth status" "-p ok --max-turns 1"; do
    # shellcheck disable=SC2086  # 兩個 refresh 指令都要拆成多個參數
    timeout 90 "$CLAUDE_BIN" $refresh >/dev/null 2>&1 || true
    new_exp=$(local_expiry)
    [ "$new_exp" -gt "$local_exp" ] && { local_exp=$new_exp; break; }
  done

  if [ $(( local_exp - now )) -lt "$THRESHOLD" ]; then
    echo "[$(ts)] ✘ 本機 token 剩 $(( (local_exp - now) / 60 )) 分鐘且刷新失敗，需手動 claude auth login"
    exit 2
  fi
  echo "[$(ts)] ↻ 本機 token 已刷新至 $(date -d "@$local_exp" '+%H:%M')"
fi

# 讀 OCI 那份的到期時間（讀不到 = 沒推過 = 一定要推）。
oci_exp=$(ssh -i "$SSH_KEY" "$OCI_HOST" \
  'f=$HOME/policydesk-secrets/anthropic-token.json; python3 -c "import json,sys;print(json.load(open(sys.argv[1]))[\"claudeAiOauth\"][\"expiresAt\"]//1000)" "$f" 2>/dev/null' \
  2>/dev/null || echo 0)

remaining=$(( oci_exp - now ))
if [ "$oci_exp" -gt 0 ] && [ "$remaining" -gt "$THRESHOLD" ]; then
  echo "[$(ts)] OCI token 還有 $((remaining/60)) 分鐘，免同步"
  exit 0
fi

# 推上去的必須比 OCI 那份晚過期，否則只是把同一份即將死掉的憑證再複製一次，而 log
# 上會印著「✓ 已同步」。上面的主動刷新讓這一關幾乎踩不到；留著是因為它是唯一擋得住
# 「靜默推無效 token」的斷言：刷新那段哪天被改壞，這裡會出聲而不是繼續印勾勾。
if [ "$local_exp" -le "$oci_exp" ]; then
  echo "[$(ts)] ✘ 本機 token 不比 OCI 那份新（同為 $(date -d "@$local_exp" '+%H:%M') 到期），同步無意義"
  exit 2
fi

ssh -i "$SSH_KEY" "$OCI_HOST" "mkdir -p ~/policydesk-secrets && chmod 700 ~/policydesk-secrets"
rsync -az -e "ssh -i $SSH_KEY" "$LOCAL_CREDS" "$OCI_HOST:$OCI_CREDS"
# 640 加容器的群組，不是 600。600 屬於 ubuntu，容器以 uid 10001 跑，讀不到。
ssh -i "$SSH_KEY" "$OCI_HOST" "sudo chgrp $DESK_UID $OCI_CREDS && sudo chmod 640 $OCI_CREDS"

# 從容器裡讀一次。權限對不對，只有容器說了算——`provider_ready provider=anthropic`
# 只證明選型走到 Anthropic，不證明那個檔讀得到，而那正是上一次漏掉的一層：報了 ready，
# 客戶那邊每一題都收到「櫃台的語言服務目前無回應」。
if ! ssh -i "$SSH_KEY" "$OCI_HOST" \
  'docker exec policydesk-desk-1 head -c 1 /run/secrets/anthropic-token.json' >/dev/null 2>&1; then
  echo "[$(ts)] ✘ 推上去了，但容器讀不到 /run/secrets/anthropic-token.json"
  exit 2
fi
echo "[$(ts)] ✓ token 已同步並經容器讀取驗證（本機到期 $(date -d "@$local_exp" '+%H:%M')，原 OCI 剩 $((remaining/60)) 分鐘）"
