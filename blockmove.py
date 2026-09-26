#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
blockmove.py — 文档块移动工具（纯 Python 标准库，单文件）

用法:
    python3 blockmove.py DOC OPS [--report REPORT_FILE]

    DOC   文档行流文件（"-" 表示 stdin）
    OPS   块移动操作序列文件
    输出  移动后的文档写到 stdout；错误清单与移动历史写到 stderr
          （或用 --report 指定文件）。有错误时退出码为 1，否则为 0。

────────────────────────────────────────────────────────────────
语法设计（自定）及理由
────────────────────────────────────────────────────────────────
文档语法（逐行识别，标记独占一行）：
    #block <id>      块开始；id 为 [A-Za-z0-9_-]+，全文档应唯一
    #endblock [id]   块结束；可选带 id，带上时会校验是否匹配
    #ref <id>        引用某个块；引用是文档中的一行，随所在块一起移动
    其余行           普通文本行

操作语法（OPS 文件，每行一条，空行与 # 开头的注释行忽略）：
    move <id> before <target-id>   把块移到目标块之前（同级）
    move <id> after  <target-id>   把块移到目标块之后（同级）
    move <id> into   <target-id>   把块移入目标块内部（成为其最后一个子块）

理由：
  * 行级标记而非缩进/配对符号：行流可逐行识别，无需回溯，实现简单且
    对人工编写友好；标记独占一行避免与正文内容歧义。
  * 操作用"关系 + 目标块"（before/after/into）而非绝对行号：行号在
    多次移动后会失效，块 id 是稳定锚点；三种关系足以表达同级排序与
    跨层级移动（移出到顶层可对顶层块用 before/after）。
  * 操作逐条顺序应用，后一条看到的是前一条的结果，符合直觉且历史可追溯。

────────────────────────────────────────────────────────────────
语义与设计取舍
────────────────────────────────────────────────────────────────
* 块与引用构成一棵树：移动一个块时其整个子树（含嵌套子块、块内引用、
  普通行）整体移动，块内引用与目标的绑定关系天然保持。
* 引用作用域采用词法作用域：#ref X 合法当且仅当引用位于块 X 内部
  （X 是引用的祖先块）。因此把块 A 移出块 B 后，A 内对 B 的引用即悬空，
  工具会报告悬空引用的（移动后）行号与其原始行号。
* 非法目标：目标块是被移动块自身或其后代（目标在块内部）时拒绝移动
  并报告，该条操作跳过，后续操作继续。
* 未闭合块：解析到 EOF 仍有未闭合块时报告其起始行；该块按"在 EOF 处
  闭合"保留在树中，使后续操作仍可作用于它（容错优先于拒绝）。
* 悬空 #endblock、#endblock id 不匹配、块 id 重复：均报告；id 重复时
  move/ref 解析到先出现者（文档中说明，避免静默歧义）。
* 移动历史：每条操作记录操作前后块的起始行号与嵌套路径（如 /a/b），
  行号是该操作时刻文档状态下的 1 基行号。
* 引用绑定检查在全部移动完成后对最终文档做一次（报告最终位置）；
  逐条移动的历史中也能定位中间状态。
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field

ID = r"[A-Za-z0-9_-]+"
BLOCK_START_RE = re.compile(rf"^\s*#block\s+({ID})\s*$")
BLOCK_END_RE = re.compile(rf"^\s*#endblock(?:\s+({ID}))?\s*$")
REF_RE = re.compile(rf"^\s*#ref\s+({ID})\s*$")
OP_RE = re.compile(rf"^move\s+({ID})\s+(before|after|into)\s+({ID})\s*$")


# ── 文档树节点 ────────────────────────────────────────────────

@dataclass
class Line:
    text: str
    origin: int  # 原始文档行号（1 基）


@dataclass
class Ref:
    target: str
    origin: int


