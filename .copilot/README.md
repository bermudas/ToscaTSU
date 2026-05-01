# GitHub Copilot CLI MCP config

GitHub Copilot CLI reads MCP server configuration from a **user-level** file
at `~/.copilot/mcp-config.json` — **not** from this directory. The file in this
folder is a project-level reference template.

## Use it

```bash
mkdir -p ~/.copilot
cp .copilot/mcp-config.json ~/.copilot/mcp-config.json
# (or merge into your existing ~/.copilot/mcp-config.json by hand)
```

After copying, restart Copilot CLI. Verify the playwright server is registered:

```bash
copilot mcp list
```

## Why a project-level template?

So newcomers cloning this repo can see exactly which MCP server it expects and
copy a known-good config into their home directory in one command, instead of
hunting through docs to figure out the right shape. Other tools (Claude Code,
VS Code, GitHub Copilot in the IDE) read project-level configs directly — see
`.mcp.json`, `.vscode/mcp.json`, `.github/mcp.json` at repo root.

## Reference

GitHub Copilot CLI MCP docs:
https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers
