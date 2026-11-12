from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from .config import Config
from .engine import Engine
from .evaluation import evaluate
from .providers import make_provider
from .retrieval import KnowledgeBase
from .sandbox import DockerSandbox
from .storage import AGENTS, Store


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def show(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="evotask", description="EvoTask 多智能体任务工作台 v0.2")
    root.add_argument("--config", help="JSON 配置；不在其中存放 API 密钥")
    root.add_argument("--env-file", default=".env", help="dotenv 路径，默认读取当前目录 .env")
    root.add_argument("--no-env", action="store_true", help="不读取 dotenv 文件，保留进程环境变量")
    root.add_argument("--state-dir", help="状态、工作区和归档目录（默认 .evotask；demo 用 .evotask-demo）")
    commands = root.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="检查模型配置、Docker 和 Git；默认不发送模型请求")
    doctor.add_argument("--live", action="store_true", help="额外发送一次受限的真实模型连通性请求")
    serve = commands.add_parser("serve", help="启动 Web 工作台（需安装 .[web]）")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--trusted-proxy", action="append", default=[], help="仅信任此 IP 的代理协议头；可重复，默认不信任")
    demo = commands.add_parser("demo", help="运行明确标注的离线模型 fixture")
    demo.add_argument("--review", action="store_true", help="演示等待人工验收及跨进程恢复")
    run = commands.add_parser("run", help="启动真实模型任务")
    run.add_argument("goal")
    run.add_argument("--input", action="append", default=[], help="授权所有参与 Agent 读取的 .md/.txt 文件")
    run.add_argument("--no-memory", action="store_true", help="关闭跨任务经验读写，供对照实验使用")
    resume = commands.add_parser("resume", help="回复持久化的人工请求")
    resume.add_argument("run_id")
    resume.add_argument("request_id")
    resume.add_argument("reply", help="审批用 yes/no；澄清用自然语言")
    cont = commands.add_parser("continue", help="恢复非等待状态的中断运行；不自动重放未确认工具操作")
    cont.add_argument("run_id")
    commands.add_parser("list", help="列出运行")
    for name in ("status", "events"):
        cmd = commands.add_parser(name)
        cmd.add_argument("run_id")
    export = commands.add_parser("export", help="取回经过哈希校验的产物（包括失败时的部分产物）")
    export.add_argument("run_id")
    export.add_argument("artifact_id")
    export.add_argument("destination")
    kb = commands.add_parser("kb", help="管理授权文本知识库")
    kb_commands = kb.add_subparsers(dest="kb_command", required=True)
    add = kb_commands.add_parser("add")
    add.add_argument("path")
    add.add_argument("--scope", choices=["shared", *AGENTS], default="shared")
    kb_commands.add_parser("list")
    remove = kb_commands.add_parser("remove")
    remove.add_argument("document_id")
    search = kb_commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--agent", choices=AGENTS, default="researcher")
    chat = commands.add_parser("chat", help="纯对话；支持 group 和指定 Agent 私聊；用 run 发起工具任务")
    chat.add_argument("--agent", choices=["group", *AGENTS], default="group")
    chat.add_argument("--message", help="省略则进入交互会话，/quit 退出")
    chat.add_argument("--demo", action="store_true")
    history = commands.add_parser("history", help="由本地人类操作者查看聊天历史")
    history.add_argument("--agent", choices=["group", *AGENTS], default="group")
    evaluate_cmd = commands.add_parser("evaluate", help="运行批量评测，保留失败样本")
    evaluate_cmd.add_argument("--suite", default=str(PROJECT_ROOT / "benchmarks" / "live.jsonl"))
    evaluate_cmd.add_argument("--demo", action="store_true", help="只运行离线 fixture 集")
    return root


def print_run(engine: Engine, run: dict) -> None:
    print(f"\nRun: {run['id']}  Status: {run['status']}  Mode: {run['mode']}")
    print(f"报告：{engine.write_report(run)}")
    if run["pending"]:
        show(run["pending"])
        print("使用同一 --state-dir 运行 resume <run_id> <request_id> <reply>；审批回复为 yes/no。")
    show(engine.metrics(run))


