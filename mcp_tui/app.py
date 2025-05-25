# stdlib imports
import asyncio
import json
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import List, Optional, Dict, Any

# 3rd party imports
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel
import typer
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Log


app_cli = typer.Typer()

class MCPServer(BaseModel):
    name: str
    command: Optional[str] = None
    type: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, Any]] = None
    url: Optional[str] = None
    defaultNamespace: Optional[str] = None
    # Add other fields as needed

class LogViewScreen(Screen):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
    ]

    def __init__(self, server_name: str, stdout_file, stderr_file):
        super().__init__()
        self.server_name = server_name
        self.stdout_file = stdout_file
        self.stderr_file = stderr_file
        self.stdout_lines = []
        self.stderr_lines = []
        if self.stdout_file:
            try:
                self.stdout_file.seek(0)
                self.stdout_lines = self.stdout_file.read().splitlines()
            except Exception:
                pass
        if self.stderr_file:
            try:
                self.stderr_file.seek(0)
                self.stderr_lines = self.stderr_file.read().splitlines()
            except Exception:
                pass
        self.log_widget = None

    def compose(self) -> ComposeResult:
        self.log_widget = Log(classes="log-pane", id="log-widget")
        yield self.log_widget
        yield Footer()

    async def on_mount(self) -> None:
        # Set dynamic border title if supported
        if hasattr(self.log_widget, "border_title"):
            self.log_widget.border_title = f"Logs for {self.server_name}"
        def write_logs():
            if self.stderr_lines:
                self.log_widget.write_lines(self.stderr_lines)
            if self.stdout_lines:
                self.log_widget.write_lines(["--- STDOUT ---"] + self.stdout_lines)
        self.call_after_refresh(write_logs)

    def on_key(self, event):
        if event.key == "escape":
            self.app.pop_screen()

class ToolsListScreen(Screen):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("q", "pop_screen", "Back"),
        Binding("escape", "pop_screen", "Back"),
        Binding("j", "j", "Down"),
        Binding("k", "k", "Up"),
    ]

    def __init__(self, server_name: str, tools: list, **kwargs):
        super().__init__(**kwargs)
        self.server_name = server_name
        self.tools = tools if tools is not None else []
        self.table = None

    def compose(self) -> ComposeResult:
        self.table = DataTable(id="tools-table", classes="tools-table-pane")
        self.table.border_title = f"Tools for {self.server_name}"
        self.table.add_column("Name")
        self.table.add_column("Description")
        if self.tools:
            for tool in self.tools:
                self.table.add_row(tool.name, tool.description)
        else:
            self.table.add_row("(No tools found or not yet loaded)")
        yield self.table
        yield Footer()

    def on_key(self, event):
        if event.key in ("escape", "q"):
            self.app.pop_screen()

    def action_j(self):
        if self.table:
            self.table.action_cursor_down()

    def action_k(self):
        if self.table:
            self.table.action_cursor_up()

