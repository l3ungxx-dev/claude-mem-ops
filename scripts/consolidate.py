#!/usr/bin/env python3
"""claude-mem 分层时间记忆巩固：小时 → 日 → 周 → 月 → 年。

设计：
- 每层只做"归纳总结"，不删除原始内容；被压缩的原始文件移到 auto/archive/ 保留可追溯。
- L0 = claude-mem observations（原始观察，DB 保留）
- L1 hourly: 每小时增量观察 → auto/hourly/YYYY-MM-DD/HH.md
- L2 daily:  当天观察 → auto/daily/YYYY-MM-DD.md
- L3 weekly: 当周 daily → auto/weekly/YYYY-Www.md
- L4 monthly: 当月 weekly → auto/monthly/YYYY-MM.md
- L5 yearly: 当年 monthly → auto/yearly/YYYY.md

用法：
  consolidate.py --layer hourly|daily|weekly|monthly|yearly
  consolidate.py --auto            # 按当前时间自动决定跑哪些层（供 timer 调用）
  consolidate.py --layer hourly --force --dry-run
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(os.environ.get("MEMORY_ROOT", str(Path(__file__).resolve().parent.parent / "docs/memory")))
AUTO = ROOT / "auto"
KNOWLEDGE = ROOT / "project-knowledge.md"  # 人工语义记忆层（自动巩固不写它，只做守门检查）
DB = os.environ.get("CLAUDE_MEM_DB", os.path.expanduser("~/.claude-mem/claude-mem.db"))
ENV_FILE = os.path.expanduser("~/.claude-mem/consolidate.env")
STATE = os.path.expanduser("~/.claude-mem/consolidate-state.json")
LOG = os.environ.get("CONSOLIDATE_LOG", os.path.expanduser("~/.local/state/claude-mem-consolidate.log"))
API_BASE = os.environ.get("CONSOLIDATE_API_BASE", "https://api.deepseek.com/v1")
MAX_CHARS = int(os.environ.get("CONSOLIDATE_MAX_CHARS", "400000"))
# 原文截断档位：超上限时逐级缩短原文（保条目覆盖、只丢细节），仍超再采样
RAW_LIMITS = (600, 300, 150, 80)
MIN_OBS = int(os.environ.get("CONSOLIDATE_MIN_OBS", "15"))
# 各层输出 token 上限：日/周/月/年输入更长，需更大预算避免 finish_reason=length 截断
MAX_TOKENS = {"hourly": 8000, "daily": 16000, "weekly": 16000, "monthly": 20000, "yearly": 24000}
# 语义文档守门阈值（行数）：超过告警线提示整理，超过自动线调用 compact-knowledge.py 归档批次章节
SEMANTIC_WARN_LINES = 250
SEMANTIC_AUTO_COMPACT_LINES = 300

PROMPTS = {
    "hourly": (
        "你是记忆巩固代理。下面是项目最近一小时的观察记录（已过滤低价值调用）。"
        "请归纳为极简要点，标题固定为：### 决策与变更 / ### 踩坑与修复 / ### 规则与环境 / ### 待办线索。"
        "要求：中文；只写结论/路径/原因；每节最多 3 条、每条 ≤2 行；总输出 ≤800 字符；无内容写「无」；不复述过程。"
    ),
    "daily": (
        "你是记忆巩固代理。下面是项目当天的观察记录。请归纳成当天日报，标题固定为："
        "### 决策与变更 / ### 踩坑与修复 / ### 规则与环境 / ### 待办线索。"
        "要求：中文；去重、合并重复项；只写结论/路径/原因；每节 ≤6 条、每条 ≤2 行；总输出 ≤2000 字符；"
        "保留具体文件/命令/参数等可检索线索；涉及用户偏好、协作约定、长期约束的内容必须保留在「规则与环境」中，不得省略；无内容写「无」。"
    ),
    "weekly": (
        "你是记忆巩固代理。下面是项目本周每天的日报。请归纳成周报，标题固定为："
        "### 本周关键决策 / ### 问题与修复 / ### 稳定规则 / ### 遗留待办。"
        "要求：中文；跨天合并重复结论，保留 1 周后仍有价值的规则与坑；用户偏好与协作规则类条目优先保留；每节 ≤6 条；总输出 ≤2000 字符；"
        "丢弃一次性过程细节；无内容写「无」。"
    ),
    "monthly": (
        "你是记忆巩固代理。下面是项目本月的周报。请归纳成月报，标题固定为："
        "### 本月里程碑 / ### 关键经验 / ### 长期规则 / ### 待办与风险。"
        "要求：中文；只保留跨周仍成立的结论与方法论；必须保留用户偏好与协作规则；每节 ≤6 条；总输出 ≤2500 字符；无内容写「无」。"
    ),
    "yearly": (
        "你是记忆巩固代理。下面是项目本年度的月报。请归纳成年报，标题固定为："
        "### 全年主线 / ### 核心经验 / ### 项目规则 / ### 长期路线与风险。"
        "要求：中文；只保留全年维度仍有价值的结论；用户协作心智模型与长期偏好必须保留；每节 ≤8 条；总输出 ≤3000 字符；无内容写「无」。"
    ),
}


def log(msg):
    # 日志写失败不阻断主流程（如 DSH 沙箱对 ~/.local/state 只读时），避免因日志权限导致巩固中断
    try:
        log_dir = os.path.dirname(LOG)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


def read_key():
    if not os.path.exists(ENV_FILE):
        return None
    for line in open(ENV_FILE, encoding="utf-8"):
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None


def read_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def write_state(**kw):
    st = read_state()
    st.update(kw)
    st["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    try:
        json.dump(st, open(STATE, "w", encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"WARN: 无法写 state: {e}")


def _extract_raw_snippet(tool_name, tinput, toutput, limit=600):
    """从工具调用原始记录提取关键内容（本地不总结、保留原文，供云端巩固提炼细节）。"""
    name = (tool_name or "").lower()
    try:
        d = json.loads(tinput) if tinput else {}
    except Exception:  # noqa: BLE001
        d = {}
    if not isinstance(d, dict):
        d = {}
    if name in ("write", "create_file"):
        fp = d.get("file_path") or d.get("path") or ""
        return f"write {fp}: {str(d.get('content') or d.get('file_text') or '')[:limit]}"
    if name in ("edit", "str_replace", "apply_patch"):
        fp = d.get("file_path") or d.get("path") or ""
        old = str(d.get("old_string") or d.get("old_str") or "")
        new = str(d.get("new_string") or d.get("new_str") or d.get("content") or "")
        return f"edit {fp}: old={old[:200]} new={new[:limit]}"
    if name == "bash":
        cmd = str(d.get("command") or d.get("cmd") or "")
        out = (toutput or "")[:300].replace("\n", " ")
        return f"bash: {cmd[:300]} => {out}" if cmd else ""
    if name == "send_message":
        return f"send_message: {str(d.get('message') or '')[:limit]}"
    if name.startswith("team_task") or name in ("spawn_teammate", "subagent"):
        return f"{name}: {str(tinput or '')[:limit]}"
    return f"{name}: {str(tinput or '')[:limit]}" if tinput else ""


def fetch_observations(start_ms, end_ms=None):
    """复制 DB（含 WAL）到临时目录后只读。

    返回「观察索引 + 关联原始工具调用全文摘要」：本地小模型只做轻量索引，
    细节由原始记录（tool_uses，保留 30 天）承载，云端巩固时从原文提炼，避免小模型总结丢细节。
    """
    tmpdir = tempfile.mkdtemp(prefix="cmem-consolidate-")
    try:
        db = os.path.join(tmpdir, "m.db")
        shutil.copy(DB, db)
        wal = DB + "-wal"
        if os.path.exists(wal):
            shutil.copy(wal, db + "-wal")
        con = sqlite3.connect(db)
        con.execute("PRAGMA temp_store=MEMORY")
        sql = ("SELECT id, type, title, narrative, facts, concepts, created_at_epoch FROM observations "
               "WHERE created_at_epoch >= ?")
        args = [start_ms]
        if end_ms:
            sql += " AND created_at_epoch < ?"
            args.append(end_ms)
        rows = con.execute(sql + " ORDER BY created_at_epoch", args).fetchall()
        # 关联原始工具调用（observation_id -> tool_uses），保存原文 tuple 供动态截断
        raw_map = {}
        ids = [r[0] for r in rows]
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            q = ("SELECT observation_id, tool_name, tool_input, tool_response FROM tool_uses "
                 f"WHERE observation_id IN ({','.join('?' * len(chunk))})")
            for oid, tname, tinput, toutput in con.execute(q, chunk):
                raw_map.setdefault(oid, []).append((tname, tinput, toutput))
        con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    def build_items(raw_limit):
        items = []
        for obs_id, typ, title, nar, facts, concepts, ts in rows:
            raw_segs = []
            for tname, tinput, toutput in raw_map.get(obs_id, []):
                x = _extract_raw_snippet(tname, tinput, toutput, limit=raw_limit)
                if x:
                    raw_segs.append(x)
            raw_txt = ("\n  原文: " + " || ".join(raw_segs)) if raw_segs else ""
            seg = (f"- [{typ or '?'}] {title or ''}\n  叙事: {(nar or '')[:250]}\n"
                   f"  事实: {(facts or '')[:250]}{raw_txt}")
            items.append((typ, seg))
        return items

    if not rows:
        return [], 0
    # 逐级缩短原文，尽量保住每一条目（只丢细节）；全部档位仍超限才采样
    ordered = None
    for raw_limit in RAW_LIMITS:
        candidate = build_items(raw_limit)
        high_types = {"decision", "bugfix", "feature", "change", "security_note",
                      "security_alert", "gotcha", "refactor"}
        high = [x for x in candidate if x[0] in high_types]
        low = [x for x in candidate if x[0] not in high_types]
        candidate = high + low
        if sum(len(x[1]) for x in candidate) <= MAX_CHARS:
            ordered = candidate
            break
    if ordered is None:
        # 极端情况：均匀采样保证覆盖整个时间窗口而不是只取最早
        total_len = sum(len(x[1]) for x in candidate)
        step = max(1, round(total_len / MAX_CHARS))
        ordered = candidate[::step]
    segs, total = [], 0
    for typ, seg in ordered:
        if total + len(seg) > MAX_CHARS:
            break
        segs.append(seg)
        total += len(seg)
    return segs, len(rows)


def record_cost(cost, layer):
    """累计每日巩固成本到 state，超阈值打 WARN（兜底：无人看也不失控）。"""
    try:
        st = read_state()
        today = datetime.now().strftime("%Y-%m-%d")
        if st.get("costDate") != today:
            st["costDate"] = today
            st["costToday"] = 0.0
        st["costToday"] = round(float(st.get("costToday") or 0.0) + cost, 4)
        st["costTotal"] = round(float(st.get("costTotal") or 0.0) + cost, 4)
        json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
        warn = float(os.environ.get("CONSOLIDATE_DAILY_COST_WARN", "10"))
        if st["costToday"] > warn:
            log(f"WARN: 今日巩固成本 ¥{st['costToday']:.2f} 已超阈值 ¥{warn:.0f}")
    except Exception as e:  # noqa: BLE001
        log(f"WARN: 成本记账失败: {e}")


def call_llm(key, model, layer, user_text):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPTS[layer]},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.3,
        "max_tokens": MAX_TOKENS.get(layer, 8000),
        "reasoning_effort": "low",
        "stream": False,
    }
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.load(r)
    choice = d["choices"][0]
    content = (choice.get("message") or {}).get("content") or ""
    usage = d.get("usage") or {}
    pt, ct = usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0
    n = datetime.now()
    if n.weekday() >= 5:
        peak = False
    else:
        hm = n.hour * 60 + n.minute
        peak = (9 * 60 <= hm < 12 * 60) or (14 * 60 <= hm < 18 * 60)
    pin, pout = (2.0, 8.0) if peak else (1.0, 4.0)
    meta = {
        "finish_reason": choice.get("finish_reason"),
        "content_len": len(content),
        "reasoning_len": len((choice.get("message") or {}).get("reasoning_content") or ""),
        "max_tokens": body["max_tokens"],
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "cost_cny": round(pt / 1e6 * pin + ct / 1e6 * pout, 4),
    }
    log(f"[{layer}] API 响应: {json.dumps(meta, ensure_ascii=False)}")
    record_cost(meta.get("cost_cny") or 0.0, layer)
    if not content.strip():
        raise RuntimeError(f"API 返回空 content: {json.dumps(meta, ensure_ascii=False)}")
    # 输出被 max_tokens 截断时不能落盘（会覆盖完整旧版），抛错触发重试
    if choice.get("finish_reason") == "length":
        raise RuntimeError(f"输出被截断(finish_reason=length): {json.dumps(meta, ensure_ascii=False)}")
    return content


def call_llm_retry(key, model, layer, user_text):
    for attempt in range(1, 4):
        try:
            return call_llm(key, model, layer, user_text)
        except Exception as e:  # noqa: BLE001
            log(f"[{layer}] 第 {attempt} 次失败: {e}")
            if attempt == 3:
                raise
            time.sleep(5)
    raise RuntimeError("unreachable")


def context_board_summary():
    """读取每小时上下文看板，生成超阈值会话摘要（写入日报，供 Lead 早上看到）。"""
    board = ROOT / ".monitor" / "context-board.md"
    try:
        text = board.read_text(encoding="utf-8")
    except OSError:
        return None
    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or "会话" in line or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        sid, role, ctx, *rest = cells
        status = cells[-1]
        if "🟠" in status or "🔴" in status:
            rows.append(f"{sid[:16]}（{role}）{ctx}")
    if not rows:
        return None
    return "上下文看板告警：" + "；".join(rows[:6])


def write_layer(layer, rel_path, title, content):
    path = AUTO / layer / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # 旧版本留档：目标已存在时先备份，避免重复归纳覆盖历史版本（记忆防损）
    if path.exists():
        ver_dir = AUTO / ".versions" / layer
        ver_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, ver_dir / f"{path.stem}.{stamp}{path.suffix}")
            log(f"[{layer}] 旧版本已备份 → {ver_dir / path.stem}.{stamp}{path.suffix}")
        except OSError as e:
            log(f"[{layer}] WARN: 旧版本备份失败: {e}")
    header = f"# {title}\n\n> 自动巩固（DeepSeek V4.1-Flash）{datetime.now():%Y-%m-%d %H:%M}\n\n"
    path.write_text(header + content.strip() + "\n", encoding="utf-8")
    return path


def archive(src: Path, dst_dir: Path):
    if not src.exists():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / src.name
    if target.exists():
        shutil.rmtree(target) if target.is_dir() else target.unlink()
    shutil.move(str(src), str(target))


def collect_files(layer, pattern):
    d = AUTO / layer
    if not d.exists():
        return []
    return sorted(d.glob(pattern))


# ---------- 各层实现 ----------

def run_hourly(key, model, dry_run=False, force=False):
    now = datetime.now()
    st = read_state()
    since = st.get("lastRunMs")
    since_ms = int(since) if since else int((now - timedelta(hours=1)).timestamp() * 1000)
    obs, total = fetch_observations(since_ms)
    log(f"[hourly] 窗口自 {datetime.fromtimestamp(since_ms/1000):%m-%d %H:%M}，观察 {total} 条，取 {len(obs)}")
    if not obs:
        print("hourly: 无新观察，跳过")
        return "skip"
    if len(obs) < MIN_OBS and not force:
        print(f"hourly: 仅 {len(obs)} 条（< {MIN_OBS}），跳过")
        return "skip"
    if dry_run:
        print(f"hourly dry-run: {len(obs)} 条观察，前 2 条：\n{obs[0][:300] if obs else ''}")
        return "dry"
    content = call_llm_retry(key, model, "hourly", "\n".join(obs))
    path = write_layer("hourly", f"{now:%Y-%m-%d}/{now:%H}.md", f"小时巩固 {now:%Y-%m-%d %H}:00", content)
    write_state(lastRunMs=int(now.timestamp() * 1000))
    print(f"hourly 完成: {len(obs)} 条 → {path}")
    return "ok"


def run_daily(key, model, dry_run=False, date_str=None):
    now = datetime.now()
    if date_str:
        day = datetime.strptime(date_str, "%Y-%m-%d")
    else:
        day = datetime(now.year, now.month, now.day)
    day_start = day
    obs, total = fetch_observations(int(day_start.timestamp() * 1000),
                                    int((day_start + timedelta(days=1)).timestamp() * 1000))
    log(f"[daily] {day:%Y-%m-%d} 观察 {total} 条，取 {len(obs)}")
    if not obs:
        print(f"daily: {day:%Y-%m-%d} 无观察，跳过")
        return "skip"
    if dry_run:
        print(f"daily dry-run: {day:%Y-%m-%d} {len(obs)} 条观察")
        return "dry"
    content = call_llm_retry(key, model, "daily", "\n".join(obs))
    board_note = context_board_summary()
    if board_note:
        content = content.rstrip() + "\n\n> " + board_note + "\n"
        log(f"[daily] 已附带上下文看板摘要")
    # 附带上下文治理执行率（基线后统计，每日累计，供 Lead 早上读日报时看到）
    try:
        import subprocess

        r = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("context-metrics.py")), "--quiet"],
            capture_output=True, text=True, timeout=900,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            content = content.rstrip() + "\n\n> " + r.stdout.strip() + "\n"
            log("[daily] 已附带上下文治理执行率")
        else:
            log(f"[daily] WARN: 上下文度量失败（退出码 {r.returncode}）")
    except Exception as e:  # noqa: BLE001
        log(f"[daily] WARN: 上下文度量异常: {e}")
    path = write_layer("daily", f"{day:%Y-%m-%d}.md", f"日汇总 {day:%Y-%m-%d}", content)
    # 归档当天小时文件
    hourly_dir = AUTO / "hourly" / f"{day:%Y-%m-%d}"
    archive(hourly_dir, AUTO / "archive" / "hourly" / f"{day:%Y-%m}")
    print(f"daily 完成: {len(obs)} 条 → {path}")
    return "ok"


def run_weekly(key, model, dry_run=False):
    now = datetime.now()
    monday = now - timedelta(days=now.weekday())
    days = [monday + timedelta(days=i) for i in range(7)]
    texts, sys_chars = [], 0
    for d in days:
        f = AUTO / "daily" / f"{d:%Y-%m-%d}.md"
        if f.exists():
            t = f.read_text(encoding="utf-8")
            if sys_chars + len(t) > MAX_CHARS:
                break
            texts.append(f"## {d:%Y-%m-%d}\n{t}")
            sys_chars += len(t)
    log(f"[weekly] 收集 {len(texts)} 天日报")
    if not texts:
        print("weekly: 本周无日报，跳过")
        return "skip"
    if dry_run:
        print(f"weekly dry-run: {len(texts)} 天")
        return "dry"
    content = call_llm_retry(key, model, "weekly", "\n\n".join(texts))
    # 附加上下文治理周度量（扫描会话，可能耗时数分钟；失败不影响周报）
    try:
        import subprocess

        r = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("context-metrics.py")), "--days", "7", "--quiet"],
            capture_output=True, text=True, timeout=900,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            content = content.rstrip() + "\n\n> " + r.stdout.strip() + "\n"
            log("[weekly] 已附带上下文治理周度量")
        else:
            log(f"[weekly] WARN: 周度量失败（退出码 {r.returncode}）: {(r.stderr or '')[:200]}")
    except Exception as e:  # noqa: BLE001
        log(f"[weekly] WARN: 周度量异常: {e}")
    iso = now.isocalendar()
    path = write_layer("weekly", f"{iso.year}-W{iso.week:02d}.md", f"周汇总 {iso.year}-W{iso.week:02d}", content)
    for d in days:
        archive(AUTO / "daily" / f"{d:%Y-%m-%d}.md", AUTO / "archive" / "daily" / f"{d:%Y-%m}")
    print(f"weekly 完成 → {path}")
    return "ok"


def run_monthly(key, model, dry_run=False):
    now = datetime.now()
    texts, chars = [], 0
    for f in collect_files("weekly", "*.md"):
        t = f.read_text(encoding="utf-8")
        if chars + len(t) > MAX_CHARS:
            break
        texts.append(f"## {f.stem}\n{t}")
        chars += len(t)
    log(f"[monthly] 收集 {len(texts)} 周报")
    if not texts:
        print("monthly: 无周报，跳过")
        return "skip"
    if dry_run:
        print(f"monthly dry-run: {len(texts)} 周报")
        return "dry"
    content = call_llm_retry(key, model, "monthly", "\n\n".join(texts))
    path = write_layer("monthly", f"{now:%Y-%m}.md", f"月汇总 {now:%Y-%m}", content)
    for f in collect_files("weekly", "*.md"):
        archive(f, AUTO / "archive" / "weekly" / f"{now:%Y}")
    print(f"monthly 完成 → {path}")
    return "ok"


def run_yearly(key, model, dry_run=False):
    now = datetime.now()
    texts, chars = [], 0
    for f in collect_files("monthly", "*.md"):
        t = f.read_text(encoding="utf-8")
        if chars + len(t) > MAX_CHARS:
            break
        texts.append(f"## {f.stem}\n{t}")
        chars += len(t)
    log(f"[yearly] 收集 {len(texts)} 月报")
    if not texts:
        print("yearly: 无月报，跳过")
        return "skip"
    if dry_run:
        print(f"yearly dry-run: {len(texts)} 月报")
        return "dry"
    content = call_llm_retry(key, model, "yearly", "\n\n".join(texts))
    path = write_layer("yearly", f"{now:%Y}.md", f"年汇总 {now:%Y}", content)
    for f in collect_files("monthly", "*.md"):
        archive(f, AUTO / "archive" / "monthly" / f"{now:%Y}")
    print(f"yearly 完成 → {path}")
    return "ok"


LAYER_FUNCS = {
    "hourly": run_hourly,
    "daily": run_daily,
    "weekly": run_weekly,
    "monthly": run_monthly,
    "yearly": run_yearly,
}


def check_semantic_doc(dry_run=False):
    """语义记忆文档守门（每日巩固后执行）。

    project-knowledge.md 是人工维护的语义层，自动巩固不写它；
    但多会话批次追加会让正文膨胀/混入批次结论，这里做检查和自动归档：
    - 行数 > SEMANTIC_WARN_LINES 或出现批次章节 → 告警，提示整理；
    - 行数 > SEMANTIC_AUTO_COMPACT_LINES 或批次章节 >= 3 → 调用 compact-knowledge.py 归档批次章节。
    """
    try:
        text = KNOWLEDGE.read_text(encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        log(f"[semantic] WARN: 无法读取 {KNOWLEDGE}: {e}")
        return
    lines = len(text.splitlines())
    sections = [ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")]
    # 分类规则与 compact-knowledge.py 共用（knowledge_rules.py），避免两处判定漂移
    sys.path.insert(0, str(Path(__file__).parent))
    from knowledge_rules import classify_section  # noqa: E402

    batch_sections = [s for s in sections if classify_section(s) == "batch"]
    log(f"[semantic] {KNOWLEDGE.name}: {lines} 行 / {len(text):,} 字符，批次章节 {len(batch_sections)} 个")
    need_warn = lines > SEMANTIC_WARN_LINES or len(batch_sections) >= 1
    need_compact = lines > SEMANTIC_AUTO_COMPACT_LINES or len(batch_sections) >= 3
    if need_warn and not need_compact:
        log(
            f"[semantic] WARN: 正文膨胀（{lines} 行，超过 {SEMANTIC_WARN_LINES} 行告警线，"
            f"或含 {len(batch_sections)} 个批次章节）——请把批次结论写到 docs/memory/batches/，"
            "或运行 scripts/memory/compact-knowledge.py 整理"
        )
    if not need_compact:
        return
    if dry_run:
        log("[semantic] dry-run: 跳过自动整理")
        return
    script = Path(__file__).with_name("compact-knowledge.py")
    if not script.exists():
        log(f"[semantic] WARN: 整理脚本不存在: {script}")
        return
    try:
        import subprocess

        r = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=180,
        )
        tail = (r.stdout or "").strip().splitlines()[-6:]
        for ln in tail:
            log(f"[semantic] {ln}")
        if r.returncode != 0:
            log(f"[semantic] WARN: compact 退出码 {r.returncode}: {(r.stderr or '').strip()[:300]}")
    except Exception as e:  # noqa: BLE001
        log(f"[semantic] WARN: 自动整理失败: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layer", choices=list(LAYER_FUNCS), default="hourly")
    ap.add_argument("--auto", action="store_true", help="按当前时间自动执行对应层（timer 用）")
    ap.add_argument("--model", default=os.environ.get("CONSOLIDATE_MODEL", "deepseek-flash"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD（仅 daily 层用，补历史）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 并发保护：timer 与手动运行互斥（flock 非阻塞），避免重复调 API / 互相覆盖产物
    if not args.dry_run:
        import fcntl

        lock_path = os.environ.get("CONSOLIDATE_LOCK", "/tmp/claude-mem-consolidate.lock")
        lock_fd = open(lock_path, "w", encoding="utf-8")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"另一巩固进程正在运行（{lock_path}），本次跳过")
            log("[lock] 另一巩固进程正在运行，跳过")
            lock_fd.close()
            return

    key = read_key()
    if not key:
        print(f"ERROR: 未找到 key，请写入 {ENV_FILE}")
        log("ERROR: 未找到 key")
        sys.exit(2)

    plan = []
    if args.auto:
        now = datetime.now()
        plan.append("hourly")
        if now.hour == 23:
            plan.append("daily")
            if now.weekday() == 6:  # 周日晚做周报
                plan.append("weekly")
            if now.day == 1:       # 每月 1 日晚做月报（上月数据已归档到 daily/weekly）
                plan.append("monthly")
            if now.month == 1 and now.day == 1:  # 元旦做年报
                plan.append("yearly")
    else:
        plan.append(args.layer)

    for layer in plan:
        fn = LAYER_FUNCS[layer]
        kwargs = {"dry_run": args.dry_run}
        if layer == "hourly":
            kwargs["force"] = args.force
        if layer == "daily" and args.date:
            kwargs["date_str"] = args.date
        try:
            fn(key, args.model, **kwargs)
        except Exception as e:  # noqa: BLE001
            log(f"[{layer}] 失败: {e}")
            print(f"ERROR [{layer}]: {e}")

    # 语义文档守门：每次巩固后检查（每小时轻量检查；超阈值才自动整理）
    if plan:
        check_semantic_doc(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
