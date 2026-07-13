"""Textual TUI for the ShipShape control plane (Phase 1).

Two tabs:
  * Pending   — denied domains harvested live from the proxy log; approve/dismiss.
  * Allow-list — every entry as a checkbox; toggle, then Apply to hot-reload Squid.

The denied-domain harvester runs in a background thread following
`docker logs -f egress-proxy`. NOTE: this is the interactive layer over the same
core as the CLI; smoke-test it with `shipshape tui` after `pipx install`.
"""

from __future__ import annotations

import time

from textual import work
from textual.app import App, ComposeResult
from textual.worker import get_current_worker
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.selection_list import Selection

from . import commands as cmds
from . import components, creds, docker_ops, egress, firstmate, images, otp, spool, watcher
from .allowlist import Allowlist
from .config import Config, Paths
from .harvester import parse_line
from .state import CommandStore, PendingStore


class QuitConfirm(ModalScreen[bool]):
    """Yes/No confirmation before quitting the control plane."""

    CSS = """
    QuitConfirm { align: center middle; }
    #quit_box { width: 56; height: auto; padding: 1 2; border: thick $warning; background: $surface; }
    #quit_box Horizontal { height: auto; align-horizontal: center; padding-top: 1; }
    """
    BINDINGS = [
        ("y", "yes", "Quit"),
        ("n", "no", "Cancel"),
        ("escape", "no", "Cancel"),
        ("q", "no", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="quit_box"):
            yield Static(
                "Quit the ShipShape control plane?\n\n"
                "The sandbox keeps running — this only closes the TUI."
            )
            with Horizontal():
                yield Button("Quit (y)", id="yes", variant="error")
                yield Button("Cancel (n)", id="no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class ShipShapeApp(App):
    CSS = """
    #status { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    DataTable { height: 1fr; }
    SelectionList { height: 1fr; }
    """
    BINDINGS = [
        ("q", "request_quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("a", "approve", "Approve"),
        ("w", "approve_wildcard", "Approve *.dom"),
        ("d", "dismiss", "Dismiss"),
        ("s", "apply_allowlist", "Apply toggles"),
        ("g", "refresh_gcp", "Refresh GCP"),
        ("o", "gen_otp", "New passphrase"),
        ("y", "cmd_accept", "Accept cmd"),
        ("n", "cmd_decline", "Decline cmd"),
        ("p", "provision", "Provision"),
        ("u", "stack_up", "Boot stack"),
        ("t", "shell", "Shell"),
        ("f", "quickstart", "Quick-start FM"),
    ]

    def __init__(self, paths: Paths):
        super().__init__()
        self.paths = paths
        self.store = PendingStore(paths.pending)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="stack"):
            with TabPane("Stack", id="stack"):
                yield Static("", id="stack_status")
                with Horizontal():
                    yield Button("Boot (u)", id="stack_up", variant="success")
                    yield Button("Shut down", id="stack_down", variant="error")
                    yield Button("Shell (t)", id="open_shell")
                    yield Button("Quick-start firstmate (f)", id="quickstart", variant="primary")
            with TabPane("Pending", id="pending"):
                yield DataTable(id="pending_table", cursor_type="row")
            with TabPane("Allow-list", id="allowlist"):
                yield SelectionList(id="allow_select")
                with Horizontal():
                    yield Button("Apply (s)", id="apply", variant="primary")
            with TabPane("Commands", id="commands"):
                yield DataTable(id="cmd_table", cursor_type="row")
            with TabPane("Credentials", id="creds"):
                yield Static("", id="creds_status")
                with Horizontal():
                    yield Button("Refresh GCP now (g)", id="refresh_gcp", variant="warning")
                    yield Button("New passphrase (o)", id="gen_otp")
            with TabPane("Provision", id="provision"):
                yield Static(
                    "Check the dev stacks you want, then Apply: enables their egress "
                    "domains + installs them in the container (unchecking disables the "
                    "domains it added).",
                    id="prov_help",
                )
                yield SelectionList(id="prov_select")
                with Horizontal():
                    yield Button("Apply (p)", id="prov_apply", variant="primary")
            with TabPane("Images", id="images"):
                yield Static("Snapshot the running container to a tag, then boot that "
                             "tag later. '●' marks the active image.", id="img_help")
                yield DataTable(id="img_table", cursor_type="row")
                with Horizontal():
                    yield Input(placeholder="new snapshot tag", id="snap_name")
                    yield Button("Snapshot", id="snapshot")
                    yield Button("Use selected", id="use_image")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#pending_table", DataTable).add_columns("hits", "method", "domain")
        self.query_one("#cmd_table", DataTable).add_columns("id", "reason", "command")
        self.query_one("#img_table", DataTable).add_columns("", "tag", "size", "created")
        self._cmd_rows: list[str] = []
        self._img_rows: list[str] = []
        self._refresh_pending()
        self._refresh_commands()
        self._reload_allowlist()
        self._refresh_creds()
        self._reload_provision()
        self._reload_stack()
        self._reload_images()
        self._harvest()
        self._spool_watch()
        self.set_interval(5.0, self._tick)  # auto-refresh so changes are visible

    # --- status helper ---
    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    # --- pending tab ---
    def _refresh_pending(self) -> None:
        self.store.load()  # pick up entries the spool watcher wrote from its own instance
        table = self.query_one("#pending_table", DataTable)
        table.clear()
        for host, e in self.store.items():
            table.add_row(str(e["count"]), e["method"], host, key=host)

    def _selected_host(self) -> str | None:
        table = self.query_one("#pending_table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        return table.get_row_at(table.cursor_row)[2]

    def _approve(self, host: str, wildcard: bool) -> None:
        domain = "." + host.lstrip(".") if wildcard else host
        entry = self.store.data.get(host)
        al = Allowlist.load(self.paths.allowlist)
        al.add(domain, enabled=True)
        res = egress.apply(al)
        if res.ok:
            if entry and entry.get("rid"):  # a broker request — reply via the spool
                spool.write_response(
                    self.paths.spool, entry["rid"], "ok", f"domain {domain} approved", time.time()
                )
            self.store.remove(host)
            self._refresh_pending()
            self._reload_allowlist()
        self._status(("approved " + domain) if res.ok else res.output.replace("\n", " "))

    def action_approve(self) -> None:
        host = self._selected_host()
        if host:
            self._approve(host, wildcard=False)

    def action_approve_wildcard(self) -> None:
        host = self._selected_host()
        if host:
            self._approve(host, wildcard=True)

    def action_dismiss(self) -> None:
        host = self._selected_host()
        if host:
            entry = self.store.data.get(host)
            if entry and entry.get("rid"):  # tell the waiting agent it was declined
                spool.write_response(
                    self.paths.spool, entry["rid"], "denied", f"domain {host} declined", time.time()
                )
            self.store.remove(host)
            self._refresh_pending()
            self._status(f"dismissed {host}")

    def action_request_quit(self) -> None:
        self.push_screen(QuitConfirm(), lambda quit_it: self.exit() if quit_it else None)

    # --- allow-list tab ---
    def _reload_allowlist(self) -> None:
        sel = self.query_one("#allow_select", SelectionList)
        sel.clear_options()
        al = Allowlist.load(self.paths.allowlist)
        for ln in al.entries():
            sel.add_option(Selection(ln.domain, ln.domain, initial_state=ln.enabled))

    def action_apply_allowlist(self) -> None:
        sel = self.query_one("#allow_select", SelectionList)
        chosen = set(sel.selected)
        al = Allowlist.load(self.paths.allowlist)
        for ln in al.entries():
            al.set_enabled(ln.domain, ln.domain in chosen)
        res = egress.apply(al)
        self._status(("applied — " + res.output).strip() if res.output else "applied")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply":
            self.action_apply_allowlist()
        elif event.button.id == "refresh_gcp":
            self.action_refresh_gcp()
        elif event.button.id == "gen_otp":
            self.action_gen_otp()
        elif event.button.id == "prov_apply":
            self.action_provision()
        elif event.button.id == "stack_up":
            self.action_stack_up()
        elif event.button.id == "stack_down":
            self._status("shutting down stack…")
            self._do_stack("down")
        elif event.button.id == "open_shell":
            self.action_shell()
        elif event.button.id == "quickstart":
            self.action_quickstart()
        elif event.button.id == "use_image":
            self.action_use_image()
        elif event.button.id == "snapshot":
            tag = self.query_one("#snap_name", Input).value.strip()
            if tag:
                self._status(f"snapshotting → shipshape-agent:{tag}…")
                self._do_snapshot(tag)
            else:
                self._status("enter a snapshot tag first")

    def action_refresh(self) -> None:
        self.store.load()
        self._refresh_pending()
        self._refresh_commands()
        self._reload_allowlist()
        self._refresh_creds()
        self._reload_provision()
        self._reload_stack()
        self._status("refreshed")

    def _tick(self) -> None:
        # Lightweight periodic refresh (every 5s) so the operator sees live changes
        # without pressing 'r'. File-backed reads only — no docker subprocess here.
        self._hb = getattr(self, "_hb", 0) + 1
        self.store.load()
        self._refresh_pending()
        self._refresh_commands()
        self._refresh_creds()
        self.sub_title = "auto-refresh " + ("●" if self._hb % 2 else "○")

    def action_shell(self) -> None:
        self._status(docker_ops.open_shell("agent-sandbox").output)

    def action_quickstart(self) -> None:
        self._status("quick-start firstmate: booting + injecting Claude creds…")
        self._do_quickstart()

    @work(thread=True, exclusive=True, group="ops")
    def _do_quickstart(self) -> None:
        ok, msg = firstmate.quick_start(self.paths, Config.load(self.paths.root))
        self.call_from_thread(self._status, ("firstmate: " + msg).replace("\n", " "))
        self.call_from_thread(self._reload_stack)

    # --- images tab (snapshot & relaunch) ---
    def _reload_images(self) -> None:
        table = self.query_one("#img_table", DataTable)
        table.clear()
        self._img_rows = []
        act = images.active(self.paths)
        for s in images.snapshots():
            mark = "●" if f"{images.PREFIX}:{s['tag']}" == act else ""
            table.add_row(mark, s["tag"], s["size"], s["created"])
            self._img_rows.append(s["tag"])

    def _selected_image(self) -> str | None:
        table = self.query_one("#img_table", DataTable)
        if not self._img_rows or table.cursor_row is None:
            return None
        if 0 <= table.cursor_row < len(self._img_rows):
            return self._img_rows[table.cursor_row]
        return None

    def action_use_image(self) -> None:
        tag = self._selected_image()
        if tag:
            self._status(f"active image → {images.set_active(self.paths, tag)} (applies on next boot)")
            self._reload_images()

    @work(thread=True)
    def _do_snapshot(self, tag: str) -> None:
        r = images.snapshot(tag)
        msg = (f"saved shipshape-agent:{tag}" if r.ok else r.output).replace("\n", " ")
        self.call_from_thread(self._status, msg)
        self.call_from_thread(self._reload_images)

    # --- stack tab ---
    def _reload_stack(self) -> None:
        r = docker_ops.compose(self.paths.root, ["ps"], timeout=20)
        self.query_one("#stack_status", Static).update(
            r.output.strip() or "(stack not running — press 'u' to boot)"
        )

    def action_stack_up(self) -> None:
        self._status("booting stack (first run builds images — can take minutes)…")
        self._do_stack("up")

    @work(thread=True, exclusive=True, group="ops")
    def _do_stack(self, action: str) -> None:
        if action == "up":
            r = docker_ops.compose(self.paths.root, ["up", "-d"], image=images.active(self.paths), timeout=1800)
        else:
            r = docker_ops.compose(self.paths.root, ["down"], timeout=180)
        msg = f"stack {action}: {'ok' if r.ok else 'FAILED ' + r.output[:120]}".replace("\n", " ")
        self.call_from_thread(self._status, msg)
        self.call_from_thread(self._reload_stack)

    # --- credentials tab ---
    def _refresh_creds(self) -> None:
        st = creds.status(self.paths)
        ost = otp.status(self.paths.state)
        if st:
            age = time.time() - st.get("created", 0)
            gcp = (
                f"SA:     {st.get('sa', '?')}\n"
                f"key id: {st.get('key_id', '?')}\n"
                f"age:    {int(age // 3600)}h {int((age % 3600) // 60)}m   (on-demand rotation)"
            )
        else:
            gcp = "No GCP key minted yet. Press 'g' to mint + inject one."
        if not ost:
            otpline = "passphrase: none registered (press 'o' to create one)"
        elif ost["used"]:
            otpline = "passphrase: used (press 'o' for a new one)"
        elif ost["expired"]:
            otpline = "passphrase: expired (press 'o' for a new one)"
        else:
            otpline = f"passphrase: active, expires in {ost['expires_in']}s"
        self.query_one("#creds_status", Static).update(gcp + "\n\n" + otpline)

    def action_refresh_gcp(self) -> None:
        self._status("minting + injecting GCP key…")
        self._do_refresh_gcp()

    @work(thread=True)
    def _do_refresh_gcp(self) -> None:
        res = creds.refresh_gcp(self.paths, Config.load(self.paths.root))
        self.call_from_thread(self._status, res.message.replace("\n", " "))
        self.call_from_thread(self._refresh_creds)

    # --- commands tab ---
    def _refresh_commands(self) -> None:
        table = self.query_one("#cmd_table", DataTable)
        table.clear()
        self._cmd_rows = []
        for rid, e in CommandStore(self.paths.commands).pending():
            table.add_row(rid[:8], cmds.sanitize(e.get("reason", "")), cmds.sanitize(e["command"]))
            self._cmd_rows.append(rid)

    def _selected_command(self) -> str | None:
        table = self.query_one("#cmd_table", DataTable)
        if not self._cmd_rows or table.cursor_row is None:
            return None
        if 0 <= table.cursor_row < len(self._cmd_rows):
            return self._cmd_rows[table.cursor_row]
        return None

    def action_cmd_accept(self) -> None:
        rid = self._selected_command()
        if rid:
            self._status(f"running {rid[:8]} on the host…")
            self._do_cmd_accept(rid)

    @work(thread=True)
    def _do_cmd_accept(self, rid: str) -> None:
        ok, output = cmds.accept(self.paths, Config.load(self.paths.root), rid)
        msg = f"{rid[:8]} {'ok' if ok else 'FAILED'} — {output[:120]}".replace("\n", " ")
        self.call_from_thread(self._status, msg)
        self.call_from_thread(self._refresh_commands)

    def action_cmd_decline(self) -> None:
        rid = self._selected_command()
        if rid:
            cmds.decline(self.paths, rid)
            self._refresh_commands()
            self._status(f"declined {rid[:8]}")

    def action_gen_otp(self) -> None:
        phrase = otp.generate(self.paths.state)
        self._status(f"one-time passphrase (single-use, 15m):  {phrase}")
        self._refresh_creds()

    # --- provision tab (init wizard) ---
    def _reload_provision(self) -> None:
        sel = self.query_one("#prov_select", SelectionList)
        sel.clear_options()
        for s in components.statuses(self.paths, Config.load(self.paths.root)):
            label = f"{s['name']}  —  {s['description']}"
            sel.add_option(Selection(label, s["name"], initial_state=s["provisioned"]))

    def action_provision(self) -> None:
        chosen = set(self.query_one("#prov_select", SelectionList).selected)
        self._status("applying provisioning (installs can take a while)…")
        self._do_provision(chosen)

    @work(thread=True, exclusive=True, group="ops")
    def _do_provision(self, chosen: set) -> None:
        cfg = Config.load(self.paths.root)
        msgs = []
        for s in components.statuses(self.paths, cfg):
            name = s["name"]
            if name in chosen:
                ok, _ = components.provision(self.paths, cfg, name)
                msgs.append(f"{name}:{'ok' if ok else 'FAIL'}")
            elif s["provisioned"]:
                components.deprovision(self.paths, name)
                msgs.append(f"{name}:off")
        self.call_from_thread(self._status, "provision — " + "  ".join(msgs))
        self.call_from_thread(self._reload_provision)
        self.call_from_thread(self._reload_allowlist)

    # --- background spool watcher (broker requests -> operator queues) ---
    @work(thread=True, exclusive=True, group="spool")
    def _spool_watch(self) -> None:
        import time as _t

        cfg = Config.load(self.paths.root)
        worker = get_current_worker()
        while not worker.is_cancelled:
            try:
                if watcher.process_once(self.paths, cfg):
                    self.call_from_thread(self._refresh_pending)
                    self.call_from_thread(self._refresh_commands)
                    self.call_from_thread(self._refresh_creds)
            except Exception:
                pass
            _t.sleep(1)

    # --- background harvester ---
    @work(thread=True, exclusive=True, group="harvest")
    def _harvest(self) -> None:
        import time

        worker = get_current_worker()
        while not worker.is_cancelled:
            proc = None
            try:
                proc = docker_ops.logs_popen()
                if proc.stdout is not None:
                    for line in proc.stdout:
                        if worker.is_cancelled:
                            break
                        d = parse_line(line)
                        if d:
                            self.store.record(d.host, d.method)
                            self.call_from_thread(self._refresh_pending)
            except Exception:  # proxy restarted / not up yet — retry
                pass
            finally:
                if proc:
                    proc.terminate()
            if worker.is_cancelled:
                break
            time.sleep(3)  # reconnect backoff
