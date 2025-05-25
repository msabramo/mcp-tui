# MCP TUI

A Python TUI app using [uv](https://github.com/astral-sh/uv), [Typer](https://typer.tiangolo.com/), and [Textual](https://textual.textualize.io/) to list MCP servers from a `mcp.json` file.

## Installation

Install dependencies using [uv](https://github.com/astral-sh/uv):

```sh
uv pip install -r requirements.txt
```

## Usage

Run the app with:

```sh
python -m mcp_tui.app mcp.json
```

Or, if you want to use the CLI directly:

```sh
python mcp_tui/app.py mcp.json
```

See `mcp.json.example` for the expected format.
