#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
blockmove.py — 文档块移动工具（纯 Python 标准库，单文件）

用法:
    python3 blockmove.py DOC OPS
    DOC : 文档文件路径，'-' 表示从 stdin 读取
    OPS : 移动操作文件路径（每行一条操作，空行与 # 开头的行忽略）

输出:
    stdout : 移动后的文档全文
    stderr : 错误报告 + 移动历史（可用 2> 重定向分离）

============================= 语法设计（及理由） =============================

块定义（逐行识别，标记独占一行，允许前导空白）:
    @block <名字>        块开始
    @end [名字]          块结束（名字可省略；若给出则校验匹配，不匹配报错但仍闭合）
  理由: 块必须可被移动操作点名，所以开始标记强制携带名字；@end 允许带名字
        是为了让嵌套场景下的人眼/机器校验更容易（不匹配可诊断）。

引用（块内一行）:
    @ref <名字>          引用某个块
  绑定规则（词法作用域 + 顶层全局）:
    1. 若引用点被名为 <名字> 的块包围（含引用所在的块自身），绑定到最近的包围块；
    2. 否则，若存在先于该引用出现的顶层块 <名字>，绑定到它（全局前向定义）；
    3. 都不满足 => 悬空引用，报告引用所在行号。
  理由: “块移动后引用跟着走并保持绑定” 最自然的语义是词法作用域——引用随块
        一起搬运时，只要定义它的祖先块一起移动，绑定就保持；移出定义范围即悬空。
        追加“顶层前向定义”是为了让同级的顺序引用（最常见写法）不算悬空。

移动操作（逐条应用，后一条看到的是前一条的结果）:
    move <名字> before <目标块>    移到目标块之前（与目标同级）
    move <名字> after  <目标块>    移到目标块之后（与目标同级）
    move <名字> into   <目标块>    移为目标块的最后一个子块（改变嵌套）
    move <名字> to-start           移到文档最前（顶层）
    move <名字> to-end             移到文档最后（顶层）
  理由: before/after/into 用“块名”而非行号定位目标——行号在多次移动后会
        漂移，块名是稳定标识；into 显式支持嵌套关系改变；to-start/to-end
        覆盖无参照物的场景。

============================= 设计取舍 =============================

* 树而非文本切割: 解析成树后移动 = 摘节点 + 插节点，嵌套关系由树结构天然
  维护；移动父块时其整个子树（含内部块与引用）随行，不会切错行。
* 逐条应用: 每条操作在最新文档树上执行并记录前后行号，历史可追溯；
  单条失败不影响后续操作（报告错误并继续）。
* 非法目标: 目标块是被移动块自身或其后代 => 拒绝（否则会产生环/自嵌套）。
* 重名块: 允许存在；move 与 @ref 均绑定文档序第一个/最近者，并在报告中
  给出警告。取舍: 不强制唯一名，因为文档可能来自外部；用警告代替硬错误。
* 未闭合块: 报错（含起始行号），并宽容地视为延伸到文件末尾，仍参与移动
  与输出，保证“尽力产出”。
* 悬空检查时机: 在全部移动完成后的最终文档上检查，报告的是最终行号；
  中间态的暂时悬空不报（可能被后续移动修复）。
* 输出保真: 原样保留每行文本（含 @block/@end 原文）；未闭合块不补 @end，
  让输出与错误报告互相印证。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field


# ------------------------------ 数据模型 ------------------------------

@dataclass
class TextLine:
    text: str                      # 原始行文本（不含换行）
    ref: str | None = None         # 若为 @ref 行，记录被引用的块名


@dataclass
class Block:
    name: str
    start_line: int                # 原始文档中的起始行号（1 基，用于报错）
    start_text: str                # @block 行原文
    end_text: str | None = None    # @end 行原文（未闭合则为 None）
    closed: bool = True
    parent: "Block | Root | None" = None
    children: list = field(default_factory=list)   # TextLine | Block


@dataclass
class Root:
    parent: None = None            # 统一 parent 链接口，便于祖先遍历
    children: list = field(default_factory=list)


# ------------------------------ 解析 ------------------------------

