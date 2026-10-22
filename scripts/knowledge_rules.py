#!/usr/bin/env python3
"""project-knowledge.md 章节分类规则（单一来源）。

consolidate.py 的语义层守门与 compact-knowledge.py 的整理共用本模块，
避免两处规则漂移导致「守门误判稳定章节」或「整理误归档稳定规则」。

分类：
- keep:     稳定知识章节（一~五章、"稳定结论/稳定规则"标记章、无法判定时保守保留）
- index:    批次结论索引（由脚本每次重建，不归档）
- snapshot: 历史自动巩固快照（旧版自动追加内容，归档到 auto/archive/）
- batch:    批次交付结论/过程回执（归档到 docs/memory/batches/<日期>.md）
"""
import re

KEEP_PREFIX = ("一、", "二、", "三、", "四、", "五、")
SNAPSHOT_MARKERS = ("附录、自动巩固历史快照", "自动巩固（DeepSeek V41-Flash 归纳）")
STABLE_MARKERS = ("稳定结论", "稳定规则")
INDEX_MARKER = "批次结论索引"
BATCH_SEMANTIC = re.compile(r"批次|收口|记录|归档")
DATE_RE = re.compile(r"2026-\d{2}-\d{2}")
OLD_BATCH_NUM = re.compile(r"^[六七八九十]+、")


def classify_section(title: str) -> str:
    if INDEX_MARKER in title:
        return "index"
    # 明确标注稳定结论/稳定规则的章节，即使带日期也保留在正文
    if any(k in title for k in STABLE_MARKERS):
        return "keep"
    if title.startswith(KEEP_PREFIX):
        return "keep"
    if any(k in title for k in SNAPSHOT_MARKERS):
        return "snapshot"
    # 带日期且带批次语义 → 批次归档
    if DATE_RE.search(title) and BATCH_SEMANTIC.search(title):
        return "batch"
    # 六~十编号的日期化章节（旧批次记录）→ 批次归档
    if OLD_BATCH_NUM.match(title) and DATE_RE.search(title):
        return "batch"
    # 不确定时保守保留在正文，避免误删稳定知识
    return "keep"
