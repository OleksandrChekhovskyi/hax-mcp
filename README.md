# hax-mcp

A small, dependency-free Python CLI for using tools of [MCP](https://modelcontextprotocol.io)
servers from the shell: remote servers over HTTP, and local ones that run as a command. It ships
with a skill so that [hax](https://github.com/OleksandrChekhovskyi/hax) can discover and call
those tools progressively, without loading every tool definition into the model's context.

## Requirements

- Python 3.9 or newer

## Installation

Clone the repository somewhere it will not be moved, then run:

```sh
./install.sh
```

The installer links `hax-mcp` into `~/.local/bin` and the skill into hax's skills directory. It
is safe to rerun and refuses to replace existing paths. Restart hax after installing the skill.

## Configuration

Add a remote server by name and URL, or a local one by the command that runs it:

```sh
hax-mcp add linear https://mcp.linear.app/mcp
hax-mcp add github https://api.githubcopilot.com/mcp/ -H 'Authorization: Bearer ${GITHUB_TOKEN}'
hax-mcp add playwright -- npx @playwright/mcp@latest --headless
hax-mcp remove linear
```

`-H` adds a header to a remote server and `-e NAME=value` an environment variable to a local one.
Put `--` before a command whose arguments start with `-`.

`add` then lists the server's tools once to report whether it works or needs a login; `remove`
also deletes the server's stored login and stops it if it is running. Servers are kept in
`~/.config/hax-mcp/servers.json` (or under `$XDG_CONFIG_HOME`), in the `mcpServers` format most
MCP clients use, so snippets from server documentation can also be pasted in by hand:

```json
{
  "mcpServers": {
    "linear": { "url": "https://mcp.linear.app/mcp" },
    "notion": { "url": "https://mcp.notion.com/mcp" },
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": { "Authorization": "Bearer ${GITHUB_TOKEN}" }
    },
    "playwright": { "command": "npx", "args": ["@playwright/mcp@latest", "--headless"] }
  }
}
```

`${VAR}` in a server's entry is replaced with the environment variable when the server is used,
so tokens can stay out of the file; quote it in the shell so it is stored as written.

## Login

Servers that use OAuth need a one-time login in a terminal:

```sh
hax-mcp login linear
```

It opens the browser to sign in; when the browser runs on another machine, such as over ssh,
paste the URL the browser ends on instead. Tokens are kept readable only by you under
`~/.local/state/hax-mcp` and refreshed automatically; `hax-mcp logout linear` removes them.
Servers without OAuth login, such as GitHub, take an access token in `headers` instead.

## Usage

```sh
hax-mcp servers
hax-mcp tools linear
hax-mcp show linear list_issues
hax-mcp call linear list_issues '{"limit": 5}'
echo '{"limit": 5}' | hax-mcp call linear list_issues -
```

`tools` prints one line per tool with its parameters (`?` marks optional ones) and a one-sentence
summary; `show` prints full descriptions and parameter types. `call` prints the tool's text
output, with JSON pretty-printed, and saves images and binary resources to temporary files. A
tool error goes to stderr with exit status 1. `--json` on `tools`, `show`, and `call` writes the
raw protocol objects for use with `jq`.

hax-mcp speaks MCP 2026-07-28 and falls back to 2025-11-25 for servers that do not support it
yet. A local server that misbehaves when asked for the newer version can be set to
`"protocol": "legacy"` in its entry. Requests that need interactive input from the client
(elicitation, sampling) are reported as errors.

## Local servers

A local server starts on first use and keeps running in the background until stopped, so state
it holds, such as an open browser, carries over from one call to the next. Each project gets its
own instance, running in the project's root directory (the nearest one containing `.git`); set
`"cwd"` in a server's entry to run one shared instance there instead.

```sh
hax-mcp status                       # running servers, their directories, memory, and idle time
hax-mcp stop playwright              # stop every instance of playwright
hax-mcp stop playwright -C ~/proj    # stop only the instance for ~/proj
hax-mcp stop                         # stop everything
```

Stopping a server discards its state. A server that exits on its own, or whose entry changed or
now expands differently (such as a rotated `${TOKEN}`), is started again on its next use, with a
note on stderr.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
