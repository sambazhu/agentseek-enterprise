#!/bin/bash
# cube-poc-portguard —— PoC 管理端口源收敛（专用链，v4+v6 双栈，R4 版）。
#
# 安全合同（R4，Codex 裁定）：
#   1) 失败后及再次执行期间，至少一个**有效**拒绝路径（跳转在 INPUT 且链内
#      规则与期望逐行全等——"有跳转"不等于"有效保护"）始终在位；除非进入时
#      本就无任何有效保护；
#   2) NEW 接管（跳转入 INPUT）前失败：仅清理**未挂跳转**的新资源，OLD 原样保留；
#   3) NEW 接管后失败（重命名等晚期失败）：保留完整 NEW 与其跳转，非零退出，
#      不清理——残留以临时链名提供等效保护，preflight 按规范形态失败关闭并暴露；
#   4) 再次 apply 先识别残留：NEW 有效 → 先收敛为规范名再继续；NEW 无效且未挂
#      跳转 → 清除后重建；NEW 挂了跳转但无效（外部污染态）→ -N 失败退出，
#      绝不自动拆除可能仍在提供部分拒绝的规则；
#   5) apply/remove 以 flock 互斥（默认等 30s，超时 rc=75）；
#   6) 显式 remove（有意拆除，不经 trap）与失败清理严格分离。
#
# 规则形态（与 preflight 的 iptables -S token 全等校验一致）：
#   v4 INPUT 第 1 条：-A INPUT -j CUBE_POC_GUARD（无条件跳转）
#   v4 链（9999/8082 各三条，有序）：
#     -A CUBE_POC_GUARD -s 127.0.0.1/32     -p tcp -m tcp --dport P -j ACCEPT
#     -A CUBE_POC_GUARD -s 192.10.50.172/32 -p tcp -m tcp --dport P -j ACCEPT
#     -A CUBE_POC_GUARD                          -p tcp -m tcp --dport P -j REJECT --reject-with tcp-reset
#   v6 链（cubelet 于 [::]:9999 双栈监听，实测同链路客户端可经 link-local 绕过
#   v4 守卫 → 必须等效 v6 保护。节点无全局 v6，平台自连走 v4/v4-mapped，
#   故 v6 无节点自源 ACCEPT，仅放行 ::1）：
#     -A CUBE_POC_GUARD -s ::1/128 -p tcp -m tcp --dport P -j ACCEPT
#     -A CUBE_POC_GUARD              -p tcp -m tcp --dport P -j REJECT --reject-with tcp-reset
#
# 用法：cube-poc-portguard.sh apply|remove
set -euo pipefail

CHAIN=CUBE_POC_GUARD
NEW_CHAIN=CUBE_POC_GUARD.new
NODE_IP=192.10.50.172
FAMILIES="iptables ip6tables"
LOCK_FILE="${CUBE_POC_GUARD_LOCK:-/run/cube-poc-portguard.lock}"
LOCK_WAIT="${CUBE_POC_GUARD_LOCK_WAIT:-30}"

log() { echo "portguard: $*" >&2; }

expected_rules() {  # $1=家族 $2=链名 → 期望规则（与 iptables -S 输出逐行一致）
  local fam="$1" ch="$2" p
  for p in 9999 8082; do
    if [ "$fam" = ip6tables ]; then
      printf '%s\n' \
        "-A $ch -s ::1/128 -p tcp -m tcp --dport $p -j ACCEPT" \
        "-A $ch -p tcp -m tcp --dport $p -j REJECT --reject-with tcp-reset"
    else
      printf '%s\n' \
        "-A $ch -s 127.0.0.1/32 -p tcp -m tcp --dport $p -j ACCEPT" \
        "-A $ch -s ${NODE_IP}/32 -p tcp -m tcp --dport $p -j ACCEPT" \
        "-A $ch -p tcp -m tcp --dport $p -j REJECT --reject-with tcp-reset"
    fi
  done
}

# 有效保护 = 跳转在 INPUT 且链内规则与期望逐行全等（"有跳转"≠"有效保护"）
effective() {  # $1=家族 $2=链名
  local fam="$1" ch="$2"
  "$fam" -C INPUT -j "$ch" 2>/dev/null || return 1
  [ "$("$fam" -S "$ch" 2>/dev/null | grep '^-A ')" = "$(expected_rules "$fam" "$ch")" ]
}

