#!/usr/bin/env bash
# idle_in_transaction_session_timeout が zombie セッションを自動破棄することを
# 検証する。セッションレベルで timeout を 10 秒に設定し、advisory lock を握った
# まま idle になったトランザクションが強制終了されることを確認する。
# 環境変数 DATABASE_URL が必要（例: postgresql://shiori:shiori@localhost:5432/shiori）。
set -euo pipefail

DB="${DATABASE_URL:?DATABASE_URL が未設定}"
LOCK_ID=12345
TIMEOUT_SECONDS=10
WAIT=$((TIMEOUT_SECONDS + 5))

# cleanup: ゾンビセッションを確実に片付ける
cleanup() {
  psql "$DB" -c "SELECT pg_terminate_backend(pid) FROM pg_locks
    WHERE locktype='advisory' AND objid=$LOCK_ID AND pid != pg_backend_pid();" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "=== 1. zombie セッション生成（advisory lock $LOCK_ID 獲得、timeout=${TIMEOUT_SECONDS}s）==="

# pg_sleep を使うと idle in transaction にならないので、FIFO で psql を
# 開きっぱなしにして block させる。
FIFO=$(mktemp -u)
mkfifo "$FIFO"
psql "$DB" --no-psqlrc -f "$FIFO" &
ZOMBIE_PID=$!
exec 3>"$FIFO"

echo "SET idle_in_transaction_session_timeout = $((TIMEOUT_SECONDS * 1000));" >&3
echo "BEGIN;" >&3
echo "SELECT pg_advisory_xact_lock($LOCK_ID);" >&3

# psql が lock 獲得するのを待つ
sleep 1

echo ""
echo "=== 2. lock 保持確認 ==="
psql "$DB" --no-psqlrc -c "
  SELECT locktype, objid, pid, granted
  FROM pg_locks
  WHERE locktype = 'advisory' AND objid = $LOCK_ID;
"

echo ""
echo "=== 3. 別セッションからの lock 取得試行（blocking_pids 確認）==="
psql "$DB" --no-psqlrc -c "
  SELECT pid, pg_blocking_pids(pid) AS blocked_by
  FROM pg_locks
  WHERE locktype = 'advisory' AND objid = $LOCK_ID AND NOT granted;
" || echo "（block されず=lock 未獲得。再試行）"

echo ""
echo "=== 4. ${TIMEOUT_SECONDS}秒待機（timeout で zombie が切断されるのを待つ）==="
for i in $(seq 1 "$WAIT"); do
  sleep 1
  HELD=$(psql "$DB" --no-psqlrc -t -A \
    -c "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=$LOCK_ID AND granted;" 2>/dev/null || echo "0")
  if [ "$HELD" = "0" ]; then
    echo "  ${i}秒経過: lock 解放を検知"
    break
  fi
  if [ "$i" -lt "$WAIT" ]; then
    echo -n "."
  fi
done
echo ""

echo ""
echo "=== 5. 最終状態確認 ==="
psql "$DB" --no-psqlrc -c "
  SELECT locktype, objid, pid, granted
  FROM pg_locks
  WHERE locktype = 'advisory' AND objid = $LOCK_ID;
"

HELD=$(psql "$DB" --no-psqlrc -t -A \
  -c "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=$LOCK_ID AND granted;" 2>/dev/null || echo "1")

echo ""
if [ "$HELD" = "0" ]; then
  echo "PASS: zombie セッションが自動破棄され、advisory lock が解放されました"
else
  echo "FAIL: lock が解放されていません（timeout=${TIMEOUT_SECONDS}s では不十分か？）"
  exit 1
fi
