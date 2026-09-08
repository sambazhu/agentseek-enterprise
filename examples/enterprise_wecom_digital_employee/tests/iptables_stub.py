#!/usr/bin/env python3
"""状态化 iptables/ip6tables 模拟器（测试专用，失败语义忠实子集）。

- -X 拒绝删除被任何链中规则引用的链；-E 目标名已存在则失败；
  -D/-C 按完整 token 相等匹配；-D 无匹配规则失败（while 循环终止信号）。
- -s 裸地址规范化为 /32|/128；-p tcp 与 --dport 并存时自动补 -m tcp
  （与真实 iptables -S 输出一致，portguard 的期望规则即该形态）。
- 每次操作（含失败与查询）后把完整状态追加到日志（带家族与运行标签），
  供测试逐步断言"有效保护始终在位"。
- 注入：CUBE_STUB_FAIL="家族:操作"（操作∈N,A,I,D,F,X,E）→ 该操作恒失败。
- 状态按家族独立持久化：$CUBE_STUB_STATE.<家族>.json。
"""
import json
import os
import sys
from pathlib import Path

ARGS = sys.argv[1:]
FAM = Path(sys.argv[0]).name
_BASE = Path(os.environ["CUBE_STUB_STATE"])
STATE_PATH = _BASE.parent / (_BASE.name + "." + FAM + ".json")
LOG_PATH = Path(os.environ["CUBE_STUB_LOG"])
TAG = os.environ.get("CUBE_STUB_TAG", "-")
FAIL = os.environ.get("CUBE_STUB_FAIL", "")
BASE_CHAINS = ("INPUT", "FORWARD", "OUTPUT")


def _load():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"chains": {c: [] for c in BASE_CHAINS}}


def _snap(state):
    with LOG_PATH.open("a") as fh:
        fh.write(f"SNAP {FAM} {TAG} {json.dumps(state, sort_keys=True)}\n")


def _save(state):
    STATE_PATH.write_text(json.dumps(state))
    _snap(state)


def _die(state, msg):
    _snap(state)  # 记录失败时刻状态（未变更）
    sys.stderr.write(f"stub-{FAM}: {msg}\n")
    sys.exit(1)


def _fail_if(state, op):
    if f"{FAM}:{op}" == FAIL:
        _die(state, f"injected failure {op}")


def _canon(tokens):
    out, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-s" and i + 1 < len(tokens) and "/" not in tokens[i + 1]:
            out += ["-s", tokens[i + 1] + ("/128" if ":" in tokens[i + 1] else "/32")]
            i += 2
            continue
        out.append(tok)
        i += 1
    if ("-p" in out and out[out.index("-p") + 1:out.index("-p") + 2] == ["tcp"]
            and "--dport" in out and "-m" not in out):
        out[out.index("-p") + 2:out.index("-p") + 2] = ["-m", "tcp"]
    return out


def _referenced(state, chain):
    for rules in state["chains"].values():
        for rule in rules:
            if "-j" in rule and rule[rule.index("-j") + 1:rule.index("-j") + 2] == [chain]:
                return True
    return False


def op_new(state, args):
    _fail_if(state, "N")
    if args[1] in state["chains"]:
        _die(state, f"chain exists: {args[1]}")
    state["chains"][args[1]] = []
    _save(state)


def op_append(state, args):
    _fail_if(state, "A")
    if args[1] not in state["chains"]:
        _die(state, f"no chain: {args[1]}")
    state["chains"][args[1]].append(_canon(args[2:]))
    _save(state)


def op_insert(state, args):
    _fail_if(state, "I")
    chain = args[1]
    if chain not in state["chains"]:
        _die(state, f"no chain: {chain}")
    state["chains"][chain].insert(int(args[2]) - 1, _canon(args[3:]))
    _save(state)


def op_delete_rule(state, args):
    _fail_if(state, "D")
    rule = _canon(args[2:])
    if args[1] not in state["chains"] or rule not in state["chains"][args[1]]:
        _die(state, f"no such rule: {args[1]} {' '.join(rule)}")
    state["chains"][args[1]].remove(rule)
    _save(state)


def op_check(state, args):
    rule = _canon(args[2:])
    ok = args[1] in state["chains"] and rule in state["chains"][args[1]]
    _snap(state)
    sys.exit(0 if ok else 1)


def op_flush(state, args):
    _fail_if(state, "F")
    if args[1] not in state["chains"]:
        _die(state, f"no chain: {args[1]}")
    state["chains"][args[1]] = []
    _save(state)


def op_delete_chain(state, args):
    _fail_if(state, "X")
    if args[1] not in state["chains"]:
        _die(state, f"no chain: {args[1]}")
    if _referenced(state, args[1]):
        _die(state, f"chain referenced: {args[1]}")
    del state["chains"][args[1]]
    _save(state)


def op_rename(state, args):
    _fail_if(state, "E")
    if args[1] not in state["chains"]:
        _die(state, f"no chain: {args[1]}")
    if args[2] in state["chains"]:
        _die(state, f"chain exists: {args[2]}")
    state["chains"][args[2]] = state["chains"].pop(args[1])
    # 真实 iptables -E 会同步更新所有跳转到旧链名的规则引用
    for rules in state["chains"].values():
        for rule in rules:
            if "-j" in rule and rule[rule.index("-j") + 1:rule.index("-j") + 2] == [args[1]]:
                rule[rule.index("-j") + 1] = args[2]
    _save(state)


def op_dump(state, args):
    chains = [args[1]] if len(args) > 1 else list(state["chains"])
    for chain in chains:
        if chain not in state["chains"]:
            _die(state, f"no chain: {chain}")
        print(f"-P {chain} ACCEPT" if chain in BASE_CHAINS else f"-N {chain}")
        for rule in state["chains"][chain]:
            print(f"-A {chain} " + " ".join(rule))
    _snap(state)


OPS = {
    "-N": op_new,
    "-A": op_append,
    "-I": op_insert,
    "-D": op_delete_rule,
    "-C": op_check,
    "-F": op_flush,
    "-X": op_delete_chain,
    "-E": op_rename,
    "-S": op_dump,
}


def main():
    if not ARGS or ARGS[0] not in OPS:
        sys.stderr.write(f"stub-{FAM}: unsupported: {ARGS}\n")
        sys.exit(64)
    OPS[ARGS[0]](_load(), ARGS)


if __name__ == "__main__":
    main()
