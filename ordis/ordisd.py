#!/usr/bin/env python3
"""
guardiand — 服务器自愈守护进程
启动: python guardiand.py
"""

from __future__ import annotations
import argparse
import json
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


def cmd_run_install(args):
    """安装并启动 systemd 常驻服务（生产部署用）。"""
    import shutil
    import subprocess

    src = Path(__file__).parent.parent / "deploy" / "ordis-guardian.service"
    dst = Path("/etc/systemd/system/ordis-guardian.service")
    if not src.exists():
        # 容器/裸装场景：直接生成
        dst.write_text(
            "[Unit]\nDescription=Ordis Guardian\nAfter=network.target\n\n"
            "[Service]\nType=simple\nWorkingDirectory=/opt/ordis\n"
            "ExecStart=/usr/bin/python3 /opt/ordis/ordisc run\n"
            "Restart=always\nRestartSec=10\n"
            "StandardOutput=append:/var/log/ordis-guardian.log\n"
            "StandardError=append:/var/log/ordis-guardian.log\n\n"
            "[Install]\nWantedBy=multi-user.target\n",
            encoding="utf-8")
    else:
        shutil.copy(src, dst)
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "enable", "--now", "ordis-guardian"],
                   check=False)
    r = subprocess.run(["systemctl", "is-active", "ordis-guardian"],
                       capture_output=True, text=True)
    print(f"ordis-guardian 服务: {r.stdout.strip()}")
    print("日志: tail -f /var/log/ordis-guardian.log")


def cmd_once(args):
    """执行一轮检查（调试用）。"""
    from engine import run_once
    triggered = run_once(verbose=False)
    print("=== Ordis 单次巡检 ===")
    if not triggered:
        print("巡检完成：未发现故障")
        return

    rendered = [_render_once_event(event) for event in triggered]
    skipped = sum(item[0] == "跳过" for item in rendered)
    faults = len(rendered) - skipped
    parts = []
    if faults:
        parts.append(f"{faults} 个故障")
    if skipped:
        parts.append(f"{skipped} 项跳过")
    print("巡检完成：" + "，".join(parts))
    for event, (status, message) in zip(triggered, rendered):
        print(f"[{status}] {message}")
        if getattr(args, "verbose", False):
            print("       " + json.dumps(event.get("value", {}),
                                          ensure_ascii=False, default=str))
    if getattr(args, "wait_ai", False):
        import ai_diagnose
        print("等待 AI 接管流程完成...")
        if not ai_diagnose.wait_for_pending():
            print("AI 接管等待超时，后台结果可能尚未完成")
            return 2
        print("AI 接管流程已完成")
    return 0


def _render_once_event(event):
    """Turn an engine event into one concise Chinese line."""
    if event.get("finding"):
        finding = event.get("value") or {}
        severity = {
            "critical": "严重", "high": "高", "medium": "中",
            "low": "低", "info": "信息",
        }.get(finding.get("severity"), "告警")
        return severity, finding.get("summary") or event.get("rule", "检测到故障")

    heal = event.get("heal") or {}
    actions = heal.get("actions") or []
    if heal.get("skipped") or (actions and all(a.get("skipped") for a in actions)):
        ports = ",".join(str(action.get("port")) for action in actions
                         if action.get("port") is not None)
        return "跳过", f"端口 {ports}：对应服务未部署在本机"
    if heal.get("success"):
        return "已修复", str(event.get("rule", "故障"))
    if heal:
        return "修复失败", str(event.get("rule", "故障"))
    return "告警", str(event.get("rule", "检测到故障"))


def _print_check_report(result, output_json=False):
    """Render a detector report without exposing credentials or raw responses."""
    if output_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    source = "Kubernetes" if result["source"] == "kubernetes" else "传统业务"
    cluster = f" ({result.get('cluster')})" if result.get("cluster") else ""
    print(f"=== {source}检测{cluster} ===")
    for check in result.get("checks", []):
        status = {"ok": "正常", "skipped": "跳过", "error": "错误"}.get(
            check.get("status"), str(check.get("status", "?")))
        detail = check.get("detail")
        suffix = f": {detail}" if detail else ""
        print(f"[{status}] {check.get('name')}{suffix}")
    findings = result.get("findings", [])
    if not findings:
        print("未发现故障")
        return
    print(f"发现 {len(findings)} 个故障:")
    for finding in findings:
        target = "/".join(filter(None, (
            finding.get("namespace"), finding.get("kind"), finding.get("name"))))
        suffix = f" [{target}]" if target else ""
        severity = {
            "critical": "严重", "high": "高", "medium": "中",
            "low": "低", "info": "信息",
        }.get(finding.get("severity"), "告警")
        print(f"[{severity}]{suffix} {finding['summary']}")


def _check_exit_code(result):
    if any(check.get("status") == "error" for check in result.get("checks", [])):
        return 2
    if result.get("findings"):
        return 1
    return 0


def cmd_check(args):
    """Run the read-only Linux and business endpoint checks."""
    from traditional_checks import run_checks
    config = (load_rules().get("detection") or {}).get("traditional") or {}
    result = run_checks(config)
    _print_check_report(result, args.output_json)
    return _check_exit_code(result)


def cmd_k8s(args):
    """Inspect Kubernetes health through a read-only API transport."""
    from k8s_client import KubernetesClient

    config = dict((load_rules().get("detection") or {}).get("kubernetes") or {})
    if args.context is not None:
        config["context"] = args.context
    if args.kubeconfig is not None:
        config["kubeconfig"] = args.kubeconfig
    client = KubernetesClient(
        context=str(config.get("context") or ""),
        kubeconfig=str(config.get("kubeconfig") or ""),
        timeout=float(config.get("timeout", 15)))

    if args.action == "doctor":
        result = client.doctor()
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif result["ok"]:
            print(f"Kubernetes 连接正常: {result['cluster']} ({result.get('nodes', 0)} nodes, {result['mode']})")
        else:
            print(f"Kubernetes 不可用: {result['error']}")
        return 0 if result["ok"] else 2

    from k8s_checks import run_checks
    result = run_checks(config, client=client)
    _print_check_report(result, args.output_json)
    return _check_exit_code(result)


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


