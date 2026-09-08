#!/bin/bash
# cube-poc-portguard —— PoC 管理端口源收敛（专用链），先于监听服务执行。
#
# 结构（preflight.check_firewall_structure 与此严格对应）：
#   INPUT 第 1 条：-j CUBE_POC_GUARD
#   CUBE_POC_GUARD（有序）：
#     ACCEPT tcp 127.0.0.1      dpt 9999
#     ACCEPT tcp 192.10.50.172  dpt 9999   （节点自源：master→cubelet grpc）
#     REJECT tcp 0.0.0.0/0      dpt 9999   tcp-reset
#     ACCEPT tcp 127.0.0.1      dpt 8082
#     ACCEPT tcp 192.10.50.172  dpt 8082   （节点自源：LCM↔proxy admin advertise）
#     REJECT tcp 0.0.0.0/0      dpt 8082   tcp-reset
#
# 幂等：每次运行先拆旧跳转/旧链再重建（等价于声明式收敛）。
# 移除：bash portguard.sh remove
set -euo pipefail

CHAIN=CUBE_POC_GUARD
NODE_IP=192.10.50.172

remove() {
  iptables -D INPUT -j "$CHAIN" 2>/dev/null || true
  iptables -F "$CHAIN" 2>/dev/null || true
  iptables -X "$CHAIN" 2>/dev/null || true
}

apply() {
  remove
  iptables -N "$CHAIN"
  for P in 9999 8082; do
    iptables -A "$CHAIN" -p tcp --dport "$P" -s 127.0.0.1 -j ACCEPT
    iptables -A "$CHAIN" -p tcp --dport "$P" -s "$NODE_IP" -j ACCEPT
    iptables -A "$CHAIN" -p tcp --dport "$P" -j REJECT --reject-with tcp-reset
  done
  # 跳转置于 INPUT 首条：任何前置规则都不可能在守卫之前放行目标端口
  iptables -I INPUT 1 -j "$CHAIN"
}

case "${1:-apply}" in
  apply) apply ;;
  remove) remove ;;
  *) echo "usage: $0 [apply|remove]" >&2; exit 64 ;;
esac