def main(argv: list[str] | None = None) -> int:
    # Windows consoles and redirected files must preserve Chinese and Markdown.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    store = None
    try:
        if not args.no_env and args.env_file != ".env" and not Path(args.env_file).is_file():
            raise ValueError("指定的 --env-file 不存在")
        config = Config.load(args.config, env_path=None if args.no_env else args.env_file)
        if args.command == "serve":
            from .environment import read_env
            try:
                import uvicorn
                from .web.app import create_app
            except ImportError:
                raise ValueError('请先安装 Web 依赖：python -m pip install -e ".[web]"') from None
            file_settings = read_env(None if args.no_env else args.env_file)
            access_token = os.environ.get("EVOTASK_WEB_ACCESS_TOKEN", file_settings.get("EVOTASK_WEB_ACCESS_TOKEN", ""))
            hosts = os.environ.get("EVOTASK_WEB_ALLOWED_HOSTS", file_settings.get("EVOTASK_WEB_ALLOWED_HOSTS", "localhost,127.0.0.1"))
            if args.host not in {"127.0.0.1", "localhost", "::1"} and len(access_token) < 24:
                raise ValueError("对外监听需要在 .env 设置至少 24 字符的 EVOTASK_WEB_ACCESS_TOKEN，并配置 EVOTASK_WEB_ALLOWED_HOSTS。")
            allowed_hosts = [h.strip() for h in hosts.split(",") if h.strip()]
            if not allowed_hosts or any("*" in h or "/" in h or ":" in h for h in allowed_hosts):
                raise ValueError("EVOTASK_WEB_ALLOWED_HOSTS 应为具体主机名或 IPv4 地址，以逗号分隔，不含端口、路径或通配符。")
            import ipaddress
            for proxy in args.trusted_proxy:
                ipaddress.ip_address(proxy)
            app = create_app(config, Path(args.state_dir or ".evotask-web"), access_token, allowed_hosts)
            print(f"EvoTask Web: http://{args.host}:{args.port}  默认离线演示；在页面中可切换真实模型。")
            uvicorn.run(app, host=args.host, port=args.port, workers=1,
                        proxy_headers=bool(args.trusted_proxy), forwarded_allow_ips=",".join(args.trusted_proxy),
                        access_log=False)
            return 0
        is_demo = args.command == "demo" or getattr(args, "demo", False)
        state_path = Path(args.state_dir or (".evotask-demo" if is_demo else ".evotask"))
        if is_demo:
            config = replace(config, provider="demo", model="deterministic-fixture")
        if args.command == "doctor":
            diagnosis = {"python": sys.version.split()[0], "provider": config.provider, "model": config.model,
                  "api_key_present": bool(os.environ.get(config.api_key_env)),
                  "api_key_env": config.api_key_env, "base_url": config.base_url,
                  "dotenv_loaded": not args.no_env and Path(args.env_file).is_file(),
                  "api_timeout": config.api_timeout, "thinking_mode": config.thinking_mode,
                  "model_configured": config.model != "SET_TO_COURSE_MODEL",
                  "docker": DockerSandbox(config).doctor(), "git": bool(shutil.which("git")),
                  "note": "No live API request was sent; HKGAI chat-completions compatibility must be checked with course docs."}
            if args.live:
                try:
                    if config.provider == "demo":
                        raise ValueError("--live requires a real configured provider")
                    probe = replace(config, max_response_tokens=256, api_timeout=min(config.api_timeout, 45))
                    result = make_provider(probe).complete("chat", {
                        "agent": "coordinator", "history": [{"sender": "human", "body": "Reply exactly EVOTASK_API_OK."}]})
                    success = result.get("reply", "").strip() == "EVOTASK_API_OK"
                    diagnosis["live_api"] = {"success": success, "requests": 1,
                                             "detail": "JSON response validated" if success else "Unexpected probe reply"}
                except (ValueError, RuntimeError, OSError) as exc:
                    diagnosis["live_api"] = {"success": False, "detail": str(exc)}
                diagnosis["note"] = "Live probe attempted; no credential values or response body printed."
            show(diagnosis)
            return 1 if args.live and not diagnosis["live_api"]["success"] else 0
        if args.command == "evaluate":
            suite = PROJECT_ROOT / "benchmarks" / "demo.jsonl" if args.demo else Path(args.suite)
            print(evaluate(state_path, suite, config))
            return 0
        store = Store(state_path)
        if args.command == "list":
            show([{k: r[k] for k in ("id", "status", "mode", "goal")} for r in store.runs()])
            return 0
        if args.command in {"status", "events"}:
            show(store.load(args.run_id) if args.command == "status" else store.events(args.run_id))
            return 0
        if args.command == "history":
            show(store.messages("group" if args.agent == "group" else f"private:{args.agent}", limit=100))
            return 0
        if args.command == "export":
            run = store.load(args.run_id)
            artifact = next((a for a in run["artifacts"] if a["id"] == args.artifact_id), None)
            if not artifact:
                raise ValueError("Artifact not found in this run")
            destination = Path(args.destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                output.write(store.artifact_bytes(artifact))
            print(f"导出 {destination.resolve()}；验证状态：{artifact['verification']}")
            return 0
        with store.lock():
            if args.command == "kb":
                kb = KnowledgeBase(store)
                if args.kb_command == "add":
                    show(kb.add(Path(args.path), args.scope))
                elif args.kb_command == "remove":
                    kb.remove(args.document_id)
                    print("已移除文档及索引。")
                elif args.kb_command == "search":
                    show(kb.search(args.query, args.agent))
                else:
                    show([{k: doc[k] for k in ("id", "name", "version", "scope")} for doc in kb.documents()])
                return 0
            if args.command in {"resume", "continue"}:
                run = store.load(args.run_id)
                config = Config(**run["config"]) if not args.config else config
                engine = Engine(store, config, make_provider(config), progress=print)
                if args.command == "resume":
                    run = engine.resume(args.run_id, args.request_id, args.reply)
                else:
                    engine.advance(run)
                print_run(engine, run)
                return 1 if run["status"] == "failed" else 0
            provider = make_provider(config)
            if args.command == "chat":
                channel = "group" if args.agent == "group" else f"private:{args.agent}"
                agent = "coordinator" if args.agent == "group" else args.agent
                while True:
                    message = args.message if args.message is not None else input(f"[{args.agent}] > ")
                    if message.strip() == "/quit":
                        break
                    if not message.strip():
                        if args.message is not None:
                            break
                        continue
                    store.message(channel, "human", message)
                    response = provider.complete("chat", {"agent": agent, "channel": channel,
                                                          "history": store.messages(channel, agent)})
                    reply = response.get("reply")
                    if not isinstance(reply, str):
                        raise ValueError("Invalid chat response")
                    store.message(channel, agent, reply)
                    print(f"[{agent}] {reply}")
                    if args.message is not None:
                        break
                return 0
            engine = Engine(store, config, provider, progress=print)
            if args.command == "demo":
                KnowledgeBase(store).add(PROJECT_ROOT / "examples" / "knowledge" / "similarity.md")
                goal = "离线 DEMO：研究 cosine similarity，委派证据摘录并生成有来源的 Markdown 报告。"
                if args.review:
                    goal += " [REVIEW] 最终报告需要人工验收。"
                run = engine.new(goal)
            else:
                run = engine.new(args.goal, [Path(p) for p in args.input], memory=not args.no_memory)
            engine.advance(run)
            print_run(engine, run)
            return 1 if run["status"] == "failed" else 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。已持久化状态保留；使用 list/status/continue 查看或恢复。", file=sys.stderr)
        return 130
    finally:
        if store:
            store.close()