def cmd_discover(args):
    """服务自动发现：列表 / 纳管 / 移除纳管。"""
    from collectors.discovery import collector as disco
    from config import load_rules, save_rules

    services = disco.collect().get("services", [])

    if args.remove:
        rules = load_rules()
        adopted = (rules.get("process", {}) or {}).get("adopted", []) or []
        new = [a for a in adopted
               if f"{a['manager']}:{a['target']}" != args.remove]
        if len(new) == len(adopted):
            print(f"未找到纳管记录: {args.remove}")
            raise SystemExit(1)
        rules.setdefault("process", {})["adopted"] = new
        save_rules(rules)
        print(f"已移除纳管: {args.remove}")
        return

    if args.adopt:
        target_key = args.adopt
        match = next((s for s in services
                      if (f"{s['manager']}:{s['target']}" == target_key
                          or s["target"] == target_key)), None)
        if not match:
            print(f"发现列表中无此服务: {args.adopt}")
            print("可用:")
            for s in services:
                print(f"  {s['manager']}:{s['target']}  ports={s['ports']}")
            raise SystemExit(1)
        key = f"{match['manager']}:{match['target']}"
        if match["manager"] == "manual":
            print(f"拒绝: {key} 不属于任何进程管理器，无法自动修复")
            raise SystemExit(1)
        rules = load_rules()
        adopted = (rules.get("process", {}) or {}).get("adopted", []) or []
        if not any(f"{a['manager']}:{a['target']}" == key for a in adopted):
            adopted.append({"manager": match["manager"],
                            "target": match["target"],
                            "ports": match["ports"]})
            rules.setdefault("process", {})["adopted"] = adopted
            save_rules(rules)
        print(f"已纳管: {key}  ports={match['ports']}  auto_heal=ON")
        return

    # 默认：列表视图
    if not services:
        print("(未发现任何监听服务)")
        return
    print(f"{'管理器':<10} {'目标':<28} {'端口':<16} 自愈")
    for s in services:
        heal = "ON" if s["auto_heal"] else ("-" if s["manager"] == "manual"
                                            else "off(未纳管)")
        ports_str = ",".join(map(str, s["ports"]))
        manager, target = s["manager"], s["target"]
        print(f"{manager:<10} {target:<28} {ports_str:<16} {heal}")


def cmd_cases(args):
    """查看 AI 诊断案例。"""
    import ai_diagnose
    cases = ai_diagnose.load_cases()
    if not cases:
        print("(暂无 AI 诊断案例)")
        return
    shown = cases[:args.n]
    print(f"共 {len(cases)} 条，显示最近 {len(shown)} 条\n")
    for c in shown:
        d = c.get("diagnosis")
        head = f"[{c.get('time', '?')}] {c.get('rule', '?')} ({c.get('collector', '?')})"
        if d:
            print(head)
            print(f"  指纹  : {c.get('_key', '?')}")
            print(f"  置信度: {d.get('confidence', '?')}")
            print(f"  根因  : {d.get('root_cause', '')}")
            fd = d.get("fix_direction", "")
            if args.full:
                print(f"  方向  : {fd}")
            else:
                cut = fd[:100] + ("…" if len(fd) > 100 else "")
                print(f"  方向  : {cut}")
        else:
            print(head + "  [诊断失败]")
            print(f"  错误  : {str(c.get('error'))[:160]}")
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


def cmd_model(args):
    """AI 供应商管理：添加/切换/测试。"""
    import model_config as mc

    # 非交互路径：model add --name X --base URL --model M --key-env VAR
    if getattr(args, "name", None):
        import os
        key = os.environ.get(args.key_env or "", "")
        if not (args.base and args.model and key):
            print("非交互模式需提供 --base --model --key-env(环境变量名，值从环境读取)")
            raise SystemExit(1)
        print(f"测试 {args.name} / {args.model} @ {args.base} ...")
        try:
            text, elapsed = mc.test_provider(
                {"base_url": args.base, "model": args.model, "api_key": key})
            print(f"[OK] {elapsed}s 响应: {text.strip()[:80]}")
        except Exception as e:
            print(f"[FAIL] 测试失败，未保存: {e}")
            raise SystemExit(1)
        mc.add_provider(args.name, args.base, args.model, key)
        cfg = mc.load_active()
        print(f"已保存并激活: {cfg['name']} / {cfg['model']} "
              f"(key {mc.mask_key(key)})")
        return

    if args.action == "status":
        data = mc.load()
        if not data["providers"]:
            print("(未配置任何供应商，运行 `ordis config model` 添加)")
            return
        for n, p in data["providers"].items():
            mark = " ← 激活" if n == data.get("active") else ""
            print(f"{n}{mark}")
            print(f"  base : {p['base_url']}")
            print(f"  model: {p['model']}")
            print(f"  key  : {mc.mask_key(p['api_key'])}  (加入于 {p['added_at']})")
        return

    if args.action == "test":
        cfg = mc.load_active()
        if not cfg:
            print("无激活供应商。运行 `ordis config model` 配置。")
            raise SystemExit(1)
        print(f"测试 {cfg['name']} / {cfg['model']} @ {cfg['base_url']} ...")
        try:
            text, elapsed = mc.test_provider(cfg)
            print(f"[OK] {elapsed}s 响应: {text.strip()[:100]}")
        except Exception as e:
            print(f"[FAIL] {e}")
            raise SystemExit(1)
        return

    # 默认 / add：交互式向导（添加或切换）
    mc.interactive_wizard()


