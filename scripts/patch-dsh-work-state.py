#!/usr/bin/env python3
"""patch-dsh-work-state — 给 @claude-mem/dsh 插件补 work_state_write / work_state_read 工具（幂等）。

背景（2026-10-22 实测）：
- claude-mem worker（14.0.1 / 15.0.1 均如此）MCP 已提供 work_state_write / work_state_read，
  且每个会话启动注入的上下文明确要求 agent 用 work_state_write 跟踪待办；
- 但官方 @claude-mem/dsh 集成只注册 5 个 mem_* 工具（mem_search/mem_timeline/
  mem_get_observations/mem_save/mem_context），work_state 工具没有暴露 → 提示要求的工具不存在。
- 本脚本给插件 bundle 打最小补丁：
  1) Ne（worker client）类新增 workStateWrite / workStateRead 方法；
  2) 新增 Wf/Rf 两个工具注册函数（cwd 取 exec.agent.session.header.cwd，回退 process.cwd()）；
  3) Tr() 末尾追加 `r.workState!==!1 && (Wf(...), Rf(...))`。
- 生效条件：重启 `dsh-web`（systemd user 服务）后新会话加载插件；升级/重装 claude-mem 会
  覆盖插件目录 → 升级后重跑本脚本即可。

用法：
  python3 scripts/memory/patch-dsh-work-state.py            # 打补丁（已打过则跳过）
  python3 scripts/memory/patch-dsh-work-state.py --check    # 只检查状态
  python3 scripts/memory/patch-dsh-work-state.py --restore  # 从最近备份恢复
"""
import argparse
import glob
import os
import shutil
import sys
import time

PLUGIN_ROOT = os.path.expanduser("~/.claude/plugins/marketplaces/thedotmack/dsh")
TARGET = os.path.join(PLUGIN_ROOT, "lib", "index.js")

METHODS = (
    'async workStateWrite(t,r){'
    'let n=this.limits.baseUrl.replace(/\\/+$/,"")+"/api/work-state/entries",'
    'i=AbortSignal.timeout(this.limits.timeoutMs),'
    'o=r?AbortSignal.any([r,i]):i,'
    'a=await fetch(n,{method:"POST",headers:{"content-type":"application/json"},'
    'body:JSON.stringify({cwd:t.cwd,list:t.list,fields:t.fields}),signal:o});'
    'if(!a.ok){let s=await a.text().catch(()=>"");'
    'throw new R(`memory worker returned HTTP ${a.status}: ${s.slice(0,500)}`,"MEM_PROVIDER_ERROR")}'
    'return await a.text()}'
    'async workStateRead(t,r){'
    'let n=new URLSearchParams({cwd:t.cwd});'
    't.list!==void 0&&n.set("list",t.list),'
    't.includeClosed!==void 0&&n.set("includeClosed",String(t.includeClosed));'
    'return await this.requestText("/api/work-state",n,r)}'
)

TOOLS = (
    'function Wf(e,t,r){e.tools.register(Z({name:"work_state_write",'
    'description:"Your canonical to-do list and working state for this project, kept across sessions: whatever is still open is shown at the start of every session. Each call appends one entry to a list. To-do item: fields {\\"task\\": \\"<name>\\", \\"status\\": \\"todo\\" | \\"doing\\" | \\"done\\" | \\"dropped\\", ...details}. State on the list itself: any other fields (the latest value of each key wins; null clears a key; \\"status\\": \\"done\\" closes the list). Returns what is still open in the list. Params: list (required), fields (required).",'
    'parameters:{list:{type:"string",required:!0,description:"The to-do list or tracked thing this entry belongs to."},fields:{type:"json",required:!0,description:"Keys to set. Include \\"task\\" to update a to-do item. Values are strings, numbers, booleans, or null to clear a key."}},'
    'output:{schema:{type:"string"},render:(n,i)=>[{type:"text",text:i}]},timeoutMs:r,isConcurrencySafe:()=>!0,'
    'async execute(n,i){let o=i.agent?.session?.header?.cwd||process.cwd();return await t.workStateWrite({cwd:o,list:n.list,fields:n.fields},i.signal)}}))}'
    'function Rf(e,t,r){e.tools.register(Z({name:"work_state_read",'
    'description:"Read this project\'s to-do lists and working state written with work_state_write: every open item, or one list, with done and dropped items when includeClosed is true. Params: list, includeClosed.",'
    'parameters:{list:{type:"string",description:"Read only this list."},includeClosed:{type:"boolean",description:"Also show done and dropped items and closed lists."}},'
    'output:{schema:{type:"string"},render:(n,i)=>[{type:"text",text:i}]},timeoutMs:r,isConcurrencySafe:()=>!0,'
    'async execute(n,i){let o=i.agent?.session?.header?.cwd||process.cwd();return await t.workStateRead({cwd:o,...n.list!==void 0?{list:n.list}:{},...n.includeClosed!==void 0?{includeClosed:n.includeClosed}:{}},i.signal)}}))}'
)

