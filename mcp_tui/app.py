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
from textual.screen import Screen, ModalScreen
from textual.widgets import DataTable, Footer, Log, Input, Button, Label, Static
from textual.containers import Container, Horizontal
from textual.events import Key
import importlib.resources


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
    CSS_PATH = importlib.resources.files("mcp_tui").joinpath("app.tcss")
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("/", "filter", "Filter"),
    ]

    def __init__(self, server_name: str, stdout_file, stderr_file):
        super().__init__()
        self.server_name = server_name
        self.stdout_file = stdout_file
        self.stderr_file = stderr_file
        self.stdout_lines = []
        self.stderr_lines = []
        self.filtered_lines = []
        self.current_regex = ""
        self.log_widget = None
        self.filter_input = None
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

    def compose(self) -> ComposeResult:
        self.log_widget = Log(classes="log-pane", id="log-widget")
        yield self.log_widget
        yield Footer()

    async def on_mount(self) -> None:
        self._refresh_log()
        if hasattr(self.log_widget, "border_title"):
            self.log_widget.border_title = f"Logs for {self.server_name}"

    def _refresh_log(self):
        self.log_widget.clear()
        all_lines = self.stderr_lines + (["--- STDOUT ---"] + self.stdout_lines if self.stdout_lines else [])
        if self.current_regex:
            import re
            try:
                regex = re.compile(self.current_regex, re.IGNORECASE)
                lines = [line for line in all_lines if regex.search(line)]
            except Exception:
                lines = all_lines
        else:
            lines = all_lines
        self.filtered_lines = lines
        if lines:
            self.log_widget.write_lines(lines)
        else:
            self.log_widget.write_lines(["(No log lines match filter)"])

    def action_filter(self):
        if self.filter_input:
            return
        self.filter_input = Input(placeholder="Regex filter (Enter=apply, Esc=cancel)", value=self.current_regex, id="filter-input")
        self.mount(self.filter_input)
        self.filter_input.focus()
        self.filter_input.border_title = "Filter"

    def on_input_submitted(self, event: Input.Submitted):
        if self.filter_input and event.input is self.filter_input:
            value = self.filter_input.value
            self.current_regex = value or ""
            self._refresh_log()
            self.filter_input.remove()
            self.filter_input = None

    def on_input_key(self, event: Key):
        if self.filter_input and event.sender is self.filter_input and event.key == "escape":
            self.filter_input.remove()
            self.filter_input = None

    def on_key(self, event):
        if self.filter_input and self.filter_input.has_focus:
            return  # Let filter input handle Esc
        if event.key == "escape":
            self.app.pop_screen()

