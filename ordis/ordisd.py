#!/usr/bin/env python3
"""
guardiand — 服务器自愈守护进程
启动: python guardiand.py
"""

import argparse
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from logger import get_logger
from config import load_rules

log = get_logger("guardiand")


def cmd_run(args):
    """启动守护循环。"""
    from engine import run_loop
    config = load_rules()
    interval = config.get("global", {}).get("check_interval", 30)
    log.info("=" * 50)
    log.info("  Guardian Daemon 启动")
    log.info("  检查间隔: %ds", interval)
    log.info("  规则数量: %d", len(config.get("rules", [])))
    log.info("=" * 50)
    run_loop(interval)


def cmd_once(args):
    """执行一轮检查（调试用）。"""
    from engine import run_once
    triggered = run_once()
    if triggered:
        log.info("触发了 %d 条规则:", len(triggered))
        for t in triggered:
            print(f"  - {t['rule']}: {t['value']}")
    else:
        log.info("无规则触发，一切正常。")


def cmd_events(args):
    """查看最近的触发事件。"""
    from engine import load_events
    events = load_events()
    n = args.n
    if not events:
        print("(暂无事件)")
    for e in events[:n]:
        print(f"[{e['time']}] {e['rule']}")
        print(f"  数据: {e['value']}")
        if e["heal"]:
            print(f"  修复: {e['heal']}")
        print()


def cmd_status(args):
    """查看当前系统状态（快速探针）。"""
    from collectors.cpu import collector as cpu
    from collectors.memory import collector as mem
    from collectors.disk import collector as disk
    from collectors.process import collector as proc

    print("=== CPU ===")
    print(cpu.collect())
    print("\n=== Memory ===")
    print(mem.collect())
    print("\n=== Disk ===")
    print(disk.collect())
    print("\n=== Process ===")
    print(proc.collect())


def main():
    parser = argparse.ArgumentParser(description="Guradian - Server Self-Healing Daemon")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="启动守护进程")
    p_run.set_defaults(func=cmd_run)

    p_once = sub.add_parser("once", help="运行一轮检查（调试）")
    p_once.set_defaults(func=cmd_once)

    p_events = sub.add_parser("events", help="查看触发事件")
    p_events.add_argument("-n", type=int, default=20, help="显示最近 N 条")
    p_events.set_defaults(func=cmd_events)

    p_status = sub.add_parser("status", help="查看系统状态")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    if args.cmd is None:
        parser.print_help()
    else:
        args.func(args)


if __name__ == "__main__":
    main()
