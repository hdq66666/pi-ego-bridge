# pi-ego-bridge

Let an AI agent on **another machine** drive the [ego lite](https://github.com/citrolabs/ego-lite)
browser on your Mac — without changing a single line of the `ego-browser` skill.

The agent still writes exactly what the skill tells it to write:

```bash
ego-browser nodejs <<'EOF'
const task = await useOrCreateTaskSpace('check the release page')
await openOrReuseTab('https://example.com', { wait: true })
cliLog(await snapshotText())
EOF
```

It just happens to run on a Linux VM, and the browser is on your Mac.

## Why this is needed

ego lite is a macOS app. `ego-browser` is a macOS arm64 binary that reaches the
browser over local IPC — there is no network API to point a remote agent at. So
an agent living anywhere else (a VM, a container, a homelab box, another laptop)
cannot use the skill at all.

`pi-ego-bridge` closes that gap with a same-named shim: `ego-browser` on the
agent host is a small Python program that forwards each script to a bridge
service on the Mac, runs it there against the real browser, and streams the
results back. Built for [pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent),
but nothing in it is pi-specific — it works with any agent that runs the skill
through a shell.

## How it works

```
 agent host (e.g. Linux VM)                    Mac running ego lite
┌────────────────────────────┐               ┌──────────────────────────────┐
│ pi / Claude Code / …       │               │                              │
│   │                        │               │                              │
│   │ bash: ego-browser      │               │                              │
│   │       nodejs <<'EOF'   │               │                              │
│   ▼                        │               │                              │
│ /usr/local/bin/ego-browser │  POST /run    │  ego-bridge.py  :8791        │
│  (shim, same name)         │──────────────►│   ① source-IP allowlist      │
│                            │ {args,stdin}  │   ② bearer token             │
│                            │               │   ③ subcommand allowlist     │
│                            │               │          │                   │
│                            │               │          ▼                   │
│                            │               │  subprocess: real            │
│                            │               │  ego-browser nodejs          │
│                            │               │          │  (stdin = script) │
│                            │◄──────────────│          ▼                   │
│                            │ {code,stdout, │  ego lite task space         │
│                            │  stderr,      │  (reuses your login state)   │
│                            │  artifacts}   │                              │
│   ├─ GET /file  ───────────┼──────────────►│  screenshots                 │
│   │  → /var/tmp/ego-bridge/files/          │                              │
│   ├─ rewrite Mac paths → local paths       │                              │
│   └─ stdout→stdout, stderr→stderr, exit code                              │
└────────────────────────────┘               └──────────────────────────────┘
```

Four details that make it transparent rather than merely functional:

1. **`cliLog` writes to stderr, not stdout.** Running the CLI in a terminal
   merges the two, so it is easy to miss. The shim keeps the streams separate and
   preserves the exit code, so the agent sees byte-identical output.
2. **Screenshots follow the agent.** `captureScreenshot()` returns a path on the
   *Mac*. The bridge scans the output for absolute paths that exist inside an
   allowlisted root, reports them as `artifacts`, and the shim downloads each one
   to `/var/tmp/ego-bridge/files/` and rewrites the path in the output. The agent
   reads the PNG with its normal file tools and never learns there was a hop.
3. **Downloads follow the agent too.** Anything the browser saves on the Mac
   during a run is copied to `/var/tmp/ego-bridge/downloads/` on the agent host,
   and the shim prints what arrived. The click that starts a download returns
   long before Chromium finishes writing the file — measured at ~3s for a small
   one — so the bridge waits a short, interruptible grace period, then waits out
   any `.crdownload` so half a file is never reported as whole. A download that
   still lands too late is picked up by the next call: the bridge keeps a
   watermark, so files are delayed, never dropped.
4. **Uploads go the other way.** `uploadFile()` needs a path on the Mac, so the
   bridge accepts `POST /upload` and answers with the Mac-side path to pass in.

## Requirements

**Mac** — ego lite installed and onboarded once (`ego-browser` on `PATH`),
Python 3.8+ (the system `python3` from Command Line Tools is fine; no pip
packages, standard library only).

**Agent host** — Python 3.6+, network reach to the Mac.

Both on a **trusted LAN**. Read [Security](#security) before you start.

## Install — Mac side

```bash
git clone https://github.com/hdq66666/pi-ego-bridge.git ~/pi-ego-bridge
cd ~/pi-ego-bridge/mac
mkdir -p uploads logs

# shared secret
python3 -c "import secrets;print(secrets.token_urlsafe(32))" > token
chmod 600 token

# allow your agent host in
cp config.example.json config.json
$EDITOR config.json          # set allow_ips to your agent host's IP

python3 ego-bridge.py        # foreground; see below for LaunchAgent
```

`token`, `config.json`, `uploads/` and `logs/` are gitignored — they are
per-host state, not source.

To keep it running across logins, install the LaunchAgent template:

```bash
sed -e "s|__PYTHON__|$(command -v python3)|" \
    -e "s|__SCRIPT__|$HOME/pi-ego-bridge/mac/ego-bridge.py|" \
    -e "s|__DIR__|$HOME/pi-ego-bridge/mac|" \
    io.github.hdq66666.ego-bridge.plist.example \
    > ~/Library/LaunchAgents/io.github.hdq66666.ego-bridge.plist
launchctl load ~/Library/LaunchAgents/io.github.hdq66666.ego-bridge.plist
```

It has to be a LaunchAgent, not a LaunchDaemon — `ego-browser` needs the user's
GUI session, where ego lite is running.

## Install — agent host side

```bash
sudo install -m 755 vm/ego-browser /usr/local/bin/ego-browser

sudo tee /etc/ego-bridge.conf >/dev/null <<'EOF'
EGO_BRIDGE_URL=http://<mac-ip>:8791
EGO_BRIDGE_TOKEN=<contents of the Mac's token file>
EOF
sudo chmod 600 /etc/ego-bridge.conf
```

Then adapt the skill for this host — see [`skill/README.md`](skill/README.md).
It is two small edits: swap `references/install.md` for the bridge-aware
[`skill/install.md`](skill/install.md), and add a host note to `SKILL.md`.

## Verify

```bash
printf "cliLog('ego bridge ready')\n" | ego-browser nodejs
```

Then the full path, browser included:

```bash
ego-browser nodejs <<'EOF'
const task = await useOrCreateTaskSpace('bridge smoke test')
await openOrReuseTab('https://example.com', { wait: true, timeout: 25 })
const info = await pageInfo()
cliLog('url: ' + info.url + ' | title: ' + info.title)
cliLog('screenshot: ' + await captureScreenshot())
EOF
```

The printed screenshot path should be local (`/var/tmp/ego-bridge/files/…`) and
the file should already be there.

## HTTP API

Every endpoint requires `Authorization: Bearer <token>` **and** a source IP in
`allow_ips`.

| Method | Path | Body / query | Returns |
|---|---|---|---|
| `GET` | `/health` | — | `{"ok": true, "ego_browser": "<path>"}` |
| `POST` | `/run` | `{"args": ["nodejs"], "stdin": "<script>", "timeout": 600}` | `{"code", "stdout", "stderr", "artifacts": [...], "downloads": [...]}` |
| `GET` | `/file` | `?path=<mac path>` | raw bytes; only inside allowlisted roots |
| `POST` | `/upload` | raw body, `?name=<filename>` | `{"path": "<mac path>"}` |

## Configuration

**Mac — `mac/config.json`** (all keys optional; see `config.example.json`)

| Key | Default | Meaning |
|---|---|---|
| `host` | `0.0.0.0` | Bind address |
| `port` | `8791` | Bind port |
| `allow_ips` | `["127.0.0.1", "::1"]` | Source IPs/CIDRs allowed in |
| `ego_browser` | `~/.local/bin/ego-browser` | Path to the real binary |
| `timeout` | `300` | Seconds per run |
| `max_parallel` | `4` | Concurrent runs |
| `allowed_subcommands` | `nodejs`, `help`, `--help`, `-h`, `--version`, `-v` | Everything else is refused |
| `watch_dirs` | `["~/Downloads"]` | Where browser downloads are picked up |
| `download_grace` | `4` | Seconds to wait for a download to appear (see below) |
| `download_settle` | `20` | Extra seconds to wait out a `.crdownload` |
| `max_download_bytes` | `104857600` | Larger files are reported, not shipped |

**About `download_grace`.** It is the one knob with a real cost: a call that
downloads nothing still waits this long. Measured on a LAN, an `ego-browser`
round trip that does trivial work takes ~270 ms; with the default 4 s grace it
takes ~4.3 s. The wait is interruptible, so a call that *does* download exits as
soon as the file lands. Set it to `0` for zero overhead — downloads then arrive
with the *next* call instead of the current one, which is usually fine because
agents rarely stop right after a download.

**Agent host — `/etc/ego-bridge.conf`**, overridable by the environment:
`EGO_BRIDGE_URL`, `EGO_BRIDGE_TOKEN`, plus `EGO_BRIDGE_CONF`,
`EGO_BRIDGE_FILE_DIR`, `EGO_BRIDGE_RUN_TIMEOUT`, `EGO_BRIDGE_HTTP_TIMEOUT`.

## Security

**Read this part.** A request that reaches `/run` executes arbitrary JavaScript
in ego lite's Node runtime on your Mac, and that runtime has `require`. Whoever
holds the token can drive your logged-in browser and read and write files as
your user. This is a remote-execution service by design; the browser automation
*is* the remote execution.

What the bridge does about it:

- **Bearer token**, 256-bit, compared in constant time.
- **Source-IP allowlist**, loopback-only until you add a host.
- **Subcommand allowlist** — `nodejs`, `help`, `--version`. `import` and
  `upgrade` are refused: touching browser profiles or upgrading the app is the
  Mac user's job, not the agent's.
- **File reads are rooted** — `/file` only serves paths under `TMPDIR`, `/tmp`,
  `~/Downloads` and `uploads/`, checked after `realpath`, so `..` gets you
  nowhere.

What it does **not** do:

- **No TLS.** Traffic is plaintext HTTP, token included. Fine inside a LAN you
  control; put it behind a tunnel (WireGuard, Tailscale, `ssh -L`) if it ever
  crosses anything less trusted.
- **No sandbox** around the executed script. The agent's own guardrails are the
  only limit on what it does in your browser.
- **No separation between your downloads and the agent's.** `watch_dirs`
  defaults to `~/Downloads`, which is where *your* browsing saves files too, and
  ego lite offers no way to give the agent its own download folder. Anything
  landing there while the agent works is copied to the agent host — including a
  file you downloaded yourself. Only files touched after the bridge starts are
  considered, so your existing Downloads folder is never swept up. If that is
  not tight enough, change ego lite's download folder in the browser's own
  settings and point `watch_dirs` at it.

Do not expose port 8791 to the internet, and do not port-forward it.

## Limitations

- The Mac must be **awake and logged in**, with ego lite running.
- Bare IPs break on DHCP lease changes; a `.local` mDNS name in
  `EGO_BRIDGE_URL` is steadier.
- `uploadFile()` needs the two-step upload described above — it is documented in
  `skill/install.md` so the agent finds it on its own.
- A screenshot path rewritten for the agent host is **not** valid back on the
  Mac, so it cannot be handed straight to `uploadFile()`; push it with
  `/upload` first.
- One bridge serves one Mac user session. Multiple agents can share it (see
  `max_parallel`); ego lite's task spaces keep their tabs apart.

## 中文说明

ego lite 是 macOS 应用，`ego-browser` 是 arm64 原生二进制、只能通过本机 IPC 和浏览器
通信，所以跑在 Linux 虚拟机上的 agent 完全用不了这个 skill。

本项目在 agent 主机上放一个**同名 shim**：agent 照常写 `ego-browser nodejs <<'EOF'`，
脚本被转发到 Mac 上的桥接服务执行，stdout/stderr/退出码原样返回。四个关键点：
`cliLog` 走 stderr 所以两条流必须分开；截图返回的是 Mac 本地路径，桥会把文件回传到
`/var/tmp/ego-bridge/files/` 并改写路径，agent 直接就能读；浏览器下载的文件同样会同步到
`/var/tmp/ego-bridge/downloads/`（点击返回时文件往往还没落盘，实测慢约 3 秒，所以桥会
等一个可中断的宽限期，再等 `.crdownload` 写完；万一还是没赶上，watermark 保证它在下一次
调用时被捞回来，只会延迟不会丢）；`uploadFile()` 需要 Mac 上的路径，所以提供了反向的
`POST /upload`。

注意 `download_grace` 有代价：没有下载的调用也要等满这个时间（默认 4 秒，实测空跑
270ms → 4.3s）。设成 `0` 可以零开销，代价是下载改为在下一次调用时到达。另外下载目录默认
就是 `~/Downloads`，和你自己的下载混在一起——agent 工作期间你自己下的文件也会被同步过去，
介意的话在 ego lite 设置里改下载目录，再把 `watch_dirs` 指过去。

安全上用 bearer token + 源 IP 白名单 + 子命令白名单防护，但请认清本质：拿到 token
就等于能在你的 Mac 上执行任意 JS。**只在可信局域网内使用，不要做端口映射。**

## License

MIT — see [LICENSE](LICENSE).

`ego lite` and the `ego-browser` skill are products of
[Citro Labs](https://github.com/citrolabs/ego-lite) and are not included in or
covered by this repository.