@dataclass
class Block:
    bid: str | None  # None 表示文档根
    children: list = field(default_factory=list)
    parent: "Block | None" = None
    origin: int = 0  # #block 标记在原始文档中的行号


# ── 解析 ──────────────────────────────────────────────────────

def parse_document(lines):
    """逐行识别块/引用/文本，返回 (root, errors)。errors: (类别, 行号, 描述)。"""
    root = Block(None)
    stack = [root]
    errors = []
    for lineno, raw in enumerate(lines, 1):
        text = raw.rstrip("\n").rstrip("\r")
        m = BLOCK_START_RE.match(text)
        if m:
            block = Block(m.group(1), parent=stack[-1], origin=lineno)
            stack[-1].children.append(block)
            stack.append(block)
            continue
        m = BLOCK_END_RE.match(text)
        if m:
            if len(stack) == 1:
                errors.append(("unmatched-end", lineno,
                               f"第 {lineno} 行：#endblock 没有匹配的 #block"))
            else:
                block = stack.pop()
                if m.group(1) and m.group(1) != block.bid:
                    errors.append(("end-id-mismatch", lineno,
                                   f"第 {lineno} 行：#endblock {m.group(1)} 与 "
                                   f"第 {block.origin} 行打开的块 {block.bid!r} 不匹配"))
            continue
        m = REF_RE.match(text)
        if m:
            stack[-1].children.append(Ref(m.group(1), lineno))
            continue
        stack[-1].children.append(Line(text, lineno))
    for block in stack[1:]:
        errors.append(("unclosed-block", block.origin,
                       f"块 {block.bid!r} 未闭合，起始于第 {block.origin} 行"
                       f"（按 EOF 处闭合处理）"))
    seen = {}
    for block in walk_blocks(root):
        if block.bid in seen:
            errors.append(("duplicate-id", block.origin,
                           f"块 id {block.bid!r} 重复定义（首次定义在第 "
                           f"{seen[block.bid]} 行）；move/ref 将解析到先出现者"))
        else:
            seen[block.bid] = block.origin
    return root, errors


def walk_blocks(root):
    for child in root.children:
        if isinstance(child, Block):
            yield child
            yield from walk_blocks(child)


# ── 树查询 ────────────────────────────────────────────────────

def find_block(root, bid):
    for block in walk_blocks(root):
        if block.bid == bid:
            return block
    return None


def is_ancestor(ancestor, node):
    p = node.parent
    while p is not None:
        if p is ancestor:
            return True
        p = p.parent
    return False


def path_of(block):
    parts = []
    node = block
    while node is not None and node.bid is not None:
        parts.append(node.bid)
        node = node.parent
    return "/" + "/".join(reversed(parts))


def line_of(root, target):
    """target（Block/Ref/Line）在当前文档状态下的起始行号（1 基）。"""
    counter = [0]
    found = [None]

    def walk(node):
        if found[0] is not None:
            return
        if node is target:
            found[0] = counter[0] + 1
            return
        if isinstance(node, Block):
            if node.bid is not None:
                counter[0] += 1
            for child in node.children:
                walk(child)
            if node.bid is not None:
                counter[0] += 1
        else:
            counter[0] += 1

    walk(root)
    return found[0]


# ── 移动 ──────────────────────────────────────────────────────

def apply_move(root, bid, rel, tid):
    """应用一条移动操作。返回 (ok, 错误描述或 None)。"""
    block = find_block(root, bid)
    if block is None:
        return False, f"块 {bid!r} 不存在"
    target = find_block(root, tid)
    if target is None:
        return False, f"目标块 {tid!r} 不存在"
    if target is block or is_ancestor(block, target):
        return False, (f"非法目标：目标块 {tid!r} 位于被移动块 {bid!r} 内部"
                       f"（或即其自身）")
    block.parent.children.remove(block)
    if rel == "into":
        target.children.append(block)
        block.parent = target
    else:
        parent = target.parent
        idx = parent.children.index(target)
        if rel == "after":
            idx += 1
        parent.children.insert(idx, block)
        block.parent = parent
    return True, None