def parse_document(lines: list[str]):
    """逐行识别块结构，返回 (树根, 错误列表)。"""
    root = Root()
    stack: list = [root]
    errors: list[str] = []

    for lineno, raw in enumerate(lines, 1):
        text = raw.rstrip("\n")
        tokens = text.strip().split()

        if tokens and tokens[0] == "@block" and len(tokens) >= 2:
            blk = Block(name=tokens[1], start_line=lineno,
                        start_text=text, parent=stack[-1])
            stack[-1].children.append(blk)
            stack.append(blk)
        elif tokens and tokens[0] == "@end":
            if len(stack) == 1:
                errors.append(f"第 {lineno} 行: 多余的 @end（无匹配的 @block），按普通行保留")
                stack[0].children.append(TextLine(text))
            else:
                blk = stack.pop()
                blk.end_text = text
                if len(tokens) >= 2 and tokens[1] != blk.name:
                    errors.append(
                        f"第 {lineno} 行: '@end {tokens[1]}' 与第 {blk.start_line} 行 "
                        f"'@block {blk.name}' 不匹配（仍按闭合 {blk.name} 处理）")
        else:
            ref = tokens[1] if (tokens and tokens[0] == "@ref" and len(tokens) >= 2) else None
            stack[-1].children.append(TextLine(text, ref))

    for blk in stack[1:]:  # 文件结束仍未闭合的块
        blk.closed = False
        errors.append(f"块 '{blk.name}' 未闭合: 起始于第 {blk.start_line} 行")
    return root, errors


# ------------------------------ 树工具 ------------------------------

def iter_blocks(node):
    for child in node.children:
        if isinstance(child, Block):
            yield child
            yield from iter_blocks(child)


def find_block(root, name):
    """返回文档序中第一个同名块。"""
    for blk in iter_blocks(root):
        if blk.name == name:
            return blk
    return None


def is_ancestor(anc, node) -> bool:
    """anc 是否为 node 的祖先（不含自身）。"""
    p = node.parent
    while p is not None:
        if p is anc:
            return True
        p = p.parent
    return False


def render(root):
    """把树渲染回行序列，并记录每个节点在输出中的行号（1 基）。

    返回 (lines, positions)；positions 以 id(node) 为键。
    """
    lines: list[str] = []
    positions: dict[int, int] = {}

    def walk(node):
        for child in node.children:
            if isinstance(child, Block):
                positions[id(child)] = len(lines) + 1
                lines.append(child.start_text)
                walk(child)
                if child.closed:
                    lines.append(child.end_text if child.end_text is not None
                                 else f"@end {child.name}")
            else:
                positions[id(child)] = len(lines) + 1
                lines.append(child.text)

    walk(root)
    return lines, positions


# ------------------------------ 移动操作 ------------------------------

def apply_op(root, op_text: str, op_index: int, errors: list[str]) -> dict:
    """应用一条移动操作，返回历史记录；失败时记录错误且不改动文档。"""
    rec = {"index": op_index, "op": op_text, "ok": False,
           "before": None, "after": None, "message": ""}
    tokens = op_text.split()

    def fail(msg):
        rec["message"] = msg
        errors.append(f"操作 {op_index} ({op_text!r}): {msg}")
        return rec

    if len(tokens) < 3 or tokens[0] != "move":
        return fail("语法错误，应为: move <名字> before|after|into <目标块> | "
                    "move <名字> to-start|to-end")
    name, mode = tokens[1], tokens[2]

    blk = find_block(root, name)
    if blk is None:
        return fail(f"引用了不存在的块 '{name}'")

    # 确定新父节点与插入下标（基于摘除前的树）
    if mode in ("before", "after", "into"):
        if len(tokens) < 4:
            return fail(f"模式 {mode} 缺少目标块名")
        target = find_block(root, tokens[3])
        if target is None:
            return fail(f"目标块 '{tokens[3]}' 不存在")
        if target is blk or is_ancestor(blk, target):
            return fail(f"非法目标: 目标块 '{target.name}' 是被移动块 "
                        f"'{name}' 自身或其后代")
        if mode == "into":
            new_parent, idx = target, len(target.children)
        else:
            new_parent = target.parent
            idx = new_parent.children.index(target) + (1 if mode == "after" else 0)
    elif mode == "to-start":
        new_parent, idx = root, 0
    elif mode == "to-end":
        new_parent, idx = root, len(root.children)
    else:
        return fail(f"未知模式 '{mode}'")

    _, pos = render(root)
    rec["before"] = pos[id(blk)]

    # 摘节点 + 插节点（嵌套关系随树结构自动更新）
    old_parent = blk.parent
    old_idx = old_parent.children.index(blk)
    old_parent.children.remove(blk)
    if new_parent is old_parent and old_idx < idx:
        idx -= 1  # 同父移动：摘除后插入点左移一格
    blk.parent = new_parent
    new_parent.children.insert(idx, blk)

    _, pos = render(root)
    rec["after"] = pos[id(blk)]
    rec["ok"] = True
    rec["message"] = f"块 '{name}': 第 {rec['before']} 行 -> 第 {rec['after']} 行"
    return rec