# 失败清理：只清**未挂跳转**的 NEW 残留；挂了跳转的 NEW 一律不动
# （可能仍在提供保护——交给下次 apply 收敛或人工处置，preflight 会暴露）
cleanup_new() {  # $1=家族
  local fam="$1"
  if "$fam" -C INPUT -j "$NEW_CHAIN" 2>/dev/null; then return 0; fi
  "$fam" -F "$NEW_CHAIN" 2>/dev/null || true
  "$fam" -X "$NEW_CHAIN" 2>/dev/null || true
}

apply_family() {  # $1=家族
  local fam="$1" p
  if effective "$fam" "$NEW_CHAIN"; then
    # 上轮 NEW 接管后失败的残留：NEW 是当前有效保护 → 收敛为规范名
    while "$fam" -D INPUT -j "$CHAIN" 2>/dev/null; do :; done
    "$fam" -F "$CHAIN" 2>/dev/null || true
    "$fam" -X "$CHAIN" 2>/dev/null || true
    "$fam" -E "$NEW_CHAIN" "$CHAIN"
    if effective "$fam" "$CHAIN"; then return 0; fi
    log "$fam 收敛核验失败（NEW 原样在位，保持保护）"
    return 1
  fi
  cleanup_new "$fam"                       # 未挂跳转的半成品残留
  "$fam" -N "$NEW_CHAIN"                   # 失败 → 旧保护在位，退出
  for p in 9999 8082; do
    if [ "$fam" = ip6tables ]; then
      "$fam" -A "$NEW_CHAIN" -s ::1 -p tcp --dport "$p" -j ACCEPT
    else
      "$fam" -A "$NEW_CHAIN" -s 127.0.0.1 -p tcp --dport "$p" -j ACCEPT
      "$fam" -A "$NEW_CHAIN" -s "$NODE_IP" -p tcp --dport "$p" -j ACCEPT
    fi
    "$fam" -A "$NEW_CHAIN" -p tcp --dport "$p" -j REJECT --reject-with tcp-reset
  done                                     # 任一失败 → NEW 未挂跳转，trap 清理，OLD 在位
  "$fam" -I INPUT 1 -j "$NEW_CHAIN"        # 接管点：此后任何失败一律保留 NEW
  while "$fam" -D INPUT -j "$CHAIN" 2>/dev/null; do :; done   # 清尽旧跳转（含异常重复）
  "$fam" -F "$CHAIN" 2>/dev/null || true
  "$fam" -X "$CHAIN" 2>/dev/null || true   # 被外部链引用时失败 → 下行 -E 必失败 → 保留 NEW
  "$fam" -E "$NEW_CHAIN" "$CHAIN"          # 原子重命名（失败则临时名链继续保护）
  effective "$fam" "$CHAIN"                # 终态核验（失败=set -e 退出，NEW 在位）
}

apply() {
  local fam
  for fam in $FAMILIES; do
    apply_family "$fam"
  done
}

remove() {  # 有意拆除（仅回滚场景；不经 trap）
  local fam
  for fam in $FAMILIES; do
    while "$fam" -D INPUT -j "$CHAIN" 2>/dev/null; do :; done
    "$fam" -F "$CHAIN" 2>/dev/null || true
    "$fam" -X "$CHAIN" 2>/dev/null || true
    "$fam" -D INPUT -j "$NEW_CHAIN" 2>/dev/null || true
    "$fam" -F "$NEW_CHAIN" 2>/dev/null || true
    "$fam" -X "$NEW_CHAIN" 2>/dev/null || true
  done
}

cmd="${1:-apply}"
case "$cmd" in
  apply|remove) ;;
  *)
    echo "usage: $0 [apply|remove]" >&2; exit 64 ;;
esac

# 锁先行、trap 后设：锁超时退出（75）绝不能触发失败清理——否则会对
# 正在持锁运行的另一实例执行 cleanup_new（并发互扰，R4 状态化测试抓获）。
exec 9>"$LOCK_FILE"
flock -w "$LOCK_WAIT" 9 || { log "锁等待超时（另一实例在执行）"; exit 75; }
trap 'rc=$?; if [ "$rc" -ne 0 ]; then for f in $FAMILIES; do cleanup_new "$f"; done; fi; exit $rc' EXIT

if [ "$cmd" = remove ]; then trap - EXIT; remove; else apply; fi