def cmd_setup(args):
    """Run the consolidated first-run configuration wizard."""
    import setup_wizard

    try:
        setup_wizard.run_interactive()
    except KeyboardInterrupt:
        print("\n配置已取消，现有配置未变更")
        return 130
    except (RuntimeError, ValueError) as exc:
        print(f"配置失败：{setup_wizard._safe_error(exc)}")
        return 1
    return 0


def _print_configuration(summary):
    model = summary["model"]
    ai = summary["ai"]
    email = summary["email"]
    print("=== Ordis 配置 ===")
    if model["configured"]:
        print(f"模型：{model['provider']} / {model['model']}")
        print(f"API：{model['base_url']}（Key {model['api_key']}）")
    else:
        print("模型：未配置（运行 `ordis setup`）")
    print(f"AI 接管：{ai['mode']}")
    print(f"全局权限：{ai['permission']}")
    limit = str(ai["daily_limit"]) if ai["daily_limit"] else "不限"
    print(f"AI 每日限额：{limit}")
    if ai["mode"] == "email" or email["to"] or email["smtp_host"]:
        print(f"管理员邮箱：{email['to'] or '未配置'}")
        print("邮件状态：" + ("就绪" if email["ready"]
                              else "；".join(email["issues"])))


def _test_model_configuration():
    import model_config
    import setup_wizard

    provider = model_config.load_active()
    if not provider:
        print("[失败] 模型：未配置，请运行 `ordis config model`")
        return False
    print(f"[测试] 模型：{provider['name']} / {provider['model']}")
    try:
        response, elapsed = model_config.test_provider(provider)
    except Exception as exc:
        print(f"[失败] 模型：{setup_wizard._safe_error(exc)}")
        return False
    print(f"[正常] 模型连接正常（{elapsed}s）：{response.strip()[:60]}")
    return True


def _test_email_configuration():
    import ai_mode

    issues = ai_mode.email_configuration_issues(require_password=True)
    if issues:
        print("[失败] 邮件：" + "；".join(issues))
        return False
    print("[正常] 邮件配置完整（未发送测试邮件）")
    return True


def cmd_config(args):
    """Consolidated model, takeover mode, and permission configuration."""
    import setup_wizard

    section = args.section or "show"
    if section == "show":
        summary = setup_wizard.configuration_summary()
        if args.output_json:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        else:
            _print_configuration(summary)
        return 0

    if section == "model":
        model_action = args.model_action
        if model_action == "test":
            return 0 if _test_model_configuration() else 1
        if model_action == "status":
            summary = setup_wizard.configuration_summary()
            if args.output_json:
                print(json.dumps(summary["model"], ensure_ascii=False,
                                 sort_keys=True))
            else:
                _print_configuration(summary)
            return 0
        try:
            setup_wizard.run_model_interactive()
        except KeyboardInterrupt:
            print("\n配置已取消，现有模型配置未变更")
            return 130
        except (RuntimeError, ValueError) as exc:
            print(f"配置失败：{setup_wizard._safe_error(exc)}")
            return 1
        return 0

    if section == "mode":
        legacy_args = argparse.Namespace(
            action=args.mode or "status", to=args.to,
            smtp_host=args.smtp_host, smtp_port=args.smtp_port,
            smtp_user=args.smtp_user, from_address=args.from_address,
            password_env=args.password_env,
            smtp_security=args.smtp_security)
        return cmd_ai_mode(legacy_args)

    if section == "permission":
        return cmd_level(argparse.Namespace(set=args.permission))

    checks = []
    if args.target in {"all", "model"}:
        checks.append(_test_model_configuration())
    should_test_email = args.target == "email"
    if args.target == "all":
        import ai_mode
        should_test_email = ai_mode.load()["mode"] == "email"
    if should_test_email:
        checks.append(_test_email_configuration())
    return 0 if all(checks) else 1


def cmd_talk(args):
    """自然语言管理：ordis talk ["问题"]。"""
    import talk
    question = " ".join(args.question).strip() if args.question else None
    talk.main(question=question)


def cmd_ai_mode(args):
    """查看或切换 AI 故障接管模式。"""
    import os
    import ai_mode

    if args.action == "status":
        config = ai_mode.load()
        email = config["email"]
        print(f"当前 AI 接管模式: {config['mode']}")
        print(f"管理员邮箱: {email.get('to') or '(未配置)'}")
        endpoint = (f"{email.get('smtp_host')}:{email.get('smtp_port')}"
                    if email.get("smtp_host") else "(未配置)")
        print(f"SMTP: {endpoint} ({email.get('security')})")
        print(f"密码来源: 环境变量 {email.get('password_env')}")
        issues = ai_mode.email_configuration_issues(
            config, require_password=True)
        print("邮件状态: " + ("就绪" if not issues else "；".join(issues)))
        return

    if args.action == "auto":
        import ai_levels
        ai_mode.set_mode("auto")
        print("AI 接管模式已切换为 auto：AI 修复命令通过校验后将自动执行并回检")
        level = ai_levels.current_level()
        print(f"当前全局权限: {level}")
        if level == "view":
            print("注意: view 会拦截写修复；需要自动修复服务请运行 "
                  "`ordis config permission operate`")
        return

    if args.action == "email":
        updates = {
            "to": args.to,
            "smtp_host": args.smtp_host,
            "smtp_port": args.smtp_port,
            "smtp_user": args.smtp_user,
            "from_address": args.from_address,
            "password_env": args.password_env,
            "security": args.smtp_security,
        }
        if any(value is not None for value in updates.values()):
            try:
                ai_mode.configure_email(**updates)
            except ValueError as exc:
                print(f"邮件配置无效: {exc}")
                raise SystemExit(1)
        config = ai_mode.load()
        issues = ai_mode.email_configuration_issues(
            config, require_password=False)
        if issues:
            print("无法切换 email 模式: " + "；".join(issues))
            raise SystemExit(1)
        ai_mode.set_mode("email")
        password_env = config["email"]["password_env"]
        print("AI 接管模式已切换为 email：只发送修复建议，不执行 AI 命令")
        if config["email"].get("smtp_user") and not os.environ.get(password_env):
            print(f"注意: 守护进程环境中还需设置 {password_env}")
        return

    if args.action == "test-email":
        from notifier import email_send
        config = ai_mode.load()
        issues = ai_mode.email_configuration_issues(
            config, require_password=True)
        if issues:
            print("邮件未就绪: " + "；".join(issues))
            raise SystemExit(1)
        ok = email_send(config["email"], "[Ordis] 邮件配置测试",
                        "Ordis SMTP 邮件通知配置正常。")
        print("测试邮件发送成功" if ok else "测试邮件发送失败")
        if not ok:
            raise SystemExit(1)


