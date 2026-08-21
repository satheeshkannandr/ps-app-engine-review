## PeopleSoft Application Engine Review

A workspace for reviewing **PeopleSoft Application Engine (AE)** programs by reading them
directly out of the PeopleTools metadata tables — no App Designer XML export required.

**Connects to whatever database you name** (saved SQLcl connections; e.g. `TEST`).
Reference instance `TEST` = PeopleSoft FSCM 9.2 / PeopleTools 8.58 · Oracle 19c;
other instances may differ and the live connection reports the actual context.

## Pre-requisites

### MCP SQLcl — Database Access

The skill uses **MCP SQLcl** to run live queries against the PeopleSoft database. Without it the skill's DB-direct extraction can't run, so there's no live program investigation — only a manual App Designer print listing / project XML export would remain as a last resort.

#### Install SQLcl

Download SQLcl from [Oracle's website](https://www.oracle.com/database/sqldeveloper/technologies/sqlcl/).  
Unzip to a permanent location (e.g. `C:\tools\sqlcl`).  
Add `C:\tools\sqlcl\bin` to your system `PATH`.

Verify:
```powershell
sql -v
```

#### Configure the SQLcl MCP server

MCP (Model Context Protocol) lets the AI tool call SQLcl as a tool. Configure it for whichever
tool you drive the review from. **The two configs are independent** — Claude Code does **not** read
VS Code's `.vscode/mcp.json`, and vice-versa.

**Claude Code** (what this workspace targets) — register the server once at **user scope** so it's
available in every project (CLI, IDE extension, and the desktop **Code** tab):

```powershell
claude mcp add --scope user sqlcl -- sql -mcp
```

This writes a `mcpServers.sqlcl` entry to your user config (`~/.claude.json`). Verify with:

```powershell
claude mcp list
```

Alternatively, to commit the server **with the repo** so colleagues get it automatically, add a
project-root `.mcp.json` (Claude Code auto-loads it and prompts for approval on first use):

```json
{
  "mcpServers": {
    "sqlcl": {
      "command": "sql",
      "args": ["-mcp"]
    }
  }
}
```

**VS Code native MCP (GitHub Copilot / VS Code agent mode)** — only needed if you also want
VS Code's *own* agent to use SQLcl. Add to your VS Code `settings.json` (or the workspace
`.vscode/mcp.json`), then restart VS Code:

```json
{
  "mcp": {
    "servers": {
      "sqlcl": {
        "command": "sql",
        "args": ["-mcp"],
        "type": "stdio"
      }
    }
  }
}
```

#### Save Named Database Connections

The skill connects by **connection name** (e.g. `TEST`, `DEV`). Save your connections once.
Start SQLcl with no connection:

```powershell
sql /nolog
```

Then, at the `SQL>` prompt, save the connection (`-save` / `-savepwd` are flags of the `connect`
command, not the `sql` launcher):

```sql
connect -save TEST -savepwd <username>/<password>@<connect_string>
```

Repeat for each environment you need to review (DEV, TEST, UAT, PROD).  
Saved connections are stored in SQLcl's connection store under the DBTools home directory —
on **Windows** that's `%APPDATA%\DBTools\connections` (e.g.
`C:\Users\<you>\AppData\Roaming\DBTools\connections`); on macOS/Linux it's `~/.dbtools`.

> **Security note:** Saved passwords are stored in an encrypted wallet. Do **not** hardcode credentials in scripts or skill files.

---

## How to use

Open this folder in your AI assistant (Antigravity, Claude Code, GitHub Copilot) and ask:

> **Review App Engine `<AE_APPLID>` and explain what it does. Connect to '<DB_NAME>' database**

For example: *Review App Engine `AR_AGING` and explain what it does. Connect to 'TEST' database*

This runs the **`ps-ae-overview`** skill ([.agents/skills/ps-ae-overview/SKILL.md](.agents/skills/ps-ae-overview/SKILL.md)), which can also be invoked directly with `/ps-ae-overview`.

### Dual-Mode Execution

1. **Mode 1: Autonomous Agent Run**  
   The AI assistant runs the bundled Python extraction script (`scripts/ae_extractor.py`) via SQLcl in seconds, pulls all metadata and dependencies into `output/<AE_APPLID>_review_package.md`, and produces the review.

2. **Mode 2: Pre-Extracted Review (Manual CLI Run)**  
   You can run the extractor directly in PowerShell:
   ```powershell
   python .agents/skills/ps-ae-overview/scripts/ae_extractor.py --ae AR_AGING --database TEST --output-dir output
   ```
   Then prompt the AI: *"Review the package for AR_AGING"*. The assistant reads the existing package and returns the review immediately without making DB round-trips.

### Output Structure

The review is returned in a clean, two-part structure:
- **Part 1: Business User Overview** — Plain-English purpose, business lifecycle placement, core business rules & qualification matrix, user interaction, and operational safeguards.
- **Part 2: Technical Architecture & Risk Review** — Active steps execution flow, technical dependencies, categorized risk findings (Restart/Reliability, Correctness, Performance, Code Quality), and prioritized action plan.

## Why DB-direct works
PeopleCode is stored as **readable plain text** in `PSPCMTXT.PCTEXT` across PeopleTools
versions (not just compiled bytecode), so the full program — PeopleCode + SQL + flow, **plus the
App Package / FUNCLIB classes it calls** — can be extracted with SQL queries alone, no XML export
needed. See the [`ps-ae-overview` skill](.agents/skills/ps-ae-overview/SKILL.md) for the exact tables,
key schemes (AE *and* referenced-code), and review checklist.

## Notes
- The review checklist emphasizes restart safety, join correctness, date/bind handling, Component Interface abends, and
  performance over large AR/GL tables.
- Extracted source for a given program can optionally be saved under `<AE_APPLID>/`.
