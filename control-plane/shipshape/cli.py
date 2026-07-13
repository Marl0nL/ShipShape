"""shipshape — control-plane CLI.

Verified, scriptable interface over the same core the TUI uses:

    shipshape list                 show the allow-list (enabled/disabled)
    shipshape enable <domain>      enable a domain (uncomment) + reload
    shipshape disable <domain>     disable a domain (#SS-OFF#) + reload
    shipshape add <domain>         add a new enabled domain + reload
    shipshape approve <host>       add + drop from the pending queue + reload
    shipshape dismiss <host>       drop from the pending queue
    shipshape pending [--scan N]   show pending denials (optionally scan logs)
    shipshape reload               `squid -k reconfigure` only
    shipshape tui                  launch the Textual TUI
"""

from __future__ import annotations

import argparse
import sys
import time

from . import commands as cmds
from . import components, creds, docker_ops, egress, otp, spool, watcher
from .allowlist import Allowlist
from .config import Config, Paths
from .harvester import parse_line
from .state import CommandStore, PendingStore


def _print_result(r: docker_ops.Result) -> int:
    print(r.output or ("ok" if r.ok else "failed"))
    return 0 if r.ok else 1


def cmd_list(paths: Paths, _args) -> int:
    al = Allowlist.load(paths.allowlist)
    for ln in al.entries():
        print(f"[{'x' if ln.enabled else ' '}] {ln.domain}")
    return 0


def cmd_enable(paths: Paths, args) -> int:
    al = Allowlist.load(paths.allowlist)
    if not al.set_enabled(args.domain, True):
        print(f"not in allow-list: {args.domain} (use `add`)", file=sys.stderr)
        return 1
    return _print_result(egress.apply(al))


def cmd_disable(paths: Paths, args) -> int:
    al = Allowlist.load(paths.allowlist)
    if not al.set_enabled(args.domain, False):
        print(f"not in allow-list: {args.domain}", file=sys.stderr)
        return 1
    return _print_result(egress.apply(al))


def cmd_add(paths: Paths, args) -> int:
    al = Allowlist.load(paths.allowlist)
    al.add(args.domain, enabled=True)
    return _print_result(egress.apply(al))


def cmd_approve(paths: Paths, args) -> int:
    store = PendingStore(paths.pending)
    entry = store.data.get(args.host)
    al = Allowlist.load(paths.allowlist)
    al.add(args.host, enabled=True)
    res = egress.apply(al)
    if res.ok:
        if entry and entry.get("rid"):  # a broker request — reply via the spool
            spool.write_response(
                paths.spool, entry["rid"], "ok", f"domain {args.host} approved", time.time()
            )
        store.remove(args.host)
    return _print_result(res)


def cmd_dismiss(paths: Paths, args) -> int:
    PendingStore(paths.pending).remove(args.host)
    print(f"dismissed {args.host}")
    return 0


def cmd_pending(paths: Paths, args) -> int:
    store = PendingStore(paths.pending)
    if args.scan:
        r = docker_ops.logs_tail(n=args.scan)
        if r.ok:
            for line in r.output.splitlines():
                d = parse_line(line)
                if d:
                    store.record(d.host, d.method)
    items = store.items()
    if not items:
        print("(no pending domain requests)")
        return 0
    for host, e in items:
        print(f"{e['count']:>4}x  {e['method']:<7} {host}")
    return 0


def cmd_refresh_gcp(paths: Paths, _args) -> int:
    res = creds.refresh_gcp(paths, Config.load(paths.root))
    print(res.message)
    return 0 if res.ok else 1


def cmd_creds(paths: Paths, _args) -> int:
    import time

    st = creds.status(paths)
    if not st:
        print("no GCP key minted yet (run `shipshape refresh-gcp`)")
        return 0
    age = time.time() - st.get("created", 0)
    print(f"SA:     {st.get('sa', '?')}")
    print(f"key id: {st.get('key_id', '?')}")
    print(f"age:    {int(age // 3600)}h {int((age % 3600) // 60)}m  (on-demand rotation)")
    return 0


def cmd_reload(_paths: Paths, _args) -> int:
    if not docker_ops.container_running():
        print("egress-proxy is not running", file=sys.stderr)
        return 1
    return _print_result(docker_ops.reconfigure())


def cmd_otp(paths: Paths, args) -> int:
    if args.phrase:
        otp.register(paths.state, args.phrase, ttl=args.ttl)
        print(f"passphrase registered (single-use, expires in {args.ttl}s)")
    else:
        phrase = otp.generate(paths.state, ttl=args.ttl)
        print(f"one-time passphrase (single-use, expires in {args.ttl}s):\n\n    {phrase}\n")
        print("Hand this to the agent; it runs `refresh-daily-auth <passphrase>`.")
    return 0


def cmd_otp_status(paths: Paths, _args) -> int:
    st = otp.status(paths.state)
    if not st:
        print("no passphrase registered")
        return 0
    print(
        f"registered  used={st['used']}  expired={st['expired']}  "
        f"expires_in={st['expires_in']}s  attempts={st['attempts']}"
    )
    return 0


def cmd_commands(paths: Paths, _args) -> int:
    items = CommandStore(paths.commands).pending()
    if not items:
        print("(no pending commands)")
        return 0
    for rid, e in items:
        reason = f"  — {cmds.sanitize(e['reason'])}" if e.get("reason") else ""
        print(f"{rid[:8]}{reason}\n          $ {cmds.sanitize(e['command'])}")
    return 0