def cmd_server(args):
    """集群 server：聚合各节点上报。"""
    import uvicorn
    from cluster import (make_app, resolve_token, enroll_node,
                         DEFAULT_PORT, load_config)
    cfg = load_config()
    port = args.port or cfg.get("listen_port") or DEFAULT_PORT

    if args.enroll_node:
        node_token = enroll_node(args.enroll_node)
        print(f"已生成节点 {args.enroll_node} 的独立凭证：")
        print(f"  token: {node_token}")
        print(f"节点加入: ordis join https://<本机IP>:{port} --token <上面的token>")
        return

    token, generated = resolve_token(args.token)
    if generated:
        print("已自动生成集群 token（写入 ~/.ordis/cluster.json）：")
        print(f"  token: {token}")
        print(f"节点加入: ordis join http://<本机IP>:{port} --token <上面的token>")
    else:
        print(f"Ordis Cluster Server 监听 :{port}（token 认证已启用）")
    cert = args.tls_cert or cfg.get("tls_cert")
    key = args.tls_key or cfg.get("tls_key")
    if bool(cert) != bool(key):
        print("失败: TLS 必须同时配置 --tls-cert 和 --tls-key")
        raise SystemExit(1)
    if not cert and args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("⚠️  当前为明文 HTTP；生产环境必须配置 --tls-cert/--tls-key")
    uvicorn.run(make_app(token), host=args.host, port=port, log_level="warning",
                ssl_certfile=cert, ssl_keyfile=key)


def cmd_agent(args):
    """集群 agent：常驻上报本机状态到 server。"""
    from cluster import agent_loop, load_config
    cfg = load_config()
    server = args.server or cfg.get("server")
    if not server:
        print("未配置 server。用 `ordis join <server-url> [--token xxx]` 或"
              " --server 参数指定。")
        raise SystemExit(1)
    agent_loop(server, token=args.token or cfg.get("token"),
               interval=args.interval,
               verify=args.ca_cert or cfg.get("ca_cert") or True)


def cmd_join(args):
    """加入集群：写入 server 地址（和 token）到 ~/.ordis/cluster.json。"""
    from cluster import save_config, load_config
    cfg = load_config()
    cfg["server"] = args.server
    if args.token:
        cfg["token"] = args.token
    if args.ca_cert:
        cfg["ca_cert"] = args.ca_cert
    save_config(cfg)
    print(f"已加入集群: {args.server}（配置 ~/.ordis/cluster.json）")
    print("启动上报: ordis agent   （或 systemd/PM2 常驻）")


def _fmt_pct(v) -> str:
    """百分比单元格渲染：None 显示 '-'；0 是合法值不能当缺失。"""
    return "-" if v is None else str(v)


def cmd_nodes(args):
    """集群全局视图：所有节点状态一览。"""
    import requests
    from cluster import load_config
    cfg = load_config()
    server = args.server or cfg.get("server")
    if not server:
        print("本机未加入集群（ordis join <server-url>），"
              "或用 --server 指定。")
        raise SystemExit(1)
    headers = {"X-Ordis-Token": args.token or cfg.get("token")} \
        if (args.token or cfg.get("token")) else {}
    try:
        r = requests.get(f"{server.rstrip('/')}/nodes",
                         headers=headers, timeout=8,
                         verify=args.ca_cert or cfg.get("ca_cert") or True)
        r.raise_for_status()
    except Exception as e:
        print(f"无法连接 server: {e}")
        raise SystemExit(1)
    data = r.json()
    nodes = data.get("nodes", [])
    if not nodes:
        print("(集群中还没有节点上报)")
        return
    print(f"{'节点':<16} {'CPU%':>6} {'内存%':>6} {'磁盘%':>6} "
          f"{'服务':>4} {'端口异常':>6}  最后上报")
    for n in nodes:
        age = n.get("age_sec", -1)
        seen = "刚刚" if 0 <= age <= 60 else f"{age // 60} 分钟前" if age > 0 else "?"
        print(f"{n['node']:<16} "
              f"{_fmt_pct(n.get('cpu_pct')):>6} "
              f"{_fmt_pct(n.get('mem_pct')):>6} "
              f"{_fmt_pct(n.get('disk_pct')):>6} "
              f"{n.get('services', 0):>4} "
              f"{n.get('ports_down', 0):>6}  {seen}")
    pending = [o for o in data.get("orders", []) if o["status"] == "pending"]
    if pending:
        print(f"\n待执行指令 {len(pending)} 条")


def cmd_shellhook(args):
    """输出 bash 钩子代码（写入 .bashrc 后记录命令历史供 ordis talk 使用）。"""
    import shell_history
    print(shell_history.install_hook())


