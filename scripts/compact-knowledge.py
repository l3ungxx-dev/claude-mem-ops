#!/usr/bin/env python3
"""语义记忆文档整理：project-knowledge.md 正文只保留稳定结论，批次结论归档到 docs/memory/batches/。

背景：AGENTS.md 规定每任务批次追加本文件，但多会话并发追加导致正文膨胀/编号重复。
规则：
- 稳定章节（一~五章 + 维护约定/用户偏好/自动巩固/记忆治理）留在正文；
- 批次结论章节（"六、…""七、…"等日期化批次记录、"打印控件…"）归档到 docs/memory/batches/<标题日期>.md；
- 历史自动巩固快照归档到 docs/memory/auto/archive/project-knowledge-snapshots/；
- 原文先备份到 docs/memory/.versions/，全部内容零丢失（原 `## ` 章节全部出现在归档文件或新正文中）。

用法：
  python3 scripts/memory/compact-knowledge.py --dry-run   # 只报告分类，不改文件
  python3 scripts/memory/compact-knowledge.py             # 执行整理
"""
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from knowledge_rules import classify_section  # noqa: E402

ROOT = Path(os.environ.get("MEMORY_ROOT", str(Path(__file__).resolve().parent.parent / "docs/memory")))
DOC = ROOT / "project-knowledge.md"
BATCHES = ROOT / "batches"
VERSIONS = ROOT / ".versions"
SNAPSHOTS = ROOT / "auto" / "archive" / "project-knowledge-snapshots"

MAX_LINES_TARGET = 200


def split_sections(text):
    """返回 (preamble, [(title, block)])，block 含 `## ` 标题行。"""
    marks = list(re.finditer(r"(?m)^## ", text))
    if not marks:
        return text, []
    preamble = text[: marks[0].start()]
    secs = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = text[m.start():end]
        title = block.split("\n", 1)[0][3:].strip()
        secs.append((title, block))
    return preamble, secs


def classify(title):
    return classify_section(title)


def section_date(title):
    m = re.search(r"(2026-\d{2}-\d{2})", title)
    return m.group(1) if m else datetime.now().strftime("%Y-%m-%d")


def build_new_text(preamble, keep_secs, snapshot_dir_rel):
    parts = [preamble.rstrip(), ""]
    for _title, block in keep_secs:
        parts.append(block.rstrip())
        parts.append("")
    idx = [
        "## 批次结论索引（正文只留稳定结论，批次记录见 `docs/memory/batches/`）",
        "",
        "> 约定：批次交付结论/过程回执写 `docs/memory/batches/<日期>.md`，**不要写进本文件正文**。",
        "> 正文目标 ≤200 行，超限时由 `scripts/memory/compact-knowledge.py` 自动归档（consolidate.py 每日检查）。",
        "",
    ]
    # 索引扫描 batches/ 现有文件，避免只列本次归档日期而丢历史条目
    for f in sorted(BATCHES.glob("*.md")):
        idx.append(f"- `{f.stem}`：批次结论 → [docs/memory/batches/{f.name}](batches/{f.name})")
    idx += [
        f"- 历史自动巩固快照：`{snapshot_dir_rel}`",
        "",
    ]
    parts.append("\n".join(idx))
    return "\n".join(p for p in parts if p is not None).rstrip() + "\n"


