"""Textual TUI for the ShipShape control plane.

Panes:
  * Dashboard   — the default view. Service dots + quick Shell/Shutdown (or Boot /
                  Quick-start when nothing is running), an Actionables queue (pending
                  network requests, agent commands, credential issues), and a live
                  event feed from the proxy + spool. New actionables ping + flash the
                  window (mute with 'm').
  * Stack       — boot / shut down / quick-start firstmate / user + root shells.
  * Settings    — firstmate repo + egress bandwidth, with Allow-list and Provision
                  as sub-sections.
  * Credentials — GCP key rotation, remote-refresh passphrase, Claude token.
  * Images      — snapshot / use / rename / delete / rebuild the agent image.

The denied-domain harvester + spool watcher run in background threads. This is the
interactive layer over the same core as the CLI; smoke-test with `shipshape tui`.
"""

from __future__ import annotations

import time

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Footer,
    Header,
    Input,
    RadioButton,
    RadioSet,
    RichLog,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.selection_list import Selection
from textual.worker import get_current_worker

from . import commands as cmds
from . import (
    components,
    config,
    creds,
    docker_ops,
    egress,
    firstmate,
    images,
    otp,
    settings,
    spool,
    watcher,
)
from .allowlist import Allowlist
from .config import Config, Paths
from .harvester import parse_access, parse_line
from .state import CommandStore, PendingStore

ACTION_ICON = {"domain": "🌐", "command": "⌘", "cred": "🔑"}