def cmd_watch(args):
    """
    在录制会话中启动交互 shell：命令与输出全量记录到 ~/.ordis/session.log，
    ordis talk 会自动读取。exit 退出后录像保留。
    """
    import os
    import subprocess
    import shell_history as sh
    sh.SESSION_LOG.parent.mkdir(parents=True, exist_ok=True)
    shell = os.environ.get("SHELL", "/bin/bash")
    print(f"Ordis 会话录制中 → {sh.SESSION_LOG}（exit 退出）")
    sys.stdout.flush()
    # script -f 实时落盘；-q 静默；-e 返回子 shell 退出码
    try:
        rc = subprocess.call(["script", "-fqe", str(sh.SESSION_LOG), "-c", shell])
    except FileNotFoundError:
        print("本机缺少 script(1) 命令，无法录制（Linux util-linux 自带）")
        raise SystemExit(1)
    raise SystemExit(rc)


def cmd_promote(args):
    """规则晋升：查看/应用/放弃 AI 诊断生成的规则草案。"""
    import promotion
    import db

    if args.apply:
        if not args.command:
            print("失败: --apply 必须同时提供 --command")
            raise SystemExit(1)
        try:
            skill = promotion.apply_draft(
                args.apply, args.command, name=args.name, port=args.port,
                cooldown=args.cooldown, check_command=args.check_command)
            print(f"✓ 已生成待确认技能: {skill['id']}")
            print(f"  确认生效: ordis skills confirm {skill['id']}")
        except ValueError as e:
            print(f"失败: {e}")
            raise SystemExit(1)
        return

    if args.discard:
        print("已放弃" if promotion.discard_draft(args.discard)
              else f"未找到草案: {args.discard}")
        return

    # 默认：列表视图
    drafts = [d for d in db.load_drafts()
              if d.get("status") in ("pending", "duplicate")]
    if not drafts:
        print("(无待审批草案。同指纹故障复发≥2次且有成功AI诊断后自动生成)")
        return
    for d in drafts:
        if d.get("status") == "duplicate":
            print(f"[{d['id']}] 与已有技能重复  目标: {d.get('existing_skill_id')}"
                  f"  复发次数: {d.get('occurrences', 1)}"
                  + ("  命令有变化" if d.get("command_changed") else ""))
            skill_data = d.get("skill", {})
            print(f"  根因: {skill_data.get('root_cause', '')}")
            sc = d.get("suggested_command", "")
            cut = sc[:150] + ("…" if len(sc) > 150 else "")
            print(f"  命令: {cut}")
            print(f"  并入: ordis skills merge {d['id']}"
                  f"   放弃: ordis promote --discard {d['id']}\n")
            continue
        skill_data = d.get("skill", {})
        print(f"[{d['id']}] {d['fingerprint']}  复发次数: {d.get('occurrences', 1)}"
              + (f"  置信度{skill_data.get('confidence')}" if skill_data.get("confidence") else ""))
        print(f"  根因: {skill_data.get('root_cause', '')}")
        fd = skill_data.get("fix_direction", "")
        cut = fd[:150] + ("…" if len(fd) > 150 else "")
        print(f"  方向: {cut}\n")


def cmd_skills(args):
    """技能库：查看/确认/停用/删除 已学会的修复流程。"""
    import promotion
    import db

    # Canonical form: `ordis skills confirm <skill-id>`; retain flags for
    # scripts and older installations.
    action = getattr(args, "action", None)
    target = getattr(args, "target", None)
    if action and not target:
        print(f"错误：skills {action} 缺少 ID")
        raise SystemExit(2)
    if action == "confirm":
        args.confirm = target
    elif action == "merge":
        args.merge = target
    elif action == "disable":
        args.disable = target
    elif action == "enable":
        args.enable = target
    elif action == "delete":
        args.delete = target

    if args.confirm:
        try:
            result = promotion.confirm_skill(args.confirm)
            s = promotion.get_skill(args.confirm)
            print(f"✓ 技能已生效: {s['name']}")
            print(f"  命令: {s['command']}")
            print("同类故障复发时将自动执行此流程")
        except ValueError as e:
            print(f"失败: {e}")
            raise SystemExit(1)
        return

    if args.merge:
        try:
            s = promotion.merge_duplicate(args.merge)
            print(f"✓ 已并入技能: {s['name']} ({s['id']})")
            print(f"  当前命令: {s['command']}")
            if s.get("status") == "active":
                print("  已生效技能：rules.yaml 规则命令已同步")
        except ValueError as e:
            print(f"失败: {e}")
            raise SystemExit(1)
        return

    if args.disable or args.enable or args.delete:
        sid = args.disable or args.enable or args.delete
        s = promotion.get_skill(sid)
        if not s:
            print(f"未找到技能: {sid}")
            raise SystemExit(1)
        if args.delete:
            db.delete_skill(sid)
            # 从 rules.yaml 移除对应规则
            from config import load_rules, save_rules
            rules = load_rules()
            n0 = len(rules.get("rules") or [])
            rules["rules"] = [r for r in (rules.get("rules") or [])
                              if r.get("_skill_id") != sid]
            save_rules(rules)
            print(f"已删除技能 {sid}（移除规则 {n0 - len(rules['rules'])} 条）")
            return
        status = "disabled" if args.disable else "active"
        promotion.set_status(sid, status)
        # 同步 rules.yaml 中规则的启用状态
        from config import load_rules, save_rules
        rules = load_rules()
        for r in (rules.get("rules") or []):
            if r.get("_skill_id") == sid:
                r["enabled"] = (status == "active")
        save_rules(rules)
        print(f"技能 {sid} → {status}")
        return

    # 默认：列表视图
    skills = db.load_skills()
    if not skills:
        print("(技能库为空。故障复发→AI诊断→promote 审批后技能入库)")
        return
    print(f"{'状态':<14} {'名称':<26} 来源草案 / 命令")
    for s in skills:
        st = {"pending_confirm": "⚠ 待确认", "active": "● 生效",
              "disabled": "○ 停用"}.get(s["status"], s["status"])
        cmd = s["command"]
        cut = cmd[:60] + ("…" if len(cmd) > 60 else "")
        print(f"{st:<14} {s['name'][:24]:<26} {cut}")
        print(f"{'':<14} {'':<26} id={s['id']}")
        if args.verbose:
            print(f"{'':<14} {'':<26} 根因: {s.get('description','')[:80]}")
        if s["status"] == "pending_confirm":
            print(f"\n⚠ 待确认技能不会自动执行。生效请执行:")
            print(f"   ordis skills confirm {s['id']}\n")