OLD_TR = (
    'function Tr(e,t,r){r.search!==!1&&Fi(e,t,r.timeoutMs),r.timeline!==!1&&Li(e,t,r.timeoutMs),'
    'r.getObservations!==!1&&Ji(e,t,r.timeoutMs),r.save!==!1&&zi(e,t,r.timeoutMs),'
    'r.context!==!1&&Ui(e,t,r.timeoutMs)}'
)
NEW_TR = (
    'function Tr(e,t,r){r.search!==!1&&Fi(e,t,r.timeoutMs),r.timeline!==!1&&Li(e,t,r.timeoutMs),'
    'r.getObservations!==!1&&Ji(e,t,r.timeoutMs),r.save!==!1&&zi(e,t,r.timeoutMs),'
    'r.context!==!1&&Ui(e,t,r.timeoutMs),'
    'r.workState!==!1&&(Wf(e,t,r.timeoutMs),Rf(e,t,r.timeoutMs))}'
)


def find_backups():
    return sorted(glob.glob(TARGET + ".bak-workstate-*"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查，不改动")
    ap.add_argument("--restore", action="store_true", help="从最近备份恢复")
    args = ap.parse_args()

    if not os.path.isfile(TARGET):
        sys.exit(f"未找到插件文件: {TARGET}（claude-mem 是否安装？）")

    s = open(TARGET, encoding="utf-8").read()

    if args.check:
        print(f"插件: {TARGET}")
        print(f"大小: {len(s)} 字符")
        print("work_state_write 存在:", "work_state_write" in s)
        print("workStateWrite 方法存在:", "async workStateWrite" in s)
        print("Tr 已接入:", "r.workState!==!1" in s)
        print("备份数:", len(find_backups()))
        sys.exit(0 if "work_state_write" in s else 1)

    if args.restore:
        backups = find_backups()
        if not backups:
            sys.exit("没有可用的 .bak-workstate-* 备份")
        latest = backups[-1]
        shutil.copy2(latest, TARGET)
        print(f"已从 {latest} 恢复（需重启 dsh-web 生效）")
        return

    if "work_state_write" in s:
        print("已打过补丁，跳过（如需重打先 --restore）")
        return

    bak = TARGET + f".bak-workstate-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(TARGET, bak)

    anchor = "async requestJson(t,r,n){"
    if s.count(anchor) != 1:
        sys.exit(f"锚点异常: requestJson 出现 {s.count(anchor)} 次")
    s = s.replace(anchor, METHODS + anchor, 1)

    tr_anchor = "function Tr(e,t,r){"
    if s.count(tr_anchor) != 1:
        sys.exit(f"锚点异常: Tr 出现 {s.count(tr_anchor)} 次")
    s = s.replace(tr_anchor, TOOLS + tr_anchor, 1)

    if s.count(OLD_TR) != 1:
        sys.exit("锚点异常: Tr 函数体未精确匹配（版本可能已变化，请人工核对）")
    s = s.replace(OLD_TR, NEW_TR, 1)

    final = open(TARGET, "w", encoding="utf-8").write(s)
    print(f"补丁完成: {TARGET}（备份 {bak}，{final} 字符）")
    print("生效条件: 重启 dsh-web（systemctl --user restart dsh-web.service），刷新页面后新会话生效")


if __name__ == "__main__":
    main()