class ToolInvokeModal(ModalScreen):
    CSS_PATH = importlib.resources.files("mcp_tui").joinpath("app.tcss")
    def __init__(self, tool, server, invoke_callback=None):
        super().__init__()
        self.tool = tool
        self.server = server
        self.invoke_callback = invoke_callback
        self.inputs = {}
        self.result = None

    def compose(self) -> ComposeResult:
        with Container():
            yield Label(f"Invoke tool: {getattr(self.tool, 'name', str(self.tool))}")
            schema = getattr(self.tool, "inputSchema", None)
            if schema and isinstance(schema, dict) and "properties" in schema:
                for field, field_info in schema["properties"].items():
                    field_type = field_info.get("type")
                    if field_type == "string":
                        input_widget = Input(id=f"input_{field}", name=field, placeholder=field)
                    elif field_type == "array" and field_info.get("items", {}).get("type") == "string":
                        input_widget = Input(id=f"input_{field}", name=field, placeholder=f"{field} (comma or newline separated)", multiline=True)
                    else:
                        input_widget = Input(id=f"input_{field}", name=field, placeholder=f"{field} (unsupported type: {field_type})")
                    self.inputs[field] = input_widget
                    yield input_widget
                    if field == "prompt":
                        break
            else:
                # No schema: let user specify key and value
                key_input = Input(id="input_key", placeholder="argument name (e.g. prompt)", value="prompt")
                value_input = Input(id="input_value", placeholder="argument value")
                self.inputs["key"] = key_input
                self.inputs["value"] = value_input
                yield key_input
                yield value_input
            with Horizontal():
                yield Button("Invoke", id="invoke", variant="success")
                yield Button("Cancel", id="cancel", variant="error")
            self.links_widget = Static("", id="tool-links-widget", classes="tool-links-pane")
            yield self.links_widget
            self.result = Log(
                id="tool-result-log",
                classes="tool-result-log-pane",
                highlight=True,
            )
            yield self.result

    def on_button_pressed(self, event):
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "invoke":
            schema = getattr(self.tool, "inputSchema", None)
            if schema and isinstance(schema, dict) and "properties" in schema:
                values = {}
                for field in schema["properties"]:
                    widget = self.inputs[field]
                    val = widget.value
                    field_type = schema["properties"][field].get("type")
                    if field_type == "array":
                        # Split by newlines or commas for arrays
                        values[field] = [v.strip() for v in val.replace(',', '\n').splitlines() if v.strip()]
                    else:
                        values[field] = val
                    if field == "prompt":
                        break
            else:
                key = self.inputs["key"].value.strip() or "prompt"
                value = self.inputs["value"].value
                values = {key: value}
            if self.invoke_callback:
                self.result.clear()
                self.result.write("[yellow]Invoking...[/yellow]")
                async def do_invoke():
                    try:
                        result = await self.invoke_callback(self.tool, self.server, values)
                        self.display_result(result)
                    except Exception as e:
                        self.result.write(f"[red]Error: {e}[/red]")
                import asyncio
                asyncio.create_task(do_invoke())

    def display_result(self, result):
        import json
        import re
        self.result.clear()
        self.links_widget.update("")
        urls = set()
        try:
            data = result
            if isinstance(result, str):
                data = json.loads(result)
            pretty = json.dumps(data, indent=2, ensure_ascii=False)
            urls.update(re.findall(r'https?://\S+', pretty))
        except Exception:
            pretty = str(result)
            urls.update(re.findall(r'https?://\S+', pretty))
        # Output URLs in the links widget if any
        if False and urls:
            def clean_url(url):
                return url.rstrip('.,;\'\"')
            links_markup = "[b]Links[/b]\n" + "\n".join(f"[link={clean_url(url)}]{clean_url(url)}[/link]" for url in urls)
            self.links_widget.update(links_markup)
        else:
            self.links_widget.update("")
        self.result.write(pretty)

class ToolsListScreen(Screen):
    CSS_PATH = importlib.resources.files("mcp_tui").joinpath("app.tcss")
    BINDINGS = [
        Binding("q", "pop_screen", "Back"),
        Binding("escape", "pop_screen", "Back"),
        Binding("j", "j", "Down"),
        Binding("k", "k", "Up"),
        Binding("/", "filter", "Filter"),
    ]

    def __init__(self, server_name: str, tools: list, server=None, invoke_callback=None, **kwargs):
        super().__init__(**kwargs)
        self.server_name = server_name
        self.tools = tools if tools is not None else []
        self.filtered_tools = self.tools
        self.table = None
        self.server = server
        self.invoke_callback = invoke_callback
        self.current_regex = ""
        self.filter_input = None

    def compose(self) -> ComposeResult:
        self.table = DataTable(id="tools-table", classes="tools-table-pane")
        self.table.border_title = f"Tools for {self.server_name}"
        self.table.add_column("Name")
        self.table.add_column("Description")
        self.table.cursor_type = "row"
        self._refresh_table()
        yield self.table
        yield Footer()

    def _refresh_table(self):
        self.table.clear()
        if self.filtered_tools:
            for tool in self.filtered_tools:
                name = getattr(tool, "name", str(tool))
                desc = getattr(tool, "description", "")
                self.table.add_row(name, desc)
        else:
            self.table.add_row("(No tools found or not yet loaded)")

    def action_filter(self):
        if self.filter_input:
            return
        self.filter_input = Input(placeholder="Regex filter (Enter=apply, Esc=cancel)", value=self.current_regex, id="filter-input")
        self.mount(self.filter_input)
        self.filter_input.focus()
        self.filter_input.border_title = "Filter"

    def on_input_submitted(self, event: Input.Submitted):
        if self.filter_input and event.input is self.filter_input:
            import re
            value = self.filter_input.value
            self.current_regex = value or ""
            if not self.current_regex:
                self.filtered_tools = self.tools
            else:
                try:
                    regex = re.compile(self.current_regex, re.IGNORECASE)
                    self.filtered_tools = [t for t in self.tools if regex.search(getattr(t, "name", "") + " " + getattr(t, "description", ""))]
                except Exception:
                    self.filtered_tools = self.tools
            self._refresh_table()
            self.filter_input.remove()
            self.filter_input = None

    def on_input_key(self, event: Key):
        if self.filter_input and event.sender is self.filter_input and event.key == "escape":
            self.filter_input.remove()
            self.filter_input = None

    def on_key(self, event):
        if self.filter_input and self.filter_input.has_focus:
            return  # Let filter input handle Esc
        if event.key == "escape":
            self.app.pop_screen()

    def action_j(self):
        if self.table:
            self.table.action_cursor_down()

    def action_k(self):
        if self.table:
            self.table.action_cursor_up()

    def on_data_table_row_selected(self, event):
        if not self.table or not self.tools:
            return
        row = self.table.cursor_row
        if row is None or row >= len(self.tools):
            return
        tool = self.tools[row]
        # Show modal dialog for tool invocation
        self.app.push_screen(ToolInvokeModal(tool, self.server, self.invoke_callback))

    async def invoke_tool_callback(self, tool, server, values):
        # This should invoke the tool on the server and return the result
        # Placeholder: just echo the input values
        # TODO: Implement actual tool invocation via MCP
        return f"Invoked {getattr(tool, 'name', str(tool))} with {values}"