# ── 悬空引用检查（词法作用域：引用须位于目标块内部）────────────

def find_dangling_refs(root):
    """返回 [(ref, 当前行号, 缺失的作用域链描述)]。"""
    problems = []

    def walk(block, scope):
        for child in block.children:
            if isinstance(child, Ref):
                if child.target not in scope:
                    problems.append((child, line_of(root, child),
                                     ">".join(sorted(scope)) or "(顶层)"))
            elif isinstance(child, Block):
                walk(child, scope | {child.bid})

    walk(root, frozenset())
    return problems


# ── 序列化 ────────────────────────────────────────────────────

def serialize(root):
    out = []

    def walk(block):
        for child in block.children:
            if isinstance(child, Line):
                out.append(child.text)
            elif isinstance(child, Ref):
                out.append(f"#ref {child.target}")
            else:
                out.append(f"#block {child.bid}")
                walk(child)
                out.append(f"#endblock {child.bid}")

    walk(root)
    return out


# ── 主流程 ────────────────────────────────────────────────────

def parse_ops(lines):
    """返回 (ops, errors)。ops: (操作行号, bid, rel, tid, 原文)。"""
    ops, errors = [], []
    for lineno, raw in enumerate(lines, 1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        m = OP_RE.match(text)
        if not m:
            errors.append(("bad-op-syntax", lineno,
                           f"操作第 {lineno} 行语法无法识别：{text!r}；"
                           f"应为 move <id> before|after|into <target-id>"))
            continue
        ops.append((lineno, m.group(1), m.group(2), m.group(3), text))
    return ops, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="文档块移动工具")
    ap.add_argument("doc", help="文档文件（- 表示 stdin）")
    ap.add_argument("ops", help="移动操作文件")
    ap.add_argument("--report", help="错误清单/移动历史输出文件（默认 stderr）")
    args = ap.parse_args(argv)

    doc_lines = sys.stdin if args.doc == "-" else open(args.doc, encoding="utf-8")
    with doc_lines:
        root, errors = parse_document(doc_lines)
    with open(args.ops, encoding="utf-8") as f:
        ops, op_errors = parse_ops(f)
    errors.extend(op_errors)

    history = []
    for seq, (op_lineno, bid, rel, tid, raw) in enumerate(ops, 1):
        block = find_block(root, bid)
        before = (f"第 {line_of(root, block)} 行，路径 {path_of(block)}"
                  if block is not None else "（块不存在）")
        ok, err = apply_move(root, bid, rel, tid)
        if ok:
            after = f"第 {line_of(root, block)} 行，路径 {path_of(block)}"
            history.append(f"  [{seq}] {raw}  =>  OK  移动前: {before}  移动后: {after}")
        else:
            history.append(f"  [{seq}] {raw}  =>  失败（已跳过）  移动前: {before}")
            errors.append(("move-error", op_lineno,
                           f"操作 [{seq}]（操作文件第 {op_lineno} 行）{raw!r}：{err}"))

    for ref, lineno, scope in find_dangling_refs(root):
        errors.append(("dangling-ref", lineno,
                       f"悬空引用：#ref {ref.target}（现第 {lineno} 行，原始第 "
                       f"{ref.origin} 行）不在块 {ref.target!r} 的定义范围内"
                       f"（当前所在作用域：{scope}）"))

    sys.stdout.write("\n".join(serialize(root)) + "\n")

    report = []
    report.append("== 移动历史 ==")
    report.extend(history if history else ["  （无移动操作）"])
    report.append("")
    report.append("== 错误清单 ==")
    if errors:
        for category, lineno, msg in errors:
            report.append(f"  [{category}] {msg}")
    else:
        report.append("  （无错误）")
    report_text = "\n".join(report) + "\n"
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report_text)
    else:
        sys.stderr.write(report_text)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