# ------------------------------ 悬空引用检查 ------------------------------

def check_dangling(root, positions) -> list[str]:
    """在最终文档上检查悬空引用，返回问题描述列表（含引用所在行号）。"""
    blocks_by_name: dict[str, list[Block]] = {}
    for blk in iter_blocks(root):
        blocks_by_name.setdefault(blk.name, []).append(blk)

    problems: list[str] = []

    def walk(node, ancestors):
        for child in node.children:
            if isinstance(child, Block):
                walk(child, ancestors + [child])
            elif child.ref is not None:
                target = child.ref
                # 规则 1: 词法作用域（含自身所在块）
                bound = any(a.name == target for a in ancestors)
                # 规则 2: 先于引用出现的顶层块（全局前向定义）
                if not bound:
                    my_line = positions[id(child)]
                    bound = any(
                        b.parent is root and positions[id(b)] < my_line
                        for b in blocks_by_name.get(target, []))
                if not bound:
                    problems.append(
                        f"第 {positions[id(child)]} 行: 悬空引用 '@ref {target}'"
                        f"（引用已移出块 '{target}' 的定义范围，或该块不存在）")

    walk(root, [])
    return problems


def duplicate_warnings(root) -> list[str]:
    seen: dict[str, Block] = {}
    warnings: list[str] = []
    for blk in iter_blocks(root):
        if blk.name in seen:
            warnings.append(
                f"块名 '{blk.name}' 重复（首见于第 {seen[blk.name].start_line} 行，"
                f"又见于第 {blk.start_line} 行）；move/@ref 均绑定文档序第一个/最近者")
        else:
            seen[blk.name] = blk
    return warnings


# ------------------------------ 主流程 ------------------------------

def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] in ("-h", "--help"):
        print(__doc__.strip(), file=sys.stderr if len(argv) == 3 else sys.stdout)
        return 0 if len(argv) != 3 else 2

    doc_path, ops_path = argv[1], argv[2]

    if doc_path == "-":
        doc_lines = sys.stdin.read().splitlines()
    else:
        with open(doc_path, encoding="utf-8") as f:
            doc_lines = f.read().splitlines()
    with open(ops_path, encoding="utf-8") as f:
        op_lines = [ln.strip() for ln in f.read().splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]

    root, errors = parse_document(doc_lines)

    history = []
    for i, op in enumerate(op_lines, 1):
        history.append(apply_op(root, op, i, errors))

    out_lines, positions = render(root)
    errors.extend(check_dangling(root, positions))
    warnings = duplicate_warnings(root)

    # stdout: 移动后的文档
    sys.stdout.write("\n".join(out_lines) + ("\n" if out_lines else ""))

    # stderr: 错误报告 + 移动历史
    report = sys.stderr
    print("===== 移动历史 =====", file=report)
    if history:
        for rec in history:
            status = "OK " if rec["ok"] else "失败"
            print(f"  [{status}] 操作 {rec['index']}: {rec['op']}  --  {rec['message']}",
                  file=report)
    else:
        print("  （无移动操作）", file=report)

    print("===== 错误报告 =====", file=report)
    if errors:
        for e in errors:
            print(f"  [错误] {e}", file=report)
    else:
        print("  （无错误）", file=report)
    for w in warnings:
        print(f"  [警告] {w}", file=report)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
