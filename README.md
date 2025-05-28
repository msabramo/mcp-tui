# MCP TUI

A Python TUI app using [uv](https://github.com/astral-sh/uv), [Typer](https://typer.tiangolo.com/), and [Textual](https://textual.textualize.io/) to list MCP servers from a `mcp.json` file.

## Installation

Clone the repository:

```sh
git clone git@github.com:msabramo/mcp-tui.git
cd mcp-tui
```

Install [uv](https://github.com/astral-sh/uv) if you don't have it already.

Install dependencies using [uv](https://github.com/astral-sh/uv):

```sh
uv sync
```

## Usage

Run the app with:

```sh
uv run python mcp_tui/app.py ~/.cursor/mcp.json
```
