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
import os
import sys
import time

from . import commands as cmds
from . import components, creds, docker_ops, egress, firstmate, images, otp, spool, watcher
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
    store = PendingStore(paths.pending)
    entry = store.data.get(args.host)
    if entry and entry.get("rid"):  # tell the waiting agent it was declined
        spool.write_response(paths.spool, entry["rid"], "denied", f"domain {args.host} declined", time.time())
    store.remove(args.host)
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
    if st:
        age = time.time() - st.get("created", 0)
        print(f"GCP SA:  {st.get('sa', '?')}")
        print(f"key id:  {st.get('key_id', '?')}")
        print(f"age:     {int(age // 3600)}h {int((age % 3600) // 60)}m  (on-demand rotation)")
    else:
        print("GCP:     no key minted yet (run `shipshape refresh-gcp`)")
    cst = creds.claude_token_status(paths)
    if cst.get("present"):
        age = time.time() - cst["mtime"]
        print(f"Claude:  token set ({cst['chars']} chars, updated {int(age // 86400)}d ago)")
    else:
        print("Claude:  no token (run `shipshape claude-token` to add one)")
    return 0


def cmd_claude_token(paths: Paths, args) -> int:
    token = args.token
    if not token:
        if sys.stdin.isatty():
            print("paste the `claude setup-token` output, then press Ctrl-D:", file=sys.stderr)
        token = sys.stdin.read()
    res = creds.set_claude_token(paths, token)
    print(res.message)
    return 0 if res.ok else 1


def cmd_up(paths: Paths, _args) -> int:
    img = images.active(paths)
    print(f"booting stack (image {img}; first run builds — can take a few minutes)…")
    r = docker_ops.compose(paths.root, ["up", "-d"], image=img, timeout=1800)
    print(r.output)
    return 0 if r.ok else 1


def cmd_snapshot(_paths: Paths, args) -> int:
    r = images.snapshot(args.tag)
    print(r.output or ("snapshot saved" if r.ok else "failed"))
    return 0 if r.ok else 1


def cmd_images(paths: Paths, _args) -> int:
    act = images.active(paths)
    snaps = images.snapshots()
    if not snaps:
        print("(no shipshape-agent images yet — build the stack, then `snapshot <tag>`)")
        return 0
    for s in snaps:
        mark = "* " if f"{images.PREFIX}:{s['tag']}" == act else "  "
        print(f"{mark}{s['tag']:<20} {s['size']:<10} {s['created']}")
    print(f"\nactive: {act}")
    return 0


def cmd_use(paths: Paths, args) -> int:
    print(f"active image set to {images.set_active(paths, args.tag)} (applies on next `shipshape up`)")
    return 0


def cmd_image_delete(paths: Paths, args) -> int:
    q = images._qualify(args.tag)
    if q == images.active(paths):
        images.set_active(paths, "base")
        print(f"(was the active image; reset active to {images.DEFAULT})")
    r = images.delete(args.tag)
    print(r.output or ("deleted" if r.ok else "failed"))
    return 0 if r.ok else 1


def cmd_image_rename(paths: Paths, args) -> int:
    was_active = images._qualify(args.old) == images.active(paths)
    r = images.rename(args.old, args.new)
    if r.ok:
        if was_active:
            images.set_active(paths, args.new)
        print(f"renamed {images._qualify(args.old)} → {images._qualify(args.new)}")
    else:
        print(r.output or "failed")
    return 0 if r.ok else 1


def cmd_rebuild(paths: Paths, _args) -> int:
    print(f"rebuilding {images.DEFAULT} from the Dockerfile (can take several minutes)…")
    r = images.rebuild(paths)
    print(r.output[-2000:] if r.output else ("built" if r.ok else "failed"))
    return 0 if r.ok else 1


def cmd_quickstart(paths: Paths, _args) -> int:
    ok, msg = firstmate.quick_start(paths, Config.load(paths.root))
    print(msg)
    return 0 if ok else 1


def cmd_down(paths: Paths, _args) -> int:
    r = docker_ops.compose(paths.root, ["down"], timeout=180)
    print(r.output or "stack down")
    return 0 if r.ok else 1


def cmd_status(paths: Paths, _args) -> int:
    r = docker_ops.compose(paths.root, ["ps"], timeout=30)
    print(r.output)
    return 0 if r.ok else 1


def cmd_shell(_paths: Paths, args) -> int:
    # hand off the terminal to an interactive shell in the container
    argv = ["docker", "exec", "-it"]
    if args.root:
        argv += ["-u", "0"]
    argv += [args.container, "/bin/bash"]
    os.execvp("docker", argv)
    return 0  # unreachable (execvp replaces the process)


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
    sub.add_parser("up", help="boot the stack (docker compose up -d)").set_defaults(fn=cmd_up)
    sub.add_parser("down", help="tear down the stack (docker compose down)").set_defaults(fn=cmd_down)
    sub.add_parser("status", help="show stack status (docker compose ps)").set_defaults(fn=cmd_status)
    sp = sub.add_parser("snapshot", help="save the running agent container as shipshape-agent:<tag>")
    sp.add_argument("tag")
    sp.set_defaults(fn=cmd_snapshot)
    sub.add_parser("images", help="list saved agent image snapshots").set_defaults(fn=cmd_images)
    sp = sub.add_parser("use", help="select which image tag `up` boots")
    sp.add_argument("tag")
    sp.set_defaults(fn=cmd_use)
    sub.add_parser("rebuild", help="rebuild shipshape-agent:base from the Dockerfile").set_defaults(fn=cmd_rebuild)
    sp = sub.add_parser("image-delete", help="delete a saved image tag (docker rmi)")
    sp.add_argument("tag")
    sp.set_defaults(fn=cmd_image_delete)
    sp = sub.add_parser("image-rename", help="rename a saved image tag")
    sp.add_argument("old")
    sp.add_argument("new")
    sp.set_defaults(fn=cmd_image_rename)
    sub.add_parser("quickstart", help="boot + inject Claude creds + open a firstmate claude session").set_defaults(fn=cmd_quickstart)
    sp = sub.add_parser("shell", help="open an interactive shell in a container")
    sp.add_argument("container", nargs="?", default="agent-sandbox")
    sp.add_argument("--root", action="store_true", help="open as root (uid 0) instead of agentdev")
    sp.set_defaults(fn=cmd_shell)
    sub.add_parser("reload", help="squid -k reconfigure").set_defaults(fn=cmd_reload)
    sub.add_parser("refresh-gcp", help="mint + inject a fresh GCP SA key").set_defaults(fn=cmd_refresh_gcp)
    sub.add_parser("creds", help="show injected credential status").set_defaults(fn=cmd_creds)
    sp = sub.add_parser("claude-token", help="set/rotate the persistent Claude OAuth token (from `claude setup-token`)")
    sp.add_argument("token", nargs="?", help="the token (omit to read from stdin — keeps it out of shell history)")
    sp.set_defaults(fn=cmd_claude_token)

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
