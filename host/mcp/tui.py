#!/usr/bin/env python3
"""Textual TUI dashboard for the vicegerent host ToolHive stack.

Read-mostly dashboard over vicegerent_mcp: shows every enabled ToolHive workload plus
the supervised processes (vMCP, ghostunnel, rclone-s3, mcp-health-watch,
caffeinate), tails their logs, and offers start / stop / restart of the
supervised stack.

Keybindings (k9s-flavoured):
  j/k, ↑/↓     navigate workload rows
  enter         open logs for the selected workload
  ctrl+s        start the stack
  ctrl+k        stop (kill) the supervised stack
  ctrl+r        restart the supervised stack
  1-7           switch log tabs (vmcp, operator-vmcp, ghostunnel, rclone-s3,
                mcp-health-watch, supervisord, caffeinate)
  r             refresh now
  ?             help
  q / esc       quit
"""

from __future__ import annotations

import sys
from time import monotonic
from pathlib import Path
from threading import Thread
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Log, Markdown, Static, TabbedContent, TabPane

from vicegerent_mcp import (
    DEFAULT_RUNTIME_DIR,
    DEFAULT_SERVERS_CONFIG,
    SUPERVISED_PROGRAMS,
    get_supervisor_states,
    group_name,
    is_server_enabled,
    list_workloads,
    load_server_state,
    load_servers_config,
    runtime_paths,
    start_stack,
    stop_stack,
    tail_log_iter,
    workload_log_process,
)

LOG_TABS = (
    "vmcp", "operator-vmcp", "ghostunnel", "rclone-s3",
    "mcp-health-watch", "supervisord", "caffeinate",
)
LOG_TAB_LABELS = {
    name: f"{index} {name}" for index, name in enumerate(LOG_TABS, start=1)
}


def _proc_markup(state: str) -> str:
    if state == "RUNNING":
        return f"[bold #5fd7a7]●[/bold #5fd7a7] [#5fd7a7]{state.lower()}[/#5fd7a7]"
    if state in ("STARTING", "BACKOFF"):
        return f"[bold #ffd166]◐[/bold #ffd166] [#ffd166]{state.lower()}[/#ffd166]"
    if state:
        return f"[bold #ff6b81]●[/bold #ff6b81] [#ff6b81]{state.lower()}[/#ff6b81]"
    return "[dim]○ stopped[/dim]"


def _workload_markup(state: str) -> str:
    if state == "running":
        return f"[bold #5fd7a7]●[/bold #5fd7a7] [#5fd7a7]{state}[/#5fd7a7]"
    if state in ("starting", "auth_retrying", "authenticating"):
        return f"[bold #ffd166]◐[/bold #ffd166] [#ffd166]{state.replace('_', ' ')}[/#ffd166]"
    if state:
        return f"[bold #ff6b81]●[/bold #ff6b81] [#ff6b81]{state.replace('_', ' ')}[/#ff6b81]"
    return "[dim]○ not created[/dim]"


def _health_summary(states: list[str], healthy: str) -> str:
    """Return a compact Rich-markup health summary."""
    total = len(states)
    ready = sum(state == healthy for state in states)
    color = "#5fd7a7" if ready == total and total else "#ffd166" if ready else "#ff6b81"
    return f"[bold {color}]{ready}/{total} healthy[/bold {color}]"