def main():
    dry = "--dry-run" in sys.argv
    # 并发保护：读取时记录 mtime，写入前若被其他会话修改则重读重做（最多 3 次）
    for attempt in range(1, 4):
        mtime = DOC.stat().st_mtime
        text = DOC.read_text(encoding="utf-8")
        preamble, secs = split_sections(text)
        groups = {"keep": [], "index": [], "snapshot": [], "batch": []}
        for title, block in secs:
            groups[classify(title)].append((title, block))

        batch_by_date = {}
        for title, block in groups["batch"]:
            batch_by_date.setdefault(section_date(title), []).append((title, block))

        print(f"=== 整理分类（第 {attempt} 次读取，文件 {len(text):,} 字符 / {len(text.splitlines())} 行）===")
        print(f"保留正文 {len(groups['keep'])} 章：")
        for t, _ in groups["keep"]:
            print(f"  [keep]     {t[:60]}")
        for d in sorted(batch_by_date):
            print(f"归档批次 {d}（{len(batch_by_date[d])} 章）：")
            for t, _ in batch_by_date[d]:
                print(f"  [batch]    {t[:60]}")
        if groups["snapshot"]:
            print(f"归档历史快照 {len(groups['snapshot'])} 章：")
            for t, _ in groups["snapshot"]:
                print(f"  [snapshot] {t[:60]}")

        if dry:
            print("\n[dry-run] 未修改文件")
            return

        # 新正文
        snap_rel = str(SNAPSHOTS.relative_to(ROOT))
        new_text = build_new_text(preamble, groups["keep"], snap_rel)

        # 零丢失校验：所有原章节标题必须出现在新正文或归档内容中
        all_new = new_text
        batch_contents = {}
        for d, items in batch_by_date.items():
            batch_contents[d] = "\n".join(b for _, b in items)
            all_new += batch_contents[d]
        snap_content = "\n".join(b for _, b in groups["snapshot"])
        all_new += snap_content
        missing = [t for t, _ in secs if t not in all_new]
        if missing:
            print(f"ERROR: 校验失败，以下章节将丢失: {missing}", file=sys.stderr)
            sys.exit(1)

        # 正文无变化时跳过（幂等：避免无意义刷新 mtime 触发其他会话 edit 冲突）
        if new_text == text:
            print("\n正文无需变化（幂等），跳过写入")
            return

        # 写入前并发检查
        if DOC.stat().st_mtime != mtime:
            print(f"WARN: 文件在读取后被其他会话修改，重试（{attempt}/3）")
            time.sleep(1)
            continue

        # 备份原文
        VERSIONS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DOC, VERSIONS / f"project-knowledge.{stamp}.md")
        print(f"\n原文已备份 → docs/memory/.versions/project-knowledge.{stamp}.md")

        # 写批次归档（追加式：保留已有文件内容）
        BATCHES.mkdir(parents=True, exist_ok=True)
        for d, content in batch_contents.items():
            f = BATCHES / f"{d}.md"
            header = f"# 批次结论归档 {d}\n\n> 从 `docs/memory/project-knowledge.md` 正文移出（归档于 {datetime.now():%Y-%m-%d %H:%M}），原文保留。\n\n"
            if f.exists():
                old = f.read_text(encoding="utf-8")
                if content.strip() not in old:
                    f.write_text(old.rstrip() + "\n\n---\n\n" + content.strip() + "\n", encoding="utf-8")
            else:
                f.write_text(header + content.strip() + "\n", encoding="utf-8")
            print(f"批次归档 → docs/memory/batches/{d}.md")

        # 写历史快照归档
        if snap_content.strip():
            SNAPSHOTS.mkdir(parents=True, exist_ok=True)
            snap = SNAPSHOTS / f"{datetime.now():%Y-%m-%d}.md"
            header = f"# project-knowledge 历史自动巩固快照（归档）\n\n> 归档于 {datetime.now():%Y-%m-%d %H:%M}，原文保留。\n\n"
            if snap.exists():
                old = snap.read_text(encoding="utf-8")
                if snap_content.strip() not in old:
                    snap.write_text(old.rstrip() + "\n\n---\n\n" + snap_content.strip() + "\n", encoding="utf-8")
            else:
                snap.write_text(header + snap_content.strip() + "\n", encoding="utf-8")
            print(f"历史快照归档 → docs/memory/auto/archive/project-knowledge-snapshots/{snap.name}")

        # 写新正文
        DOC.write_text(new_text, encoding="utf-8")
        lines = len(new_text.splitlines())
        print(f"\n正文已整理：{lines} 行 / {len(new_text):,} 字符")
        if lines > MAX_LINES_TARGET:
            print(f"WARN: 正文仍超过 {MAX_LINES_TARGET} 行，请人工复核 keep 章节内容")
        return

    print("ERROR: 并发冲突重试 3 次仍失败，放弃（未修改文件）", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