def cmd_suggest(args):
    """AI 命令补全：ordis suggest -- 'top -tail 20'（或 --ordis 钩子触发）。"""
    import ai_suggest
    raw = " ".join(args.command).strip()
    if not raw:
        print("用法: ordis suggest -- '<命令片段>'")
        raise SystemExit(1)
    try:
        candidates = ai_suggest.suggest(raw)
    except KeyboardInterrupt:
        print("\n(已中断)")
        return
    if not candidates:
        print("(AI 未能给出候选命令)")
        return
    try:
        cmd = ai_suggest.choose(candidates)
    except KeyboardInterrupt:
        print("\n已取消")
        return
    if not cmd:
        print("已取消")
        return
    if ai_suggest.confirm_exec(cmd):
        ai_suggest.run_selected(cmd)


def cmd_level(args):
    """AI 权限分级：view(只读建议) / operate(正常操作) / root(root 权限)。"""
    import ai_levels

    if args.set:
        if ai_levels.set_level(args.set):
            print(f"AI 权限已设为 {args.set}")
            _print_level_help()
        else:
            print(f"无效等级: {args.set}（可选 view / operate / root）")
            raise SystemExit(1)
        return

    lv = ai_levels.current_level()
    print(f"当前 AI 权限: {lv}")
    _print_level_help()


def _print_level_help():
    print("""分级说明:
  view     talk 只读命令直接执行，写操作需逐条确认
  operate  talk 常规运维命令直接执行，系统级越权操作需逐条确认
  root     talk 可直接执行任意非空命令，不再确认
设置: ordis config permission <view|operate|root>""")


PUBLIC_COMMANDS = (
    "setup", "config", "talk", "check", "once", "k8s", "status", "events",
    "cases", "discover", "run", "run-install", "server", "agent", "join",
    "nodes", "shellhook", "watch", "promote", "skills", "suggest",
)
LEGACY_COMMANDS = ("model", "ai-mode", "level", "view", "operate", "root")
COMMANDS = PUBLIC_COMMANDS + LEGACY_COMMANDS


class OrdisParser(argparse.ArgumentParser):
    """带拼写容错的解析器：输错子命令时给出最接近的建议（git 风格）。"""

    def error(self, message):
        import difflib
        import re as _re

        m = _re.search(
            r"argument (?:cmd|\{[^}]+\}): invalid choice: '([^']+)'", message)
        if m:
            bad = m.group(1)
            close = difflib.get_close_matches(bad, COMMANDS, n=1, cutoff=0.5)
            if close:
                reason = (f"未知命令 '{bad}'，你是不是想执行 "
                          f"`ordis {close[0]}`？")
            else:
                reason = f"未知命令 '{bad}'，运行 `ordis --help` 查看可用命令"
            self.exit(2, f"错误：{reason}\n")

        patterns = (
            (r"unrecognized arguments: (.+)",
             lambda match: f"无法识别的参数：{match.group(1)}"),
            (r"the following arguments are required: (.+)",
             lambda match: f"缺少必填参数：{match.group(1)}"),
            (r"argument ([^:]+): expected one argument",
             lambda match: f"参数 {match.group(1)} 需要一个值"),
            (r"argument ([^:]+): invalid choice: '([^']+)' \(choose from (.+)\)",
             lambda match: (f"参数 {match.group(1)} 的值 '{match.group(2)}' 无效，"
                            f"可选值：{match.group(3)}")),
        )
        for pattern, translate in patterns:
            match = _re.search(pattern, message)
            if match:
                self.exit(2, f"错误：{translate(match)}\n")
        self.exit(2, "错误：命令参数无效，请使用 `ordis <命令> --help` 查看用法\n")


