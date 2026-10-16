# hax-mcp

A small, dependency-free Python CLI for using tools of remote
[MCP](https://modelcontextprotocol.io) servers from the shell. It ships with a skill so that
[hax](https://github.com/OleksandrChekhovskyi/hax) can discover and call those tools
progressively, without loading every tool definition into the model's context.

## Requirements

- Python 3.9 or newer

## Configuration

Add a server by name and URL:

```sh
hax-mcp add linear https://mcp.linear.app/mcp
hax-mcp add github https://api.githubcopilot.com/mcp/ -H 'Authorization: Bearer ${GITHUB_TOKEN}'
hax-mcp remove linear
```

`add` then lists the server's tools once to report whether it works or needs a login; `remove`
also deletes the server's stored login. Servers are kept in `~/.config/hax-mcp/servers.json` (or
under `$XDG_CONFIG_HOME`), in the `mcpServers` format most MCP clients use, so snippets from
server documentation can also be pasted in by hand:

```json
{
  "mcpServers": {
    "linear": { "url": "https://mcp.linear.app/mcp" },
    "notion": { "url": "https://mcp.notion.com/mcp" },
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": { "Authorization": "Bearer ${GITHUB_TOKEN}" }
    }
  }
}
```

`${VAR}` in a URL or header value is replaced with the environment variable when the server is
used, so tokens can stay out of the file; quote it in the shell so it is stored as written. Only
remote (Streamable HTTP) servers are supported; local stdio servers are not.

## Login

Servers that use OAuth need a one-time login in a terminal:

```sh
hax-mcp login linear
```

hax-mcp identifies itself with its client metadata document at
<https://usehax.dev/hax-mcp.json> when the authorization server supports that, and otherwise
registers a client dynamically. It opens the browser and receives the redirect on a loopback
port: 28471, or 28472 when that is taken, for metadata-document logins. When the browser runs on
another machine, such as over ssh, paste the URL the browser ends on instead. Tokens are stored
with mode 0600 in `~/.local/state/hax-mcp/credentials.json` and refreshed automatically;
`hax-mcp logout linear` removes them. Servers that support neither, such as GitHub, take an
access token in `headers` instead.

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

hax-mcp speaks MCP 2026-07-28 and falls back to the 2025-11-25 session handshake for servers that
do not support it yet. Requests that need interactive input from the client (elicitation,
sampling) are reported as errors.

## Source installation

Clone the repository somewhere it will not be moved, then run:

```sh
./install.sh
```

The installer links `hax-mcp` into `~/.local/bin` and the skill into hax's XDG configuration
directory. It is safe to rerun and refuses to replace existing paths. Restart hax after installing
the skill.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
