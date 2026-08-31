# Adapting the `ego-browser` skill for the agent host

The skill itself ships with ego lite and belongs to Citro Labs — **this repo does
not redistribute it**. Copy it from your Mac, then apply the two changes below so
the agent understands it is driving a remote browser.

## 1. Copy the skill from the Mac

The Mac keeps it at `~/.local/share/ego/ego-skills` (a symlink into the app
bundle). Copy the whole tree to wherever your agent discovers skills:

| Agent | Skill directory |
|---|---|
| pi | `~/.pi/agent/skills/ego-browser/` |
| Claude Code | `~/.claude/skills/ego-browser/` |
| Any Agent Skills runtime | `~/.agents/skills/ego-browser/` |

```bash
# on the Mac
tar czf /tmp/ego-skill.tgz -C ~/.local/share/ego/ego-skills .
scp /tmp/ego-skill.tgz agent-host:/tmp/

# on the agent host
mkdir -p ~/.pi/agent/skills/ego-browser
tar xzf /tmp/ego-skill.tgz -C ~/.pi/agent/skills/ego-browser
find ~/.pi/agent/skills/ego-browser -name '._*' -delete   # macOS tar metadata
```

## 2. Replace `references/install.md`

The stock file explains how to install the macOS app, which is wrong and
actively misleading on a Linux host — the skill tells the agent to read it
whenever a command fails, and it would send the agent chasing a `.dmg`.

```bash
cp skill/install.md ~/.pi/agent/skills/ego-browser/references/install.md
rm -rf ~/.pi/agent/skills/ego-browser/scripts   # macOS-only installer
```

## 3. Add a host note to `SKILL.md`

Insert this block right after the `# ego-browser` heading, so the agent knows
about the remote hop before it reads anything else:

```markdown
> **Host note — this machine drives ego lite over a remote bridge.** The
> `ego-browser` command here is a shim that forwards every script to the ego lite
> browser running on the macOS host on the LAN. Usage below is unchanged: same
> `ego-browser nodejs <<'EOF'` heredoc, same helpers, same task spaces.
> `captureScreenshot()` paths are rewritten to local files under
> `/var/tmp/ego-bridge/files/`, so you can read them directly. If a command fails
> to connect, read `references/install.md`; never run `scripts/install.sh` (macOS
> only). The browser belongs to a real user — respect the handoff rules below.
```

Everything else in `SKILL.md` stays exactly as shipped.
