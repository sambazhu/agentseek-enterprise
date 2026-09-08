#!/bin/bash
# cube-poc-portguard —— PoC 管理端口源收敛（专用链），先于监听服务执行。
#
# 结构（preflight.check_firewall_structure 按 iptables -S 完整形态校验）：
#   INPUT 第 1 条：-A INPUT -j CUBE_POC_GUARD   （无条件跳转，无任何匹配项）
#   CUBE_POC_GUARD（有序，完整形态）：
#     -A CUBE_POC_GUARD -s 127.0.0.1/32    -p tcp -m tcp --dport 9999 -j ACCEPT
#     -A CUBE_POC_GUARD -s 192.10.50.172/32 -p tcp -m tcp --dport 9999 -j ACCEPT
#     -A CUBE_POC_GUARD                        -p tcp -m tcp --dport 9999 -j REJECT --reject-with tcp-reset
#     （8082 同构三条）
#
# 无暴露窗口换链（R3）：新链先建满规则并插入 INPUT 首位，确认新跳转在位
# 后才移除旧跳转/旧链，最后重命名为规范名。任一步失败：旧保护或新保护
# 至少一个仍在位（set -e 退出非零），preflight 会按规范形态失败关闭。
#
# 移除：bash portguard.sh remove
set -euo pipefail

CHAIN=CUBE_POC_GUARD
NEW_CHAIN=CUBE_POC_GUARD.new
NODE_IP=192.10.50.172

cleanup_new() {
  iptables -D INPUT -j "$NEW_CHAIN" 2>/dev/null || true
  iptables -F "$NEW_CHAIN" 2>/dev/null || true
  iptables -X "$NEW_CHAIN" 2>/dev/null || true
}

remove() {
  iptables -D INPUT -j "$CHAIN" 2>/dev/null || true
  iptables -F "$CHAIN" 2>/dev/null || true
  iptables -X "$CHAIN" 2>/dev/null || true
  cleanup_new
}

apply() {
  cleanup_new                      # 清理上次失败残留的临时链（不动旧保护）
  iptables -N "$NEW_CHAIN"         # 失败 → 旧保护在位，退出
  for P in 9999 8082; do
    iptables -A "$NEW_CHAIN" -p tcp --dport "$P" -s 127.0.0.1 -j ACCEPT
    iptables -A "$NEW_CHAIN" -p tcp --dport "$P" -s "$NODE_IP" -j ACCEPT
    iptables -A "$NEW_CHAIN" -p tcp --dport "$P" -j REJECT --reject-with tcp-reset
  done                             # 任一失败 → cleanup（trap）→ 旧保护在位
  iptables -I INPUT 1 -j "$NEW_CHAIN"   # 新跳转入首位（此刻新旧双保护）
  iptables -D INPUT -j "$CHAIN" 2>/dev/null || true   # 移除旧跳转（新保护已接管）
  iptables -F "$CHAIN" 2>/dev/null || true
  iptables -X "$CHAIN" 2>/dev/null || true
  iptables -E "$NEW_CHAIN" "$CHAIN"     # 规范名（失败则临时名仍在位保护）
}

trap 'rc=$?; [ "$rc" -ne 0 ] && cleanup_new; exit $rc' EXIT

case "${1:-apply}" in
  apply) apply ;;
  remove) trap - EXIT; remove ;;
  *) echo "usage: $0 [apply|remove]" >&2; exit 64 ;;
esac