class HelpScreen(ModalScreen):
    """Full-screen help overlay."""

    BINDINGS = [Binding("escape", "dismiss", show=False), Binding("question_mark", "dismiss", show=False)]

    HELP_TEXT = """\
# vicegerent host stack — keybindings

## Navigation
| Key | Action |
|-----|--------|
| `j` / `↓` | Move down |
| `k` / `↑` | Move up |
| `Enter` | Open selected workload logs |
| `r` | Refresh now |

## Stack control
| Key | Action |
|-----|--------|
| `ctrl+s` | Start the stack |
| `ctrl+k` | Stop (kill) the supervised stack |
| `ctrl+r` | Restart the supervised stack |

## Log tabs
| Key | Tab |
|-----|-----|
| `1` | vmcp |
| `2` | operator-vmcp |
| `3` | ghostunnel |
| `4` | rclone-s3 |
| `5` | mcp-health-watch |
| `6` | supervisord |
| `7` | caffeinate |

## General
| Key | Action |
|-----|--------|
| `?` | Toggle this help |
| `q` / `Esc` | Quit |
"""

    def compose(self) -> ComposeResult:
        with Vertical(id="help-container"):
            yield Markdown(self.HELP_TEXT)

    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help-container {
        width: 64; height: auto; max-height: 80%;
        padding: 1 2; background: $surface; border: double $primary; overflow-y: auto;
    }
    """


class WorkloadLogScreen(Screen):
    """Live ToolHive logs for a selected MCP workload."""

    BINDINGS = [
        Binding("r", "refresh_logs", "Reload", show=True),
        Binding("escape", "dismiss", "Back", show=True),
        Binding("q", "dismiss", "Back", show=True),
    ]

    CSS = """
    WorkloadLogScreen { background: #0d1117; }
    #workload-log-header {
        height: 3; padding: 0 2; background: #171c28; border-bottom: tall #8b7cf6;
        content-align: left middle;
    }
    #workload-log { height: 1fr; padding: 1 2; background: #0b0f15; color: #b8c1d1; }
    """

    def __init__(self, workload: str) -> None:
        super().__init__()
        self.workload = workload
        self._process = None

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold #b9adff]◈ {self.workload}[/bold #b9adff]\n[dim]ToolHive workload logs · Esc/q to return[/dim]",
            id="workload-log-header",
        )
        yield Log(id="workload-log", max_lines=500)
        yield Footer()

    def on_mount(self) -> None:
        self._stream_logs()

    def on_unmount(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()

    def action_refresh_logs(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self.query_one("#workload-log", Log).clear()
        self.app.notify(f"Reloading {self.workload} logs…")
        self._stream_logs()

    @work(thread=True, exclusive=True)
    def _stream_logs(self) -> None:
        widget = self.query_one("#workload-log", Log)
        try:
            self._process = workload_log_process(self.workload)
            if self._process.stdout is None:
                raise RuntimeError("ToolHive log stream has no output")
            pending = []
            last_flush = monotonic()
            for line in self._process.stdout:
                pending.append(line.rstrip("\n"))
                now = monotonic()
                if len(pending) >= 200 or now - last_flush >= 0.1:
                    self.app.call_from_thread(widget.write_lines, pending)
                    pending = []
                    last_flush = now
            if pending:
                self.app.call_from_thread(widget.write_lines, pending)
        except Exception as exc:
            self.app.call_from_thread(widget.write_line, f"Unable to read logs: {exc}")
        finally:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()


class HostMCPApp(App):
    TITLE = "vicegerent"
    SUB_TITLE = "host control plane"

    CSS = """
    $accent: #8b7cf6;
    $success: #5fd7a7;
    $warning: #ffd166;
    $danger: #ff6b81;

    Screen { layout: vertical; background: #0d1117; color: #d8dee9; }
    #header {
        height: 3; padding: 0 2; background: #171c28; border-bottom: tall $accent;
    }
    #brand { width: 1fr; content-align: left middle; color: #f4f0ff; }
    #stack-health { width: auto; content-align: right middle; padding-left: 2; }
    .section-title {
        height: 2; padding: 1 1 0 1; color: #9aa7bd; text-style: bold;
    }
    #workload-summary { width: 1fr; text-align: right; }
    #workload-table {
        height: auto; max-height: 12; margin: 0 1; background: #111722;
        border: round #303a52;
    }
    #workload-table > .datatable--header { background: #20283a; color: #c8d0e0; text-style: bold; }
    #workload-table > .datatable--cursor { background: #393268; color: #ffffff; text-style: bold; }
    #infra-status {
        height: auto; min-height: 1; margin: 0 1; padding: 0 1; color: #aeb8ca;
    }
    #log-tabs { height: 1fr; margin: 0 1 1 1; background: #0b0f15; }
    TabbedContent { border: round #303a52; }
    Tabs { background: #171c28; color: #9aa7bd; }
    Tab.-active { color: #ffffff; background: #393268; text-style: bold; }
    Log { padding: 0 1; background: #0b0f15; color: #b8c1d1; }
    Footer { height: 1; background: #171c28; color: #aeb8ca; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("enter", "workload_logs", "Logs", show=True, priority=True),
        Binding("r", "refresh_now", "Refresh", show=True),
        Binding("ctrl+s", "start_stack", "Start", show=True),
        Binding("ctrl+k", "stop_stack", "Kill", show=True),
        Binding("ctrl+r", "restart_stack", "Restart", show=True),
        Binding("1", "tab_vmcp", "vmcp", show=False),
        Binding("2", "tab_operator_vmcp", "operator-vmcp", show=False),
        Binding("3", "tab_ghostunnel", "ghostunnel", show=False),
        Binding("4", "tab_rclone_s3", "rclone-s3", show=False),
        Binding("5", "tab_mcp_health_watch", "mcp-health-watch", show=False),
        Binding("6", "tab_supervisord", "supervisord", show=False),
        Binding("7", "tab_caffeinate", "caffeinate", show=False),
        Binding("question_mark", "help", "Help", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("escape", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        runtime_dir: Path = DEFAULT_RUNTIME_DIR,
        servers_config: Path = DEFAULT_SERVERS_CONFIG,
    ) -> None:
        super().__init__()
        self.runtime_dir = runtime_dir
        self.servers_config = servers_config
        self.config = load_servers_config(servers_config)
        self.group = group_name(self.config)
        self._log_threads: list[Thread] = []
        self._tailing = False
        self._workload_names: list[str | None] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static("[bold #b9adff]◈ VICEGERENT[/bold #b9adff]\n[dim]host control plane[/dim]", id="brand")
            yield Static("", id="stack-health")
        with Horizontal(classes="section-title"):
            yield Static("TOOLHIVE WORKLOADS")
            yield Static("", id="workload-summary")
        yield DataTable(id="workload-table")
        yield Static("SUPERVISED SERVICES", classes="section-title")
        yield Static("", id="infra-status")
        yield Static("LIVE LOGS", classes="section-title")
        with TabbedContent(id="log-tabs"):
            for name in LOG_TABS:
                with TabPane(LOG_TAB_LABELS[name], id=f"tab-{name}"):
                    yield Log(id=f"log-{name}")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#workload-table", DataTable)
        table.add_columns("Workload", "Status")
        table.cursor_type = "row"
        table.zebra_stripes = True
        self.refresh_data()
        self.set_interval(2, self.refresh_data)
        self._start_log_tailers()

    # ------------------------------------------------------------------ data

    def refresh_data(self) -> None:
        if len(self.screen_stack) > 1:
            return
        sup_states = get_supervisor_states(self.runtime_dir)
        self._refresh_header(sup_states)
        self._refresh_workloads()
        self._refresh_infra(sup_states)

    def _refresh_header(self, sup_states: dict[str, str]) -> None:
        header = self.query_one("#stack-health", Static)
        if not sup_states:
            label = "[dim]○ STACK STOPPED[/dim]"
        elif all(sup_states.get(p) == "RUNNING" for p in SUPERVISED_PROGRAMS):
            label = "[bold #5fd7a7]● STACK HEALTHY[/bold #5fd7a7]"
        else:
            label = "[bold #ffd166]◐ STACK DEGRADED[/bold #ffd166]"
        header.update(f"{label}\n[dim]group {self.group}  ·  ? for help[/dim]")

    def _refresh_workloads(self) -> None:
        table = self.query_one("#workload-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        workloads = list_workloads(self.group)
        state = load_server_state(self.runtime_dir)
        enabled_states = []
        self._workload_names = []
        for server in self.config.get("servers", []):
            name = server["name"]
            if not is_server_enabled(server, state):
                self._workload_names.append(None)
                table.add_row(f"[dim]{name}[/dim]", "[dim]—  disabled[/dim]")
            else:
                self._workload_names.append(name)
                workload_state = workloads.get(name, "")
                enabled_states.append(workload_state)
                table.add_row(f"[bold]{name}[/bold]", _workload_markup(workload_state))
        self.query_one("#workload-summary", Static).update(
            f"{_health_summary(enabled_states, 'running')}  [dim]· {len(enabled_states)} enabled[/dim]"
        )
        if table.row_count and cursor < table.row_count:
            table.move_cursor(row=cursor)

    def _refresh_infra(self, sup_states: dict[str, str]) -> None:
        widget = self.query_one("#infra-status", Static)
        parts = [f"{p}: {_proc_markup(sup_states.get(p, ''))}" for p in SUPERVISED_PROGRAMS]
        widget.update("  │  ".join(parts))

    # ------------------------------------------------------------------ logs

    def _start_log_tailers(self) -> None:
        if self._tailing:
            return
        self._tailing = True
        paths = runtime_paths(self.runtime_dir)
        for name in LOG_TABS:
            log_file = paths["logs"] / f"{name}.log"
            widget = self.query_one(f"#log-{name}", Log)
            if not log_file.exists():
                widget.write_line("No log file yet — start the stack first.")
                continue
            t = Thread(target=self._tail, args=(log_file, widget), daemon=True)
            t.start()
            self._log_threads.append(t)

    def _tail(self, log_file: Path, widget: Log) -> None:
        try:
            for line in tail_log_iter(log_file, n_lines=50):
                self.call_from_thread(widget.write_line, line)
        except Exception:
            pass

    # --------------------------------------------------------------- actions

    def action_cursor_up(self) -> None:
        self.query_one("#workload-table", DataTable).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#workload-table", DataTable).action_cursor_down()

    def action_refresh_now(self) -> None:
        self.refresh_data()
        self.notify("Dashboard refreshed")

    def action_workload_logs(self) -> None:
        row = self.query_one("#workload-table", DataTable).cursor_row
        if row >= len(self._workload_names) or self._workload_names[row] is None:
            self.notify("Select an enabled workload to view its logs", severity="warning")
            return
        self.push_screen(WorkloadLogScreen(self._workload_names[row]))

    @work(exclusive=True, thread=True)
    def action_start_stack(self) -> None:
        self.call_from_thread(self.notify, "Starting stack… (workloads + vMCP + ghostunnel)")
        try:
            rc = start_stack(self.runtime_dir, self.servers_config)
            msg = "Stack started" if rc == 0 else "Stack started with warnings — check logs"
            self.call_from_thread(self.notify, msg, severity="information" if rc == 0 else "warning")
            self.call_from_thread(self._start_log_tailers)
        except SystemExit as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
        self.call_from_thread(self.refresh_data)

    @work(exclusive=True, thread=True)
    def action_stop_stack(self) -> None:
        self.call_from_thread(self.notify, "Stopping supervised stack…")
        try:
            # stop_workloads defaults True; the TUI only ever stops the supervised
            # processes, so the ToolHive containers keep their OAuth tokens.
            stop_stack(self.runtime_dir, self.servers_config, stop_workloads=False)
            self.call_from_thread(self.notify, "Supervised stack stopped (workloads left running)")
        except SystemExit as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
        self.call_from_thread(self.refresh_data)

    @work(exclusive=True, thread=True)
    def action_restart_stack(self) -> None:
        self.call_from_thread(self.notify, "Restarting supervised stack…")
        try:
            stop_stack(self.runtime_dir, self.servers_config, stop_workloads=False)
            rc = start_stack(self.runtime_dir, self.servers_config)
            msg = "Stack restarted" if rc == 0 else "Restarted with warnings — check logs"
            self.call_from_thread(self.notify, msg, severity="information" if rc == 0 else "warning")
        except SystemExit as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
        self.call_from_thread(self.refresh_data)

    def _activate_tab(self, name: str) -> None:
        self.query_one("#log-tabs", TabbedContent).active = f"tab-{name}"

    def action_tab_vmcp(self) -> None:
        self._activate_tab("vmcp")

    def action_tab_operator_vmcp(self) -> None:
        self._activate_tab("operator-vmcp")

    def action_tab_ghostunnel(self) -> None:
        self._activate_tab("ghostunnel")

    def action_tab_rclone_s3(self) -> None:
        self._activate_tab("rclone-s3")

    def action_tab_mcp_health_watch(self) -> None:
        self._activate_tab("mcp-health-watch")

    def action_tab_supervisord(self) -> None:
        self._activate_tab("supervisord")

    def action_tab_caffeinate(self) -> None:
        self._activate_tab("caffeinate")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())


if __name__ == "__main__":
    HostMCPApp().run()