class ServerListScreen(Screen):
    CSS_PATH = importlib.resources.files("mcp_tui").joinpath("app.tcss")
    BINDINGS = [
        Binding("l", "show_logs", "Show Logs"),
        Binding("j", "j", "Down"),
        Binding("k", "k", "Up"),
        Binding("q", "quit", "Quit"),
        Binding("/", "filter", "Filter"),
    ]

    def __init__(self, servers: List[MCPServer], server_logs, **kwargs):
        super().__init__(**kwargs)
        self.servers = servers
        self.filtered_servers = self.servers
        self.table = None
        self._row_keys = []
        self.status_col_key = None
        self.server_logs = server_logs
        self.server_tools = {}  # idx -> list of tool objects
        self.server_sessions = {}  # idx -> (AsyncExitStack, ClientSession)
        self.current_regex = ""
        self.filter_input = None

    def compose(self) -> ComposeResult:
        self.table = DataTable(id="servers-table", classes="servers-table-pane")
        self.table.border_title = "MCP Servers"
        self.table.add_column("Name")
        self.status_col_key = self.table.add_column("Status", width=8)
        self.table.add_column("Type", width=10)
        self.table.cursor_type = "row"
        self._refresh_table()
        yield self.table
        yield Footer()

    async def on_mount(self) -> None:
        self.call_after_refresh(self.start_checks)

    def start_checks(self):
        for idx, server in enumerate(self.servers):
            asyncio.create_task(self.check_server(idx, server))

    async def check_server(self, idx, server: MCPServer):
        tools = []
        session = None
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
                stack = AsyncExitStack()
                await stack.__aenter__()
                stdio, write = await stack.enter_async_context(
                    stdio_client(server_params, errlog=stderr_file)
                )
                session = await stack.enter_async_context(ClientSession(stdio, write))
                await session.initialize()
                tool_list = await session.list_tools()
                # Store the full tool objects for invocation
                if hasattr(tool_list, "tools"):
                    tools = tool_list.tools
                else:
                    tools = tool_list
                self.server_tools[idx] = tools
                self.server_sessions[idx] = (stack, session)
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

    def action_filter(self):
        if self.filter_input:
            return
        self.filter_input = Input(placeholder="Regex filter (Enter=apply, Esc=cancel)", value=self.current_regex, id="filter-input")
        self.mount(self.filter_input)
        self.filter_input.focus()
        self.filter_input.border_title = "Filter"

    def on_input_submitted(self, event: Input.Submitted):
        if self.filter_input and event.input is self.filter_input:
            import re
            value = self.filter_input.value
            self.current_regex = value or ""
            if not self.current_regex:
                self.filtered_servers = self.servers
            else:
                try:
                    regex = re.compile(self.current_regex, re.IGNORECASE)
                    self.filtered_servers = [s for s in self.servers if regex.search(s.name + " " + (s.type or "") + " " + (s.command or "") + " " + (s.url or ""))]
                except Exception:
                    self.filtered_servers = self.servers
            self._refresh_table()
            self.filter_input.remove()
            self.filter_input = None

    def on_input_key(self, event: Key):
        if self.filter_input and event.sender is self.filter_input and event.key == "escape":
            self.filter_input.remove()
            self.filter_input = None

    def _refresh_table(self):
        self.table.clear()
        self._row_keys = []
        for server in self.filtered_servers:
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

    def on_data_table_row_selected(self, event):
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
        session_tuple = self.server_sessions.get(idx)
        session = session_tuple[1] if session_tuple else None
        self.app.push_screen(ToolsListScreen(server.name, tools, server, invoke_callback=self.make_invoke_callback(session)))

    def make_invoke_callback(self, session):
        async def invoke_tool(tool, server, values):
            if not session:
                return "No session available for this server."
            tool_name = getattr(tool, "name", str(tool))
            # Always pass a dict if the tool has an input schema
            schema = getattr(tool, "input_schema", None)
            if schema and isinstance(schema, dict):
                arguments = values if isinstance(values, dict) else {}
            else:
                # If no schema, pass as a single value or as dict with a generic key
                arguments = values if isinstance(values, dict) else {"value": values}
            try:
                result = await session.call_tool(tool_name, arguments)
                # Try to extract a string result
                if hasattr(result, "content"):
                    # result.content may be a list of content objects
                    if isinstance(result.content, list):
                        # Try to join text fields
                        texts = []
                        for c in result.content:
                            if hasattr(c, "text"):
                                texts.append(str(c.text))
                            else:
                                texts.append(str(c))
                        return "\n".join(texts)
                    return str(result.content)
                return str(result)
            except Exception as e:
                import traceback
                return f"Error invoking tool: {e}\n{traceback.format_exc()}"
        return invoke_tool