class ServerListScreen(Screen):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("l", "show_logs", "Show Logs"),
        Binding("j", "j", "Down"),
        Binding("k", "k", "Up"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, servers: List[MCPServer], server_logs, **kwargs):
        super().__init__(**kwargs)
        self.servers = servers
        self.table = None
        self._row_keys = []
        self.status_col_key = None
        self.server_logs = server_logs
        self.server_tools = {}  # idx -> list of tool names

    def compose(self) -> ComposeResult:
        self.table = DataTable(id="servers-table", classes="servers-table-pane")
        self.table.border_title = "MCP Servers"
        self.table.add_column("Name")
        self.status_col_key = self.table.add_column("Status", width=8)
        self.table.add_column("Type", width=10)
        self.table.cursor_type = "row"
        self._row_keys = []
        for server in self.servers:
            name = server.name
            status = ""
            if server.type:
                type_ = server.type
            elif server.command:
                type_ = "stdio"
            elif server.url:
                type_ = "http"
            else:
                type_ = ""
            row_key = self.table.add_row(name, status, type_)
            self._row_keys.append(row_key)
        yield self.table
        yield Footer()

    async def on_mount(self) -> None:
        self.call_after_refresh(self.start_checks)

    def start_checks(self):
        for idx, server in enumerate(self.servers):
            asyncio.create_task(self.check_server(idx, server))

    async def check_server(self, idx, server: MCPServer):
        tools = []
        if not server.command and server.url:
            stderr_file = tempfile.TemporaryFile(mode="w+")
            self.server_logs[idx] = (None, stderr_file)
            self.update_status(idx, "[yellow]●[/yellow]")
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(server.url, timeout=5)
                    if response.status_code == 200:
                        self.update_status(idx, "[green]✔[/green]")
                        # HTTP servers: no tool list
                    else:
                        self.update_status(idx, "[red]✗[/red]")
                        stderr_file.write(f"HTTP status code: {response.status_code}\n")
                        stderr_file.flush()
            except Exception as e:
                self.update_status(idx, "[red]✗[/red]")
                import traceback
                stderr_file.write(traceback.format_exc())
                stderr_file.flush()
            finally:
                stderr_file.seek(0)
                stderr_contents = stderr_file.read()
                if not stderr_contents:
                    stderr_file.seek(0)
                    stderr_file.write("No output captured from HTTP health check or Python exception.")
                    stderr_file.flush()
                    stderr_file.seek(0)
            self.server_tools[idx] = tools
        else:
            stderr_file = tempfile.TemporaryFile(mode="w+")
            self.server_logs[idx] = (None, stderr_file)
            self.update_status(idx, "[yellow]●[/yellow]")
            try:
                command = server.command
                args = server.args or []
                env = server.env or None
                server_params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=env
                )
                async with AsyncExitStack() as stack:
                    stdio, write = await stack.enter_async_context(
                        stdio_client(server_params, errlog=stderr_file)
                    )
                    session = await stack.enter_async_context(ClientSession(stdio, write))
                    await session.initialize()
                    tool_list = await session.list_tools()
                    print(f"Tool list: {tool_list}")
                    if isinstance(tool_list, dict) and "tools" in tool_list:
                        tools = [t["name"] if isinstance(t, dict) and "name" in t else str(t) for t in tool_list["tools"]]
                    elif isinstance(tool_list, list):
                        tools = [t["name"] if isinstance(t, dict) and "name" in t else str(t) for t in tool_list]
                    tools = tool_list.tools
                    print(f"Adding tools: {tools}")
                    self.server_tools[idx] = tools
                    self.update_status(idx, "[green]✔[/green]")
            except Exception as e:
                self.update_status(idx, "[red]✗[/red]")
                import traceback
                stderr_file.write(traceback.format_exc())
            finally:
                stderr_file.seek(0)
                stderr_contents = stderr_file.read()
                if not stderr_contents:
                    stderr_file.seek(0)
                    stderr_file.write("No output captured from process or Python exception.")
                    stderr_file.flush()
                    stderr_file.seek(0)

    def update_status(self, idx, status):
        if self.table and self.status_col_key is not None:
            self.table.update_cell(self._row_keys[idx], self.status_col_key, status)

    def action_show_logs(self):
        if not self.table or not self._row_keys:
            return
        row = self.table.cursor_row
        if row is None or row >= len(self.servers):
            return
        server = self.servers[row]
        logs = self.server_logs.get(row, (None, None))
        self.app.push_screen(LogViewScreen(server.name, logs[0], logs[1]))

    def action_j(self):
        if self.table:
            self.table.action_cursor_down()

    def action_k(self):
        if self.table:
            self.table.action_cursor_up()

    def action_quit(self):
        self.app.exit()

    def on_data_table_row_selected(self, event):
        # event.row_key gives the selected row's key
        if not self.table or not self._row_keys:
            return
        try:
            idx = self._row_keys.index(event.row_key)
        except ValueError:
            return
        if idx >= len(self.servers):
            return
        server = self.servers[idx]
        tools = self.server_tools.get(idx, [])
        print(f"Tools: {tools}")
        self.app.push_screen(ToolsListScreen(server.name, tools))

class MCPServerListApp(App):
    CSS_PATH = "app.tcss"

    def __init__(self, servers: List[MCPServer], **kwargs):
        super().__init__(**kwargs)
        self.servers = servers
        self.server_logs = {}  # idx -> (stdout, stderr)

    def on_mount(self):
        self.push_screen(ServerListScreen(self.servers, self.server_logs))

@app_cli.command()
def main(mcp_json: Path):
    """Open a TUI listing MCP servers from a mcp.json file."""
    with mcp_json.open() as f:
        data = json.load(f)
    # Adjusted for ~/.cursor/mcp.json structure
    servers = []
    mcp_servers = data.get("mcpServers", {})
    for name, config in mcp_servers.items():
        config = dict(config)  # ensure it's a dict
        config["name"] = name
        servers.append(MCPServer(**config))
    MCPServerListApp(servers).run()

if __name__ == "__main__":
    app_cli()