def build_parser() -> OrdisParser:
    """Build the command parser for console-script and module entry points."""
    try:
        from ordis import __version__
    except ImportError:
        __version__ = "1.0.0"

    parser = OrdisParser(
        prog="ordis",
        description="Ordis - Server Self-Healing Platform")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--json", dest="output_json", action="store_true",
                        help="机器可读 JSON 输出（支持 config/check/k8s）")
    sub = parser.add_subparsers(
        dest="cmd", metavar="{" + ",".join(PUBLIC_COMMANDS) + "}")

    p_setup = sub.add_parser("setup", help="交互式一键配置模型、模式和权限")
    p_setup.set_defaults(func=cmd_setup)

    p_config = sub.add_parser("config", help="统一查看和修改 Ordis 配置")
    p_config.add_argument("--json", dest="output_json", action="store_true",
                          default=argparse.SUPPRESS, help="输出稳定 JSON")
    config_sub = p_config.add_subparsers(dest="section", metavar="配置项")

    p_config_show = config_sub.add_parser("show", help="查看配置汇总")
    p_config_show.add_argument(
        "--json", dest="output_json", action="store_true",
        default=argparse.SUPPRESS, help="输出稳定 JSON")

    p_config_model = config_sub.add_parser("model", help="配置或测试模型 API")
    p_config_model.add_argument(
        "model_action", nargs="?", default="setup",
        choices=["setup", "status", "test"],
        help="setup=配置，status=查看，test=真实连通测试")
    p_config_model.add_argument(
        "--json", dest="output_json", action="store_true",
        default=argparse.SUPPRESS, help="状态使用 JSON 输出")

    p_config_mode = config_sub.add_parser(
        "mode", help="配置 AI 自动修复或邮件建议模式")
    p_config_mode.add_argument(
        "mode", nargs="?", choices=["auto", "email"],
        help="不填则查看当前模式")
    p_config_mode.add_argument("--to", help="管理员收件邮箱")
    p_config_mode.add_argument("--smtp-host", help="SMTP 服务器")
    p_config_mode.add_argument("--smtp-port", type=int, help="SMTP 端口")
    p_config_mode.add_argument("--smtp-user", help="SMTP 用户名")
    p_config_mode.add_argument("--from", dest="from_address", help="发件邮箱")
    p_config_mode.add_argument("--password-env", help="SMTP 密码环境变量名")
    p_config_mode.add_argument(
        "--smtp-security", choices=["ssl", "starttls", "plain"],
        help="SMTP 连接安全模式")

    p_config_permission = config_sub.add_parser(
        "permission", help="查看或配置全局 AI 权限")
    p_config_permission.add_argument(
        "permission", nargs="?", choices=["view", "operate", "root"],
        help="不填则查看当前权限")

    p_config_test = config_sub.add_parser("test", help="检查当前配置")
    p_config_test.add_argument(
        "target", nargs="?", default="all",
        choices=["all", "model", "email"],
        help="model 会真实连接；email 仅检查配置，不发送邮件")
    p_config.set_defaults(func=cmd_config)

    p_run = sub.add_parser("run", help="启动守护进程")
    p_run.set_defaults(func=cmd_run)

    p_install = sub.add_parser("run-install", help="安装并启动 systemd 守护服务")
    p_install.set_defaults(func=cmd_run_install)

    p_once = sub.add_parser("once", help="运行一轮检查（调试）")
    p_once.add_argument("-v", "--verbose", action="store_true",
                        help="额外显示原始采集数据")
    p_once.add_argument("--wait-ai", action="store_true",
                        help="等待本轮异步 AI 接管完成后退出")
    p_once.set_defaults(func=cmd_once)

    p_check = sub.add_parser("check", help="检测 Linux 与传统业务故障（只读）")
    p_check.add_argument("--json", dest="output_json", action="store_true",
                         default=argparse.SUPPRESS, help="输出稳定 JSON")
    p_check.set_defaults(func=cmd_check)

    p_k8s = sub.add_parser("k8s", help="检测 Kubernetes 资源故障（只读）")
    p_k8s.add_argument("action", nargs="?", default="status",
                       choices=["status", "check", "doctor"],
                       help="status/check=检测故障，doctor=检查连接和权限")
    p_k8s.add_argument("--context", help="临时覆盖 kubectl context")
    p_k8s.add_argument("--kubeconfig", help="临时覆盖 kubeconfig 路径")
    p_k8s.add_argument("--json", dest="output_json", action="store_true",
                       default=argparse.SUPPRESS, help="输出稳定 JSON")
    p_k8s.set_defaults(func=cmd_k8s)

    p_events = sub.add_parser("events", help="查看触发事件")
    p_events.add_argument("-n", type=int, default=20, help="显示最近 N 条")
    p_events.set_defaults(func=cmd_events)

    p_cases = sub.add_parser("cases", help="查看 AI 诊断案例")
    p_cases.add_argument("-n", type=int, default=10, help="显示最近 N 条")
    p_cases.add_argument("-v", "--verbose", "--full", dest="full",
                         action="store_true",
                         help="显示完整修复方向（默认截断 100 字）")
    p_cases.set_defaults(func=cmd_cases)

    p_disc = sub.add_parser("discover", help="服务自动发现 (列表/纳管)")
    p_disc.add_argument("--adopt", metavar="SERVICE",
                        help="纳管服务，如 pm2:photography 或 systemd:nginx")
    p_disc.add_argument("--remove", metavar="SERVICE",
                        help="移除纳管")
    p_disc.set_defaults(func=cmd_discover)

    p_status = sub.add_parser("status", help="查看系统状态")
    p_status.set_defaults(func=cmd_status)

    p_model = sub.add_parser("model", help="AI 供应商管理 (添加/切换/测试)")
    p_model.add_argument("action", nargs="?", default="wizard",
                         choices=["wizard", "add", "status", "test"],
                         help="wizard/add=添加或切换(默认), status=查看, test=连通测试")
    p_model.add_argument("--name", help="非交互: 供应商名称")
    p_model.add_argument("--base", help="非交互: API Base URL")
    p_model.add_argument("--model", dest="model", help="非交互: 模型名称")
    p_model.add_argument("--key-env", dest="key_env",
                         help="非交互: 存放 API Key 的环境变量名（值不落命令行）")
    p_model.set_defaults(func=cmd_model)

    p_talk = sub.add_parser("talk", help="自然语言管理服务器（AI 对话）")
    p_talk.add_argument("question", nargs="*", help="问题；留空进入多轮对话")
    p_talk.set_defaults(func=cmd_talk)

    p_ai_mode = sub.add_parser(
        "ai-mode", help="AI 故障接管：自动修复或邮件建议")
    p_ai_mode.add_argument(
        "action", nargs="?", default="status",
        choices=["status", "auto", "email", "test-email"],
        help="status=查看，auto=自动修复，email=邮件建议，test-email=测试邮件")
    p_ai_mode.add_argument("--to", help="管理员收件邮箱")
    p_ai_mode.add_argument("--smtp-host", help="SMTP 服务器")
    p_ai_mode.add_argument("--smtp-port", type=int, help="SMTP 端口")
    p_ai_mode.add_argument("--smtp-user", help="SMTP 用户名")
    p_ai_mode.add_argument("--from", dest="from_address", help="发件邮箱")
    p_ai_mode.add_argument(
        "--password-env", help="保存 SMTP 密码的环境变量名")
    p_ai_mode.add_argument(
        "--smtp-security", choices=["ssl", "starttls", "plain"],
        help="SMTP 连接安全模式")
    p_ai_mode.set_defaults(func=cmd_ai_mode)

    p_server = sub.add_parser("server", help="启动集群 server（聚合各节点）")
    p_server.add_argument("--host", default="0.0.0.0")
    p_server.add_argument("--port", type=int, default=None)
    p_server.add_argument("--token",
                          help="集群管理 token（缺省自动生成并持久化）")
    p_server.add_argument("--enroll-node", metavar="NODE",
                          help="生成/轮换节点独立凭证后退出")
    p_server.add_argument("--tls-cert", help="TLS 证书文件")
    p_server.add_argument("--tls-key", help="TLS 私钥文件")
    p_server.set_defaults(func=cmd_server)

    p_agent = sub.add_parser("agent", help="集群 agent：常驻上报本机状态")
    p_agent.add_argument("--server", help="server 地址，如 http://127.0.0.1:9800")
    p_agent.add_argument("--token", help="覆盖配置文件中的节点 token")
    p_agent.add_argument("--ca-cert", help="验证 server TLS 的 CA 证书")
    p_agent.add_argument("--interval", type=int, default=30, help="上报间隔秒")
    p_agent.set_defaults(func=cmd_agent)

    p_join = sub.add_parser("join", help="加入集群（写入 server 地址）")
    p_join.add_argument("server", help="server 地址，如 http://127.0.0.1:9800")
    p_join.add_argument("--token", help="节点独立 token")
    p_join.add_argument("--ca-cert", help="验证 server TLS 的 CA 证书")
    p_join.set_defaults(func=cmd_join)

    p_nodes = sub.add_parser("nodes", help="集群全局视图：所有节点状态")
    p_nodes.add_argument("--server", help="直接指定 server 地址查询")
    p_nodes.add_argument("--token", help="覆盖配置中的管理 token")
    p_nodes.add_argument("--ca-cert", help="验证 server TLS 的 CA 证书")
    p_nodes.set_defaults(func=cmd_nodes)

    p_hook = sub.add_parser("shellhook",
                            help="输出 bash 钩子(eval 写入 .bashrc)，记录命令历史供 talk 使用")
    p_hook.set_defaults(func=cmd_shellhook)

    p_watch = sub.add_parser("watch",
                             help="录制终端会话（命令+输出），ordis talk 自动读取上下文")
    p_watch.set_defaults(func=cmd_watch)

    p_prom = sub.add_parser("promote", help="AI 诊断→规则草案审批")
    p_prom.add_argument("--apply", metavar="DRAFT_ID", help="应用指定草案")
    p_prom.add_argument("--discard", metavar="DRAFT_ID", help="放弃指定草案")
    p_prom.add_argument("--command", help="审批确认的修复命令")
    p_prom.add_argument("--port", type=int, help="关联监视端口/端口回检（可选）")
    p_prom.add_argument("--check-command", help="修复后的效果回检命令")
    p_prom.add_argument("--name", help="规则名称（默认自动生成）")
    p_prom.add_argument("--cooldown", type=int, default=300,
                        help="规则冷却秒数(默认300)")
    p_prom.set_defaults(func=cmd_promote)

    p_skills = sub.add_parser("skills", help="技能库：已学会的修复流程")
    p_skills.add_argument(
        "action", nargs="?",
        choices=["confirm", "merge", "disable", "enable", "delete"],
        help="操作：confirm/merge/disable/enable/delete")
    p_skills.add_argument("target", nargs="?", metavar="ID",
                          help="技能或草案 ID")
    p_skills.add_argument("--confirm", metavar="SKILL_ID",
                          help="确认技能生效（旧式写法）")
    p_skills.add_argument("--merge", metavar="DRAFT_ID",
                          help="审核通过：把重复草案并入已有技能")
    p_skills.add_argument("--disable", metavar="SKILL_ID", help="停用技能")
    p_skills.add_argument("--enable", metavar="SKILL_ID", help="重新启用")
    p_skills.add_argument("--delete", metavar="SKILL_ID", help="删除技能及规则")
    p_skills.add_argument("-v", "--verbose", action="store_true",
                          help="显示根因等详情")
    p_skills.set_defaults(func=cmd_skills)

    p_sug = sub.add_parser("suggest", help="AI 命令补全：推测意图给出候选命令")
    p_sug.add_argument("command", nargs="*", help="命令片段，如 top -tail 20")
    p_sug.set_defaults(func=cmd_suggest)

    p_level = sub.add_parser("level", help="AI 权限分级 (view/operate/root)")
    p_level.add_argument("set", nargs="?", default=None,
                         help="不填=查看当前等级")
    p_level.set_defaults(func=cmd_level)

    for level_name, help_text in (
            ("view", "将全局 AI 权限设为 view（只读直接执行）"),
            ("operate", "将全局 AI 权限设为 operate（常规运维直接执行）"),
            ("root", "将全局 AI 权限设为 root（所有命令直接执行）")):
        p_shortcut = sub.add_parser(level_name, help=help_text)
        p_shortcut.set_defaults(func=cmd_level, set=level_name)

    hidden = set(LEGACY_COMMANDS)
    sub._choices_actions[:] = [
        action for action in sub._choices_actions
        if action.dest not in hidden
    ]

    return parser


def main(argv=None):
    parser = build_parser()

    args = parser.parse_args(argv)
    import db
    migration = db.initialize()
    if migration["migrated"] or migration["failed"]:
        log.info("SQLite 旧数据迁移: 成功 %d 个文件，失败 %d 个文件",
                 migration["migrated"], migration["failed"])
    if args.cmd is None:
        parser.print_help()
    else:
        return args.func(args)


if __name__ == "__main__":
    main()