class QuitConfirm(ModalScreen[bool]):
    """Yes/No confirmation before quitting the control plane."""

    CSS = """
    QuitConfirm { align: center middle; }
    #quit_box { width: 56; height: auto; padding: 1 2; border: thick $warning; background: $surface; }
    #quit_box Horizontal { height: auto; align-horizontal: center; padding-top: 1; }
    """
    BINDINGS = [("y", "yes", "Quit"), ("n", "no", "Cancel"),
                ("escape", "no", "Cancel"), ("q", "no", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="quit_box"):
            yield Static("Quit the ShipShape control plane?\n\n"
                         "The sandbox keeps running — this only closes the TUI.")
            with Horizontal():
                yield Button("Quit (y)", id="yes", variant="error")
                yield Button("Cancel (n)", id="no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class SetupWizard(ModalScreen[bool]):
    """First-launch setup: pick the firstmate source repo + egress bandwidth. The
    default allow-list is already in place; it's tunable later in Settings."""

    CSS = """
    SetupWizard { align: center middle; }
    #wiz_box { width: 72; height: auto; max-height: 90%; padding: 1 2; border: thick $primary; background: $surface; }
    #wiz_box Static.h { text-style: bold; padding-top: 1; }
    #wiz_box RadioSet { height: auto; }
    #wiz_box Input { width: 60; }
    #wiz_box Horizontal { height: auto; align-horizontal: center; padding-top: 1; }
    #wiz_title { text-style: bold; color: $primary; }
    """

    def __init__(self, paths: Paths):
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        s = settings.load(self.paths)
        repos = list(settings.FIRSTMATE_REPOS.items())
        cur = s.get("firstmate_repo")
        with Vertical(id="wiz_box"):
            yield Static("⚓  Welcome to ShipShape — quick setup", id="wiz_title")
            yield Static("firstmate source repo (cloned when the image is built):", classes="h")
            buttons = [RadioButton(name, value=(url == cur)) for name, url in repos]
            buttons.append(RadioButton("Custom URL…", value=cur not in settings.FIRSTMATE_REPOS.values()))
            yield RadioSet(*buttons, id="wiz_repo")
            yield Input(value=("" if cur in settings.FIRSTMATE_REPOS.values() else cur),
                        placeholder="https://github.com/you/firstmate.git", id="wiz_repo_custom")
            yield Static("Egress bandwidth cap (MB/s):", classes="h")
            yield Input(value=str(s.get("bandwidth_mbps", 20)), id="wiz_bw")
            yield Static("A sensible default egress allow-list is already in place "
                         "(GitHub, Google, Anthropic, npm, PyPI…). Tune it anytime in "
                         "Settings → Allow-list.", classes="h")
            with Horizontal():
                yield Button("Finish", id="wiz_finish", variant="success")
                yield Button("Skip", id="wiz_skip")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "wiz_finish":
            self._save()
        settings.update(self.paths, first_run_done=True)
        self.dismiss(True)

    def _save(self) -> None:
        repos = list(settings.FIRSTMATE_REPOS.items())
        idx = self.query_one("#wiz_repo", RadioSet).pressed_index
        if 0 <= idx < len(repos):
            repo = repos[idx][1]
        else:  # Custom
            repo = self.query_one("#wiz_repo_custom", Input).value.strip() or settings.DEFAULTS["firstmate_repo"]
        try:
            bw = int(self.query_one("#wiz_bw", Input).value.strip() or 20)
        except ValueError:
            bw = 20
        settings.update(self.paths, firstmate_repo=repo, bandwidth_mbps=bw)
        egress.set_bandwidth(self.paths, bw)


class ShipShapeApp(App):
    CSS = """
    #status { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    DataTable { height: 1fr; }
    SelectionList { height: 1fr; }
    TabPane Horizontal { height: auto; }
    TabPane Horizontal Button { width: auto; }
    #snap_name, #claude_token_input, #bw_input { width: 34; }
    #fm_repo_custom { width: 60; }
    .section { text-style: bold; color: $accent; padding: 1 0 0 0; }
    #dash_services { height: auto; padding: 0 0 1 0; }
    #action_table { height: auto; max-height: 40%; }
    #feed { height: 1fr; min-height: 5; border: round $panel; }
    Collapsible { height: auto; }
    """
    BINDINGS = [
        ("q", "request_quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("m", "toggle_mute", "Mute"),
        ("a", "approve", "Approve"),
        ("w", "approve_wildcard", "Approve *.dom"),
        ("d", "dismiss", "Dismiss"),
        ("y", "cmd_accept", "Accept cmd"),
        ("n", "cmd_decline", "Decline cmd"),
        ("s", "apply_allowlist", "Apply allow-list"),
        ("p", "provision", "Provision"),
        ("g", "refresh_gcp", "Refresh GCP"),
        ("o", "gen_otp", "New passphrase"),
        ("c", "set_claude_token", "Set Claude tok"),
        ("u", "stack_up", "Boot stack"),
        ("t", "shell", "Shell"),
        ("f", "quickstart", "Quick-start FM"),
    ]

    def __init__(self, paths: Paths):
        super().__init__()
        self.paths = paths
        self.cfg = Config.load(paths)
        config.set_active(paths, self.cfg)  # so docker_ops targets THIS instance
        self.title = f"ShipShape · {paths.instance}"
        self.store = PendingStore(paths.pending)
        self._action_rows: list[dict] = []

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                yield Static("", id="dash_services")
                with Horizontal(id="dash_svc_buttons"):
                    yield Button("Shell (t)", id="open_shell")
                    yield Button("Shut down", id="stack_down", variant="error")
                with Horizontal(id="dash_boot_buttons"):
                    yield Button("Boot (u)", id="stack_up", variant="success")
                    yield Button("Quick-start firstmate (f)", id="quickstart", variant="primary")
                yield Static("Actionables (0)", id="action_header", classes="section")
                yield DataTable(id="action_table", cursor_type="row")
                yield Static("Event feed", classes="section")
                yield RichLog(id="feed", max_lines=500, markup=True, highlight=False, wrap=True)
            with TabPane("Stack", id="stack"):
                yield Static("", id="stack_status")
                with Horizontal():
                    yield Button("Boot (u)", id="stack_up2", variant="success")
                    yield Button("Shut down", id="stack_down2", variant="error")
                    yield Button("Quick-start firstmate (f)", id="quickstart2", variant="primary")
                with Horizontal():
                    yield Button("Shell (t)", id="open_shell2")
                    yield Button("Root shell", id="open_root_shell", variant="warning")
            with TabPane("Settings", id="settings"):
                with Collapsible(title="General", collapsed=False):
                    yield Static("firstmate source repo (applied on image Rebuild):")
                    yield RadioSet(
                        *[RadioButton(name) for name in settings.FIRSTMATE_REPOS],
                        RadioButton("Custom URL…"),
                        id="fm_repo_set",
                    )
                    yield Input(placeholder="https://github.com/you/firstmate.git", id="fm_repo_custom")
                    yield Static("Egress bandwidth cap (MB/s):")
                    with Horizontal():
                        yield Input(id="bw_input")
                        yield Button("Save general", id="save_general", variant="primary")
                with Collapsible(title="Egress allow-list", collapsed=True):
                    yield SelectionList(id="allow_select")
                    with Horizontal():
                        yield Button("Apply (s)", id="apply", variant="primary")
                with Collapsible(title="Provision dev stacks", collapsed=True):
                    yield Static("Check the dev stacks you want, then Apply: enables their "
                                 "egress domains + installs them in the container.", id="prov_help")
                    yield SelectionList(id="prov_select")
                    with Horizontal():
                        yield Button("Apply (p)", id="prov_apply", variant="primary")
            with TabPane("Credentials", id="creds"):
                yield Static("", id="creds_status")
                with Horizontal():
                    yield Button("Refresh GCP now (g)", id="refresh_gcp", variant="warning")
                    yield Button("New passphrase (o)", id="gen_otp")
                with Horizontal():
                    yield Input(placeholder="paste `claude setup-token` output",
                                password=True, id="claude_token_input")
                    yield Button("Set Claude token (c)", id="set_claude_token", variant="primary")
            with TabPane("Images", id="images"):
                yield Static("Snapshot the running container to a tag, then boot that tag "
                             "later. '●' marks the active image. The box feeds Snapshot (new "
                             "tag) and Rename→ (new name for the selected row); Use/Delete "
                             "act on the selected row; Rebuild base rebuilds from the "
                             "Dockerfile.", id="img_help")
                yield DataTable(id="img_table", cursor_type="row")
                with Horizontal():
                    yield Input(placeholder="tag / new name", id="snap_name")
                    yield Button("Snapshot", id="snapshot")
                    yield Button("Rename→", id="rename_image")
                with Horizontal():
                    yield Button("Use selected", id="use_image")
                    yield Button("Delete selected", id="delete_image", variant="error")
                    yield Button("Rebuild base", id="rebuild_image", variant="warning")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#action_table", DataTable).add_columns("", "what")
        self.query_one("#img_table", DataTable).add_columns("", "tag", "size", "created")
        self._cmd_rows: list[str] = []
        self._img_rows: list[str] = []
        self._reload_allowlist()
        self._reload_provision()
        self._load_settings_general()
        self._refresh_creds()
        self._reload_stack()
        self._reload_images()
        self._reload_services()
        self._refresh_actionables()
        self._harvest()
        self._spool_watch()
        # Fast (1s) refresh while focused; 5s when the window loses focus.
        self._tick_interval = 1.0
        self._tick_timer = self.set_interval(self._tick_interval, self._tick)
        self.set_interval(4.0, self._reload_services)  # dashboard dots (docker ps)
        self._load_settings_general()
        if not settings.get(self.paths, "first_run_done"):
            self.push_screen(SetupWizard(self.paths), lambda _=None: self._post_wizard())

    def _post_wizard(self) -> None:
        self._load_settings_general()
        self._reload_allowlist()
        self._status("setup complete — welcome aboard ⚓")

    # ------------------------------------------------------------------ helpers
    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def _feed(self, msg: str) -> None:
        try:
            self.query_one("#feed", RichLog).write(f"[dim]{time.strftime('%H:%M:%S')}[/] {msg}")
        except Exception:
            pass

    # ------------------------------------------------------------------ dashboard
    def _reload_services(self) -> None:
        running = docker_ops.running_containers()
        services = [
            ("control-plane", self.cfg.broker_container),
            ("egress-proxy", self.cfg.proxy_container),
            ("agent-sandbox", self.cfg.agent_container),
        ]
        anyrun = any(cname in running for _, cname in services)
        dots = "   ".join(
            f"[green]●[/] {label}" if cname in running else f"[red]○[/] {label}"
            for label, cname in services
        )
        self.query_one("#dash_services", Static).update(
            dots if anyrun else f"Instance '{self.paths.instance}' is not running — boot it below."
        )
        self.query_one("#dash_svc_buttons").display = anyrun
        self.query_one("#dash_boot_buttons").display = not anyrun

    def _cred_issues(self) -> list[str]:
        out = []
        cfg = Config.load(self.paths)
        if cfg.agent_sa and not creds.status(self.paths):
            out.append("GCP key not minted — Credentials tab (g)")
        if not creds.claude_token(self.paths) and not (firstmate.HOST_CREDS.is_file()):
            out.append("Claude token not set — Credentials tab (c)")
        return out

    def _refresh_actionables(self) -> None:
        self.store.load()
        rows: list[dict] = []
        for host, e in self.store.items():
            rows.append({"kind": "domain", "host": host,
                         "summary": f"{e['method']} {host}  ({e['count']}×)"})
        for rid, e in CommandStore(self.paths.commands).pending():
            reason = f" — {cmds.sanitize(e['reason'])}" if e.get("reason") else ""
            rows.append({"kind": "command", "id": rid,
                         "summary": f"$ {cmds.sanitize(e['command'])[:70]}{reason}"})
        for issue in self._cred_issues():
            rows.append({"kind": "cred", "summary": issue})
        self._action_rows = rows
        tbl = self.query_one("#action_table", DataTable)
        tbl.clear()
        for r in rows:
            tbl.add_row(ACTION_ICON[r["kind"]], r["summary"])
        self.query_one("#action_header", Static).update(f"Actionables ({len(rows)})")
        self._maybe_ping(len(rows))

    def _maybe_ping(self, count: int) -> None:
        prev = getattr(self, "_last_action_count", None)
        self._last_action_count = count
        if prev is None or count <= prev:
            return
        if not settings.get(self.paths, "muted"):
            self.bell()  # sound + (in most emulators) flashes / highlights the window

    def _selected_actionable(self) -> dict | None:
        tbl = self.query_one("#action_table", DataTable)
        if not self._action_rows or tbl.cursor_row is None:
            return None
        if 0 <= tbl.cursor_row < len(self._action_rows):
            return self._action_rows[tbl.cursor_row]
        return None

    def action_toggle_mute(self) -> None:
        muted = not settings.get(self.paths, "muted")
        settings.update(self.paths, muted=muted)
        self._status("🔇 sound muted" if muted else "🔔 sound on")

    # ------------------------------------------------------------------ domains
    def _approve(self, host: str, wildcard: bool) -> None:
        domain = "." + host.lstrip(".") if wildcard else host
        entry = self.store.data.get(host)
        al = Allowlist.load(self.paths.allowlist)
        al.add(domain, enabled=True)
        res = egress.apply(al)
        if res.ok:
            if entry and entry.get("rid"):
                spool.write_response(self.paths.spool, entry["rid"], "ok",
                                     f"domain {domain} approved", time.time())
            self.store.remove(host)
            self._refresh_actionables()
            self._reload_allowlist()
            self._feed(f"[green]✓ approved[/] {domain}")
        self._status(("approved " + domain) if res.ok else res.output.replace("\n", " "))

    def action_approve(self) -> None:
        a = self._selected_actionable()
        if a and a["kind"] == "domain":
            self._approve(a["host"], wildcard=False)
        else:
            self._status("select a pending network request to approve")

    def action_approve_wildcard(self) -> None:
        a = self._selected_actionable()
        if a and a["kind"] == "domain":
            self._approve(a["host"], wildcard=True)
        else:
            self._status("select a pending network request first")

    def action_dismiss(self) -> None:
        a = self._selected_actionable()
        if not a or a["kind"] != "domain":
            self._status("select a pending network request to dismiss")
            return
        host = a["host"]
        entry = self.store.data.get(host)
        if entry and entry.get("rid"):
            spool.write_response(self.paths.spool, entry["rid"], "denied",
                                 f"domain {host} declined", time.time())
        self.store.remove(host)
        self._refresh_actionables()
        self._feed(f"[red]✗ dismissed[/] {host}")
        self._status(f"dismissed {host}")

    # ------------------------------------------------------------------ commands
    def action_cmd_accept(self) -> None:
        a = self._selected_actionable()
        if a and a["kind"] == "command":
            self._status(f"running {a['id'][:8]} on the host…")
            self._do_cmd_accept(a["id"])
        else:
            self._status("select an agent command to accept")

    @work(thread=True)
    def _do_cmd_accept(self, rid: str) -> None:
        ok, output = cmds.accept(self.paths, Config.load(self.paths), rid)
        msg = f"{rid[:8]} {'ok' if ok else 'FAILED'} — {output[:120]}".replace("\n", " ")
        self.call_from_thread(self._status, msg)
        self.call_from_thread(self._feed, f"{'[green]✓ ran[/]' if ok else '[red]✗ failed[/]'} {rid[:8]}")
        self.call_from_thread(self._refresh_actionables)

    def action_cmd_decline(self) -> None:
        a = self._selected_actionable()
        if a and a["kind"] == "command":
            cmds.decline(self.paths, a["id"])
            self._refresh_actionables()
            self._feed(f"[red]✗ declined[/] {a['id'][:8]}")
            self._status(f"declined {a['id'][:8]}")
        else:
            self._status("select an agent command to decline")

    # ------------------------------------------------------------------ buttons
    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "apply":
            self.action_apply_allowlist()
        elif bid == "prov_apply":
            self.action_provision()
        elif bid == "save_general":
            self.action_save_general()
        elif bid == "refresh_gcp":
            self.action_refresh_gcp()
        elif bid == "gen_otp":
            self.action_gen_otp()
        elif bid == "set_claude_token":
            self.action_set_claude_token()
        elif bid in ("stack_up", "stack_up2"):
            self.action_stack_up()
        elif bid in ("stack_down", "stack_down2"):
            self.action_stack_down()
        elif bid in ("open_shell", "open_shell2"):
            self.action_shell()
        elif bid == "open_root_shell":
            self.action_root_shell()
        elif bid in ("quickstart", "quickstart2"):
            self.action_quickstart()
        elif bid == "use_image":
            self.action_use_image()
        elif bid == "snapshot":
            tag = self.query_one("#snap_name", Input).value.strip()
            if tag:
                self._status(f"snapshotting → shipshape-agent:{tag}…")
                self._do_snapshot(tag)
            else:
                self._status("enter a snapshot tag first")
        elif bid == "rename_image":
            tag, new = self._selected_image(), self.query_one("#snap_name", Input).value.strip()
            if not tag:
                self._status("select an image to rename")
            elif not new:
                self._status("enter the new name in the tag box")
            else:
                self._status(f"renaming {tag} → {new}…")
                self._do_image_op("rename", tag, new)
        elif bid == "delete_image":
            tag = self._selected_image()
            if tag:
                self._status(f"deleting shipshape-agent:{tag}…")
                self._do_image_op("delete", tag)
            else:
                self._status("select an image to delete")
        elif bid == "rebuild_image":
            self._status("rebuilding shipshape-agent:base (can take minutes)…")
            self._do_image_op("rebuild")

    # ------------------------------------------------------------------ refresh
    def action_refresh(self) -> None:
        self._refresh_actionables()
        self._reload_allowlist()
        self._load_settings_general()
        self._refresh_creds()
        self._reload_provision()
        self._reload_stack()
        self._reload_services()
        self._status("refreshed")

    def _tick(self) -> None:
        # Lightweight file-backed refresh (no docker subprocess) — cheap at 1s.
        self._hb = getattr(self, "_hb", 0) + 1
        self._refresh_actionables()
        self._refresh_creds()
        rate = int(getattr(self, "_tick_interval", 1))
        self.sub_title = f"refresh {rate}s " + ("●" if self._hb % 2 else "○")

    def _set_tick_interval(self, seconds: float) -> None:
        if getattr(self, "_tick_interval", None) == seconds:
            return
        timer = getattr(self, "_tick_timer", None)
        if timer is not None:
            timer.stop()
        self._tick_interval = seconds
        self._tick_timer = self.set_interval(seconds, self._tick)

    def on_app_focus(self, event: events.AppFocus) -> None:
        self._set_tick_interval(1.0)

    def on_app_blur(self, event: events.AppBlur) -> None:
        self._set_tick_interval(5.0)

    def action_request_quit(self) -> None:
        self.push_screen(QuitConfirm(), lambda q: self.exit() if q else None)

    # ------------------------------------------------------------------ stack
    def _reload_stack(self) -> None:
        r = docker_ops.compose(["ps"], timeout=20)
        self.query_one("#stack_status", Static).update(
            r.output.strip() or "(stack not running — press 'u' to boot)"
        )

    def action_stack_up(self) -> None:
        self._status("booting stack (first run builds images — can take minutes)…")
        self.query_one("#stack_status", Static).update("⏳  Booting stack…  (first run builds images)")
        self._feed("[yellow]⏳ booting stack…[/]")
        self._do_stack("up")

    def action_stack_down(self) -> None:
        self._status("shutting down stack…")
        self.query_one("#stack_status", Static).update("⏳  Shutting down services…  (graceful stop)")
        self.query_one("#dash_services", Static).update("⏳  Shutting down services…")
        self._feed("[yellow]⏳ shutting down…[/]")
        self._do_stack("down")

    @work(thread=True, exclusive=True, group="ops")
    def _do_stack(self, action: str) -> None:
        if action == "up":
            r = docker_ops.compose(["up", "-d"],
                                   image=images.active(self.paths), timeout=1800)
        else:
            r = docker_ops.compose(["down"], timeout=180)
        msg = f"stack {action}: {'ok' if r.ok else 'FAILED ' + r.output[:120]}".replace("\n", " ")
        self.call_from_thread(self._status, msg)
        self.call_from_thread(self._feed, f"{'[green]✓[/]' if r.ok else '[red]✗[/]'} stack {action}")
        self.call_from_thread(self._reload_stack)
        self.call_from_thread(self._reload_services)

    def action_shell(self) -> None:
        self._status(docker_ops.open_shell().output)

    def action_root_shell(self) -> None:
        self._status(docker_ops.open_shell(user="0").output + " (root)")

    def action_quickstart(self) -> None:
        self._status("quick-start firstmate: booting + wiring Claude auth…")
        self._feed("[yellow]⏳ quick-start firstmate…[/]")
        self._do_quickstart()

    @work(thread=True, exclusive=True, group="ops")
    def _do_quickstart(self) -> None:
        ok, msg = firstmate.quick_start(self.paths, Config.load(self.paths))
        self.call_from_thread(self._status, ("firstmate: " + msg).replace("\n", " "))
        self.call_from_thread(self._reload_stack)
        self.call_from_thread(self._reload_services)

    # ------------------------------------------------------------------ settings
    def _load_settings_general(self) -> None:
        s = settings.load(self.paths)
        cur = s.get("firstmate_repo")
        urls = list(settings.FIRSTMATE_REPOS.values())
        target = urls.index(cur) if cur in urls else len(urls)  # last button = Custom
        buttons = list(self.query_one("#fm_repo_set", RadioSet).query(RadioButton))
        for i, b in enumerate(buttons):
            if i == target:
                b.value = True  # RadioSet unsets the siblings
        self.query_one("#fm_repo_custom", Input).value = "" if cur in urls else (cur or "")
        self.query_one("#bw_input", Input).value = str(s.get("bandwidth_mbps", 20))

    def action_save_general(self) -> None:
        repos = list(settings.FIRSTMATE_REPOS.items())
        idx = self.query_one("#fm_repo_set", RadioSet).pressed_index
        if 0 <= idx < len(repos):
            repo = repos[idx][1]
        else:
            repo = self.query_one("#fm_repo_custom", Input).value.strip() or settings.DEFAULTS["firstmate_repo"]
        try:
            bw = int(self.query_one("#bw_input", Input).value.strip() or 20)
        except ValueError:
            self._status("bandwidth must be a number")
            return
        old = settings.load(self.paths)
        settings.update(self.paths, firstmate_repo=repo, bandwidth_mbps=bw)
        note = egress.set_bandwidth(self.paths, bw).output
        if repo != old.get("firstmate_repo"):
            note += "  •  firstmate repo changed — Rebuild base (Images tab) to apply"
        self._status("saved: " + note)
        self._feed("[green]✓[/] settings saved")

    # ------------------------------------------------------------------ allow-list
    def _reload_allowlist(self) -> None:
        sel = self.query_one("#allow_select", SelectionList)
        sel.clear_options()
        for ln in Allowlist.load(self.paths.allowlist).entries():
            sel.add_option(Selection(ln.domain, ln.domain, initial_state=ln.enabled))

    def action_apply_allowlist(self) -> None:
        try:
            sel = self.query_one("#allow_select", SelectionList)
        except Exception:
            return
        chosen = set(sel.selected)
        al = Allowlist.load(self.paths.allowlist)
        for ln in al.entries():
            al.set_enabled(ln.domain, ln.domain in chosen)
        res = egress.apply(al)
        self._status(("applied — " + res.output).strip() if res.output else "applied")
        self._feed("[green]✓[/] allow-list applied")

    # ------------------------------------------------------------------ provision
    def _reload_provision(self) -> None:
        sel = self.query_one("#prov_select", SelectionList)
        sel.clear_options()
        for s in components.statuses(self.paths, Config.load(self.paths)):
            sel.add_option(Selection(f"{s['name']}  —  {s['description']}", s["name"],
                                     initial_state=s["provisioned"]))

    def action_provision(self) -> None:
        try:
            chosen = set(self.query_one("#prov_select", SelectionList).selected)
        except Exception:
            return
        self._status("applying provisioning (installs can take a while)…")
        self._do_provision(chosen)

    @work(thread=True, exclusive=True, group="ops")
    def _do_provision(self, chosen: set) -> None:
        cfg = Config.load(self.paths)
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

    # ------------------------------------------------------------------ credentials
    def _refresh_creds(self) -> None:
        st = creds.status(self.paths)
        ost = otp.status(self.paths.state)
        if st:
            age = time.time() - st.get("created", 0)
            gcp = (f"SA:     {st.get('sa', '?')}\n"
                   f"key id: {st.get('key_id', '?')}\n"
                   f"age:    {int(age // 3600)}h {int((age % 3600) // 60)}m   (on-demand rotation)")
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
        cst = creds.claude_token_status(self.paths)
        if cst.get("present"):
            age = time.time() - cst["mtime"]
            claudeline = (f"Claude token: set ({cst['chars']} chars, updated "
                          f"{int(age // 86400)}d {int((age % 86400) // 3600)}h ago)")
        else:
            claudeline = "Claude token: none — paste `claude setup-token` output below to add one"
        self.query_one("#creds_status", Static).update(gcp + "\n\n" + otpline + "\n" + claudeline)

    def action_refresh_gcp(self) -> None:
        self._status("minting + injecting GCP key…")
        self._do_refresh_gcp()

    @work(thread=True)
    def _do_refresh_gcp(self) -> None:
        res = creds.refresh_gcp(self.paths, Config.load(self.paths))
        self.call_from_thread(self._status, res.message.replace("\n", " "))
        self.call_from_thread(self._feed, f"{'[green]✓[/]' if res.ok else '[red]✗[/]'} GCP key refresh")
        self.call_from_thread(self._refresh_creds)
        self.call_from_thread(self._refresh_actionables)

    def action_gen_otp(self) -> None:
        phrase = otp.generate(self.paths.state)
        self._status(f"one-time passphrase (single-use, 15m):  {phrase}")
        self._refresh_creds()

    def action_set_claude_token(self) -> None:
        inp = self.query_one("#claude_token_input", Input)
        tok = inp.value.strip()
        if not tok:
            self._status("paste the `claude setup-token` output into the box first")
            return
        res = creds.set_claude_token(self.paths, tok)
        inp.value = ""
        self._status(res.message.replace("\n", " "))
        self._refresh_creds()
        self._refresh_actionables()

    # ------------------------------------------------------------------ images
    def _reload_images(self) -> None:
        table = self.query_one("#img_table", DataTable)
        table.clear()
        self._img_rows = []
        act = images.active(self.paths)
        for s in images.snapshots(self.paths):
            mark = "●" if f"{images.prefix(self.paths)}:{s['tag']}" == act else ""
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
        r = images.snapshot(self.paths, tag)
        msg = (f"saved shipshape-agent:{tag}" if r.ok else r.output).replace("\n", " ")
        self.call_from_thread(self._status, msg)
        self.call_from_thread(self._reload_images)

    @work(thread=True, group="ops")
    def _do_image_op(self, op: str, *a) -> None:
        if op == "rename":
            old, new = a
            was_active = f"{images.prefix(self.paths)}:{old}" == images.active(self.paths)
            r = images.rename(self.paths, old, new)
            if r.ok and was_active:
                images.set_active(self.paths, new)
            msg = f"renamed {old} → {new}" if r.ok else r.output
        elif op == "delete":
            (tag,) = a
            if f"{images.prefix(self.paths)}:{tag}" == images.active(self.paths):
                images.set_active(self.paths, "base")
            r = images.delete(self.paths, tag)
            msg = f"deleted {tag}" if r.ok else r.output
        else:
            r = images.rebuild(self.paths)
            msg = "rebuilt shipshape-agent:base" if r.ok else r.output[-160:]
        self.call_from_thread(self._status, msg.replace("\n", " "))
        self.call_from_thread(self._reload_images)

    # ------------------------------------------------------------------ watchers
    @work(thread=True, exclusive=True, group="spool")
    def _spool_watch(self) -> None:
        import time as _t
        cfg = Config.load(self.paths)
        worker = get_current_worker()
        while not worker.is_cancelled:
            try:
                n = watcher.process_once(self.paths, cfg)
                if n:
                    self.call_from_thread(self._feed, f"[cyan]→ {n} new agent request(s)[/]")
                    self.call_from_thread(self._refresh_actionables)
                    self.call_from_thread(self._refresh_creds)
            except Exception:
                pass
            _t.sleep(1)

    @work(thread=True, exclusive=True, group="harvest")
    def _harvest(self) -> None:
        import time as _t
        worker = get_current_worker()
        while not worker.is_cancelled:
            proc = None
            try:
                proc = docker_ops.logs_popen()
                if proc.stdout is not None:
                    for line in proc.stdout:
                        if worker.is_cancelled:
                            break
                        acc = parse_access(line)
                        if acc and (acc.method == "CONNECT" or not acc.allowed):
                            icon = "[green]✓[/]" if acc.allowed else "[red]✗ DENIED[/]"
                            self.call_from_thread(self._feed, f"{icon} {acc.host}")
                        d = parse_line(line)
                        if d:
                            self.store.record(d.host, d.method)
                            self.call_from_thread(self._refresh_actionables)
            except Exception:
                pass
            finally:
                if proc:
                    proc.terminate()
            if worker.is_cancelled:
                break
            _t.sleep(3)
