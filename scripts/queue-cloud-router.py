#!/usr/bin/env python3
"""queue-cloud-router — 观察队列积压时自动切换到 DeepSeek 云端处理（带每日预算上限），回落自动切回本地 Ollama。

判据（2026-10-22 修订：按真实积压条数，而不是入队/生成比率）：
- 真实积压 = worker `GET /api/processing-status` 的 queueDepth（pending 消息条数）；
- 积压 ≥ --enter-depth（默认 1000 条）→ 切 DeepSeek 云端；
- 积压 ≤ --exit-depth（默认 300 条，真正清干净才停）→ 切回本地；
- 每日云端费用 ≥ --budget（默认 3 元）→ 强制切回本地并停止当天路由；
- API 不可用时回退到旧比率判据（入队/生成 > --enter-ratio）。

背景与实测（2026-10-22）：
- 本地 qwen3-8b-q4 多会话并行时处理不过来，真实积压常态 800–900 条并在上涨；
- `POST /api/settings` 热切换 provider 立即生效（无需重启 worker）；50 秒窗口 DeepSeek 处理 18 批
  （本地同期 1–2 批），输入 19.7 万/输出 0.93 万 tokens ≈ ¥0.47（高峰价保守口径）；
- POST **不写 API key**（敏感键静默忽略），DeepSeek key 来自 `~/.claude-mem/.env` 的 OPENAI_COMPAT_API_KEY。

价格口径（DeepSeek 官方 2026-10 价，保守=按缓存未命中算）：
  高峰（工作日 9-12/14-18）：输入 ¥2/M、输出 ¥8/M；空闲：输入 ¥1/M、输出 ¥4/M。
  日志不含缓存命中信息，按未命中计（高估成本，更安全）。

用法：
  python3 scripts/memory/queue-cloud-router.py --check     # 只读：积压、预算、当前模式
  python3 scripts/memory/queue-cloud-router.py --tick      # 单次巡检（systemd timer 用）
  python3 scripts/memory/queue-cloud-router.py --on        # 手动切云端（仍受预算限制）
  python3 scripts/memory/queue-cloud-router.py --off       # 手动切回本地
  --budget 3.0        # 每日云端预算上限（元）
  --enter-depth 1000  # 积压 ≥ 此条数才切云端
  --exit-depth 300    # 积压 ≤ 此条数才切回本地
  --window 5          # 日志统计窗口（分钟，辅助信息）
  --dry-run           # 只打印决策，不实际切换
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.expanduser("~/.claude-mem")
SETTINGS = os.path.join(DATA_DIR, "settings.json")
CONSOLIDATE_ENV = os.path.join(DATA_DIR, "consolidate.env")
LOG_DIR = os.path.join(DATA_DIR, "logs")
STATE_FILE = os.path.join(DATA_DIR, "cloud-router-state.json")
MONITOR_OUT = os.environ.get("CLOUD_ROUTER_MONITOR", os.path.join(ROOT, "docs/memory/.monitor/cloud-router.md"))
API_SETTINGS = "http://127.0.0.1:37700/api/settings"
API_PROCESSING = "http://127.0.0.1:37700/api/processing-status"

CLOUD = {
    "CLAUDE_MEM_OPENAI_COMPAT_PRESET": "deepseek",
    "CLAUDE_MEM_OPENAI_COMPAT_MODEL": "deepseek-flash",
}
LOCAL = {
    "CLAUDE_MEM_OPENAI_COMPAT_PRESET": "ollama",
    "CLAUDE_MEM_OPENAI_COMPAT_MODEL": "qwen3-8b-q4",
}
# 打包参数：批大小代码上限 32（Sh(v,1,32)）；这些键不在 POST /api/settings 白名单，
# 但 worker 每次批处理时都会 loadFromFile 重读 → 直接改 settings.json 即热生效。
CLOUD_EXTRA = {
    "CLAUDE_MEM_CLAUDE_OBSERVATION_BATCH_SIZE": 32,      # 一次打包 32 条（本地 12）
    "CLAUDE_MEM_OBSERVER_MAX_OUTPUT_TOKENS": 16384,      # 32 条观察输出留足
    # 注意：上下文窗口/对话字符上限不动（保持 Ollama 兼容的 40000/150000）。
    # 32 条观察内容约 8–20k tokens，40k 窗口足够；改大反而会让切换瞬间的本地批次超限卡死（2026-10-22 实测踩坑）。
}
LOCAL_EXTRA = {
    "CLAUDE_MEM_CLAUDE_OBSERVATION_BATCH_SIZE": 12,
    "CLAUDE_MEM_OBSERVER_CONTEXT_WINDOW": 40000,         # Ollama 40960 上下文
    "CLAUDE_MEM_OBSERVER_MAX_OUTPUT_TOKENS": 4096,
    "CLAUDE_MEM_OBSERVER_MAX_CONVERSATION_CHARS": 150000,
}


def today_log():
    return os.path.join(LOG_DIR, f"claude-mem-{datetime.now():%Y-%m-%d}.log")


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"mode": "local", "date": "", "cost_today": 0.0, "tokens_in": 0,
                "tokens_out": 0, "calls": 0, "original": None,
                "last_switch_at": None, "switched_count_today": 0,
                "last_depth": None}


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    json.dump(s, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)
    os.chmod(STATE_FILE, 0o600)


def reset_day(s):
    today = datetime.now().strftime("%Y-%m-%d")
    if s.get("date") != today:
        s.update({"date": today, "cost_today": 0.0, "tokens_in": 0,
                  "tokens_out": 0, "calls": 0, "switched_count_today": 0})
    return s


def is_peak():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 <= hm < 12 * 60) or (14 * 60 <= hm < 18 * 60)


def price_per_m():
    return (2.0, 8.0) if is_peak() else (1.0, 4.0)


def scan_queue_depth():
    """真实待处理积压条数（worker queueDepth）。"""
    try:
        with urllib.request.urlopen(API_PROCESSING, timeout=5) as r:
            d = json.load(r)
        v = d.get("queueDepth")
        return int(v) if isinstance(v, (int, float)) else None
    except Exception:
        return None


def scan_log_window(minutes):
    path = today_log()
    cutoff = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    e = s = 0
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 4 * 1024 * 1024)
            f.seek(-chunk, os.SEEK_END)
            data = f.read().decode("utf-8", "replace")
        for line in data.splitlines():
            if not line.startswith("[") or line[1:20] < cutoff:
                continue
            if "ENQUEUED |" in line:
                e += 1
            elif "STORED |" in line:
                s += 1
    except FileNotFoundError:
        pass
    return e, s


def scan_cloud_cost(s):
    path = today_log()
    inp = out = n = 0
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if "DeepSeek API usage" not in line:
                    continue
                m = re.search(r"inputTokens=(\d+), outputTokens=(\d+)", line)
                if m:
                    inp += int(m.group(1))
                    out += int(m.group(2))
                    n += 1
    except FileNotFoundError:
        return s
    pin, pout = price_per_m()
    s["tokens_in"] = inp
    s["tokens_out"] = out
    s["calls"] = n
    s["cost_today"] = round(inp / 1e6 * pin + out / 1e6 * pout, 3)
    return s


def read_key():
    try:
        for line in open(CONSOLIDATE_ENV, errors="replace"):
            m = re.match(r"\s*DEEPSEEK_API_KEY\s*=\s*(.+)\s*$", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return None


def post_settings(obj):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(API_SETTINGS, data=data,
                                 headers={"content-type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode()[:200]


def current_config():
    d = json.load(open(SETTINGS))
    cfg = {
        "CLAUDE_MEM_OPENAI_COMPAT_PRESET": d.get("CLAUDE_MEM_OPENAI_COMPAT_PRESET", "ollama"),
        "CLAUDE_MEM_OPENAI_COMPAT_MODEL": d.get("CLAUDE_MEM_OPENAI_COMPAT_MODEL", "qwen3-8b-q4"),
        "CLAUDE_MEM_OPENAI_COMPAT_API_KEY": d.get("CLAUDE_MEM_OPENAI_COMPAT_API_KEY", ""),
    }
    for k in LOCAL_EXTRA:
        cfg[k] = d.get(k, LOCAL_EXTRA[k])
    return cfg


def write_settings_keys(kv):
    """直接写 settings.json 的非白名单键（批大小/上下文等），worker 下次批处理即读新值。"""
    d = json.load(open(SETTINGS))
    d.update(kv)
    tmp = SETTINGS + ".tmp"
    json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS)


def read_current_mode():
    try:
        if current_config()["CLAUDE_MEM_OPENAI_COMPAT_PRESET"] == "deepseek":
            return "cloud"
    except Exception:
        pass
    return "local"


def wait_provider_switch(marker, timeout_s=90, interval_s=5):
    """轮询 worker 日志，确认切换后已出现目标 provider 的调用（marker 如 'DeepSeek API usage'）。"""
    start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = today_log()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                chunk = min(size, 2 * 1024 * 1024)
                f.seek(-chunk, os.SEEK_END)
                data = f.read().decode("utf-8", "replace")
            for line in data.splitlines():
                if line.startswith("[") and line[1:20] >= start and marker in line:
                    return True
        except FileNotFoundError:
            pass
        time.sleep(interval_s)
    return False


def switch(s, target, dry):
    if target == "cloud":
        cfg = dict(CLOUD)
        if not read_key():
            return False, "未找到 DEEPSEEK_API_KEY（~/.claude-mem/consolidate.env）"
        if dry:
            return True, f"[dry-run] 将切换 cloud: deepseek/deepseek-flash（先切 provider，确认生效后再打包 32）"
        st, body = post_settings(cfg)
        if st != 200 or '"success":true' not in body.replace(" ", ""):
            return False, f"切换 provider 失败 HTTP {st}: {body}"
        # 关键顺序：先确认 DeepSeek 真正接上，再放宽打包参数（否则本地 Ollama 会被超长批次卡死）
        if not wait_provider_switch("DeepSeek API usage", timeout_s=90):
            orig = s.get("original") or {}
            post_settings({
                "CLAUDE_MEM_OPENAI_COMPAT_PRESET": orig.get("CLAUDE_MEM_OPENAI_COMPAT_PRESET", "ollama"),
                "CLAUDE_MEM_OPENAI_COMPAT_MODEL": orig.get("CLAUDE_MEM_OPENAI_COMPAT_MODEL", "qwen3-8b-q4"),
            })
            return False, "切换后 90s 内未见 DeepSeek 调用，已回滚本地"
        batch = CLOUD_EXTRA["CLAUDE_MEM_CLAUDE_OBSERVATION_BATCH_SIZE"]
        write_settings_keys(CLOUD_EXTRA)
        return True, f"已切换 cloud: deepseek/deepseek-flash（打包 {batch} 条，已确认生效）"

    # target == local：先恢复批参数，再切回本地 provider
    orig = s.get("original") or {}
    extra = {k: orig.get(k, LOCAL_EXTRA[k]) for k in LOCAL_EXTRA}
    if dry:
        return True, "[dry-run] 将切回本地 ollama（恢复批参数）"
    write_settings_keys(extra)
    cfg = {
        "CLAUDE_MEM_OPENAI_COMPAT_PRESET": orig.get("CLAUDE_MEM_OPENAI_COMPAT_PRESET", LOCAL["CLAUDE_MEM_OPENAI_COMPAT_PRESET"]),
        "CLAUDE_MEM_OPENAI_COMPAT_MODEL": orig.get("CLAUDE_MEM_OPENAI_COMPAT_MODEL", LOCAL["CLAUDE_MEM_OPENAI_COMPAT_MODEL"]),
    }
    st, body = post_settings(cfg)
    if st != 200 or '"success":true' not in body.replace(" ", ""):
        return False, f"切回本地失败 HTTP {st}: {body}"
    return True, "已切回 local: ollama/qwen3-8b-q4（批参数已恢复）"


def decide(s, args, depth, e, st, ratio):
    """返回 (decision, reason)。decision: hold/on/off"""
    mode = s.get("mode", "local")
    over_budget = s["cost_today"] >= args.budget
    use_depth = depth is not None

    if mode == "cloud":
        if over_budget:
            return "off", f"日预算已用尽（¥{s['cost_today']:.2f}/¥{args.budget:.2f}）"
        if use_depth and depth <= args.exit_depth:
            return "off", f"积压已清到低水位（队列 {depth} ≤ {args.exit_depth}）"
        if not use_depth and ratio is not None and ratio < args.exit_ratio:
            return "off", f"积压回落（比率 {ratio:.2f} < {args.exit_ratio}，API 不可用回退判据）"
        return "hold", f"云端清积压中（队列 {depth if use_depth else 'N/A'}，比率 {ratio:.2f}）"

    if over_budget:
        return "hold", f"积压 {depth if use_depth else 'N/A'}，但今日预算已用尽 ¥{s['cost_today']:.2f}/{args.budget:.2f}"
    if use_depth and depth >= args.enter_depth:
        return "on", f"积压达阈值（队列 {depth} ≥ {args.enter_depth}）"
    if not use_depth and e >= 10 and ratio > args.enter_ratio:
        return "on", f"[回退比率判据] 入队 {e}/生成 {st} = {ratio:.2f} > {args.enter_ratio}"
    return "hold", f"本地处理（队列 {depth if use_depth else 'N/A'}，比率 {ratio:.2f}）"


def tick(args):
    s = reset_day(load_state())
    s = scan_cloud_cost(s)
    now = datetime.now()
    depth = scan_queue_depth()
    if depth is not None:
        s["last_depth"] = depth
    e, st = scan_log_window(args.window)
    ratio = (e / st) if st > 0 else (float("inf") if e > 0 else 0.0)
    mode = s.get("mode", "local")

    decision, reason = decide(s, args, depth, e, st, ratio)

    if decision in ("on", "off"):
        if decision == "on" and not s.get("original"):
            s["original"] = current_config()
        ok, msg = switch(s, "cloud" if decision == "on" else "local", args.dry_run)
        if ok:
            if not args.dry_run:
                s["mode"] = "cloud" if decision == "on" else "local"
                s["last_switch_at"] = now.isoformat()
                if decision == "on":
                    s["switched_count_today"] = s.get("switched_count_today", 0) + 1
            print(f"[{now:%H:%M:%S}] SWITCH {decision}: {msg} | 原因: {reason}")
        else:
            print(f"[{now:%H:%M:%S}] SWITCH FAILED: {msg}")
    else:
        print(f"[{now:%H:%M:%S}] {reason} | 模式={mode} 今日云端 ¥{s['cost_today']:.2f}/{args.budget:.2f}")

    if args.dry_run:
        return 0
    save_state(s)
    write_monitor(s, args, e, st, ratio, reason, depth)
    return 0


def write_monitor(s, args, e, st, ratio, reason, depth=None):
    try:
        os.makedirs(os.path.dirname(MONITOR_OUT), exist_ok=True)
        mode = s.get("mode", "local")
        ratio_txt = "∞" if ratio == float("inf") else f"{ratio:.2f}"
        lines = [
            "# 积压云端路由状态",
            "",
            f"> 更新 {datetime.now():%Y-%m-%d %H:%M:%S}；模式：{'☁️ DeepSeek 云端' if mode == 'cloud' else '💻 本地 Ollama'}",
            "",
            "| 项 | 值 |",
            "|---|---|",
            f"| 当前模式 | {mode} |",
            f"| 真实积压队列 | {depth if depth is not None else 'N/A'} 条（worker queueDepth） |",
            f"| 触发阈值 | 积压 ≥ {args.enter_depth} 条切云端；≤ {args.exit_depth} 条切回本地 |",
            f"| 近 {args.window} 分钟 入队/生成 | {e} / {st}（比率 {ratio_txt}，辅助参考） |",
            f"| 今日云端费用 | ¥{s['cost_today']:.3f} / 上限 ¥{args.budget:.2f} |",
            f"| 今日云端调用 | {s['calls']} 次（输入 {s['tokens_in']:,} / 输出 {s['tokens_out']:,} tokens） |",
            f"| 今日切换次数 | {s.get('switched_count_today', 0)} |",
            f"| 当前时段价格口径 | {'高峰（输入¥2/M 输出¥8/M）' if is_peak() else '空闲（输入¥1/M 输出¥4/M）'} |",
            f"| 最近决策 | {reason} |",
        ]
        open(MONITOR_OUT, "w").write("\n".join(lines) + "\n")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--tick", action="store_true")
    ap.add_argument("--on", action="store_true")
    ap.add_argument("--off", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--budget", type=float, default=3.0)
    ap.add_argument("--enter-depth", type=int, default=1000)
    ap.add_argument("--exit-depth", type=int, default=300)
    ap.add_argument("--window", type=int, default=5)
    ap.add_argument("--enter-ratio", type=float, default=3.0)
    ap.add_argument("--exit-ratio", type=float, default=1.5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.check or args.status:
        s = reset_day(load_state())
        s = scan_cloud_cost(s)
        depth = scan_queue_depth()
        e, st = scan_log_window(args.window)
        ratio = (e / st) if st > 0 else (float("inf") if e > 0 else 0.0)
        print(f"路由状态: {s.get('mode','local')}（实际配置: {read_current_mode()}）")
        print(f"真实积压队列: {depth if depth is not None else 'N/A'} 条（阈值：进 {args.enter_depth} / 出 {args.exit_depth}）")
        print(f"近 {args.window} 分钟: 入队 {e} / 生成 {st} = 比率 {ratio if ratio != float('inf') else '∞'}（辅助）")
        print(f"今日云端: {s['calls']} 次调用, ¥{s['cost_today']:.3f} / 预算 ¥{args.budget:.2f}, 切换 {s.get('switched_count_today',0)} 次")
        print(f"当前时段: {'高峰' if is_peak() else '空闲'}；{'预算已用尽' if s['cost_today']>=args.budget else '预算未用尽'}")
        return 0
    if args.on or args.off:
        s = reset_day(load_state())
        s = scan_cloud_cost(s)
        if args.on and s.get("mode") != "cloud":
            if not s.get("original"):
                s["original"] = current_config()
            ok, msg = switch(s, "cloud", args.dry_run)
            if ok:
                s["mode"] = "cloud"
                s["last_switch_at"] = datetime.now().isoformat()
                save_state(s)
            print(msg)
        elif args.off and s.get("mode") == "cloud":
            ok, msg = switch(s, "local", args.dry_run)
            if ok:
                s["mode"] = "local"
                save_state(s)
            print(msg)
        else:
            print(f"无需操作（当前模式 {s.get('mode','local')}）")
        return 0
    return tick(args)


if __name__ == "__main__":
    sys.exit(main())