def cmd_command_accept(paths: Paths, args) -> int:
    ok, output = cmds.accept(paths, Config.load(paths.root), args.id)
    print(output)
    return 0 if ok else 1


def cmd_command_edit(paths: Paths, args) -> int:
    if cmds.edit(paths, args.id, args.command):
        print("updated")
        return 0
    print("no such pending command", file=sys.stderr)
    return 1


def cmd_command_decline(paths: Paths, args) -> int:
    if cmds.decline(paths, args.id):
        print("declined")
        return 0
    print("no such command", file=sys.stderr)
    return 1


def cmd_watch(paths: Paths, _args) -> int:
    """Headless daemon: harvest egress denials + process broker spool requests."""
    import threading

    cfg = Config.load(paths.root)
    store = PendingStore(paths.pending)
    stop = threading.Event()

    def harvest():
        while not stop.is_set():
            proc = None
            try:
                proc = docker_ops.logs_popen()
                if proc.stdout:
                    for line in proc.stdout:
                        if stop.is_set():
                            break
                        d = parse_line(line)
                        if d:
                            store.record(d.host, d.method)
            except Exception:
                pass
            finally:
                if proc:
                    proc.terminate()
            stop.wait(3)

    threading.Thread(target=harvest, daemon=True).start()
    print("watching egress denials + broker spool (Ctrl-C to stop)…")
    try:
        while True:
            n = watcher.process_once(paths, cfg)
            if n:
                print(f"processed {n} broker request(s)")
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        print("\nstopped")
    return 0


def cmd_components(paths: Paths, args) -> int:
    cfg = Config.load(paths.root)
    for s in components.statuses(paths, cfg, probe=args.probe):
        dom = "on " if s["domains_enabled"] else "off"
        prov = " provisioned" if s["provisioned"] else ""
        inst = "" if s["installed"] is None else (" installed" if s["installed"] else " NOT-installed")
        print(f"{s['name']:<10} domains:{dom}{prov}{inst}   {s['description']}")
    return 0


def cmd_provision(paths: Paths, args) -> int:
    ok, msg = components.provision(paths, Config.load(paths.root), args.name)
    print(msg)
    return 0 if ok else 1


def cmd_deprovision(paths: Paths, args) -> int:
    ok, msg = components.deprovision(paths, args.name)
    print(msg)
    return 0 if ok else 1


def cmd_tui(paths: Paths, _args) -> int:
    try:
        from .app import ShipShapeApp
    except ModuleNotFoundError as e:
        print(f"TUI needs Textual installed ({e}). `pipx install ./control-plane`.", file=sys.stderr)
        return 1
    ShipShapeApp(paths).run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shipshape", description="ShipShape control plane")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="show the allow-list").set_defaults(fn=cmd_list)
    for name, fn, helptext in [
        ("enable", cmd_enable, "enable a domain"),
        ("disable", cmd_disable, "disable a domain"),
        ("add", cmd_add, "add a new enabled domain"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("domain")
        sp.set_defaults(fn=fn)
    for name, fn, helptext in [
        ("approve", cmd_approve, "approve a pending denial"),
        ("dismiss", cmd_dismiss, "drop a pending denial"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("host")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("pending", help="show pending denials")
    sp.add_argument("--scan", type=int, default=0, metavar="N", help="scan last N log lines first")
    sp.set_defaults(fn=cmd_pending)
    sub.add_parser("reload", help="squid -k reconfigure").set_defaults(fn=cmd_reload)
    sub.add_parser("refresh-gcp", help="mint + inject a fresh GCP SA key").set_defaults(fn=cmd_refresh_gcp)
    sub.add_parser("creds", help="show injected credential status").set_defaults(fn=cmd_creds)

    sp = sub.add_parser("otp", help="register a one-time passphrase for remote refresh")
    sp.add_argument("phrase", nargs="?", help="passphrase (omit to auto-generate)")
    sp.add_argument("--ttl", type=int, default=otp.DEFAULT_TTL, help="validity seconds")
    sp.set_defaults(fn=cmd_otp)
    sub.add_parser("otp-status", help="show passphrase status").set_defaults(fn=cmd_otp_status)

    sub.add_parser("commands", help="list pending agent commands").set_defaults(fn=cmd_commands)
    sp = sub.add_parser("command-accept", help="run a pending command on the host")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_command_accept)
    sp = sub.add_parser("command-edit", help="edit a pending command before accepting")
    sp.add_argument("id")
    sp.add_argument("command")
    sp.set_defaults(fn=cmd_command_edit)
    sp = sub.add_parser("command-decline", help="decline a pending command")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_command_decline)

    sub.add_parser("watch", help="headless: harvest denials + process broker spool").set_defaults(fn=cmd_watch)

    sp = sub.add_parser("components", help="list installable components")
    sp.add_argument("--probe", action="store_true", help="check installed state in the container")
    sp.set_defaults(fn=cmd_components)
    sp = sub.add_parser("provision", help="enable a component's domains + install it in the container")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_provision)
    sp = sub.add_parser("deprovision", help="disable the domains a component added (no uninstall)")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_deprovision)

    sub.add_parser("tui", help="launch the TUI").set_defaults(fn=cmd_tui)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths.discover()
    paths.ensure()
    fn = getattr(args, "fn", None)
    if fn is None:
        return cmd_tui(paths, args)  # bare `shipshape` launches the TUI
    return fn(paths, args)


if __name__ == "__main__":
    raise SystemExit(main())
