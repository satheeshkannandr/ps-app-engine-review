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

## Install the playbook in your AI tool

[SKILL.md](SKILL.md) is the playbook — the metadata map, extraction queries, review checklist and
output format. It is plain Markdown, so use it whichever way suits your tool:

**Claude Code** — put it where skills are discovered, then it auto-invokes (or call it with
`/ps-app-engine-review`):

```
<your-project>/.claude/skills/ps-app-engine-review/SKILL.md
```

**GitHub Copilot (VS Code)** — save it as a reusable prompt file, then run `/ps-app-engine-review`
in Copilot Chat:

```
<your-project>/.github/prompts/ps-app-engine-review.prompt.md
```

To have Copilot apply it to *every* chat in the project instead, put the content in
`.github/copilot-instructions.md`.

**Any other tool** — paste the contents of `SKILL.md` into the chat as context before asking for a
review. Nothing in it depends on a particular product.

> The `---` frontmatter block at the top of `SKILL.md` (`name` / `description`) is Claude Code's
> skill format. Copilot prompt files use `---` frontmatter too but different keys, so replace it
> with `mode: agent` and a `description:` line if you go that route; harmless to leave otherwise.

## How to use

Ask (substitute the program id and the database):

> **Review App Engine `<AE_APPLID>` and explain what it does. Connect to '<DB_NAME>' database**

For example: *Review App Engine `AR_AGING` and explain what it does. Connect to 'TEST' database*

The assistant will (per [SKILL.md](SKILL.md)):

1. Connect to the `<DB_NAME>` you named.
2. Pull the program from the metadata tables:
   - **Structure** — `PSAEAPPLDEFN`, `PSAESECTDEFN`, `PSAESTEPDEFN`, `PSAESTMTDEFN`
   - **SQL action text** — `PSSQLTEXTDEFN` (joined on `SQLID`)
   - **PeopleCode source** — `PSPCMTXT.PCTEXT` (plain text)
   - **Referenced code (by default)** — any **App Package** classes (`import`), **record/FUNCLIB**
     functions (`Declare Function`), and **named SQL** (`SQL.<name>`) the AE calls are also pulled
     from `PSPCMTXT` / `PSSQLTEXTDEFN`, since the program's real behavior usually lives there.
3. Reassemble the program in execution order and return a review:
   **Part 1: Business User Overview (plain-English purpose, business lifecycle role, rules/retention matrix, user interaction & safeguards) → Part 2: Technical Architecture & Risk Review (active steps flow, dependencies, categorized risks, prioritized action plan).**

## Why DB-direct works
PeopleCode is stored as **readable plain text** in `PSPCMTXT.PCTEXT` across PeopleTools
versions (not just compiled bytecode), so the full program — PeopleCode + SQL + flow, **plus the
App Package / FUNCLIB classes it calls** — can be extracted with SQL queries alone, no XML export
needed. See [SKILL.md](SKILL.md) for the exact tables,
key schemes (AE *and* referenced-code), queries, and the fallback if a program returns no source.

## Notes
- The review checklist emphasizes restart safety, join correctness, date/bind handling, and
  performance over large AR/GL tables.
- Extracted source for a given program can optionally be saved under `<AE_APPLID>/`.