class MCPServerListApp(App):
    CSS_PATH = importlib.resources.files("mcp_tui").joinpath("app.tcss")

    def __init__(self, servers: List[MCPServer], **kwargs):
        super().__init__(**kwargs)
        self.servers = servers
        self.server_logs = {}  # idx -> (stdout, stderr)

    def on_mount(self):
        self.push_screen(ServerListScreen(self.servers, self.server_logs))

@app_cli.command()
def main(
    mcp_json: Path = typer.Argument(None, help="Path to mcp.json file", show_default=False),
    server_cmd: Optional[str] = typer.Option(None, "--server-cmd", help="Command to run as a stdio MCP server"),
    server_args: Optional[List[str]] = typer.Option(None, "--server-args", help="Arguments for the stdio MCP server command"),
    server_name: Optional[str] = typer.Option("Custom MCP Server", "--server-name", help="Name for the MCP server when using --server-cmd"),
) -> None:
    """Open a TUI listing MCP servers from a mcp.json file, or connect to a single stdio MCP server if --server-cmd is given."""
    if server_cmd:
        # User specified a command, connect to it as a stdio MCP server
        server = MCPServer(
            name=server_name or "Custom MCP Server",
            command=server_cmd,
            args=server_args or [],
            type="stdio"
        )
        # Show tools for this server directly (skip server list if only one server)
        class SingleServerApp(App):
            CSS_PATH = importlib.resources.files("mcp_tui").joinpath("app.tcss")
            def __init__(self, server: MCPServer, **kwargs):
                super().__init__(**kwargs)
                self.server = server
                self.server_logs = {0: (None, None)}
            async def on_mount(self):
                # Reuse ServerListScreen logic to connect and get tools
                screen = ServerListScreen([self.server], self.server_logs)
                await screen.check_server(0, self.server)
                tools = screen.server_tools.get(0, [])
                session_tuple = screen.server_sessions.get(0)
                session = session_tuple[1] if session_tuple else None
                self.push_screen(ToolsListScreen(self.server.name, tools, self.server, invoke_callback=screen.make_invoke_callback(session)))
        SingleServerApp(server).run()
        return
    # Default: load from mcp.json
    if not mcp_json:
        typer.echo("Error: You must specify either a path to mcp.json or --server-cmd.")
        raise typer.Exit(1)
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
