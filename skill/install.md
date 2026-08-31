# ego lite access on this host (remote bridge)

Read this file only if an `ego-browser` command fails with a missing-command or
connection error. For day-to-day browser work, go back to `SKILL.md`.

**This host does not run ego lite itself, and cannot.** ego lite is a macOS app;
its `ego-browser` binary is a macOS executable. The `ego-browser` on this host is
a drop-in shim (`/usr/local/bin/ego-browser`) that forwards each
`ego-browser nodejs` script over the LAN to a bridge service running on the Mac,
executes it there against the real ego lite browser, and streams the output back.

Everything in `SKILL.md` is used exactly as written — same command, same
heredoc, same helpers. The remoting is transparent.

**Do not run `scripts/install.sh`.** It is macOS-only and would try to install
the browser app on this Linux host.

## What is different from a local ego lite

- **Screenshots**: `await captureScreenshot()` returns a path that has already
  been rewritten to a local file under `/var/tmp/ego-bridge/files/`. The PNG is
  downloaded here automatically, so read it with your normal file tools.
- **Downloads**: files the browser saves on the Mac are copied here
  automatically, into `/var/tmp/ego-bridge/downloads/`. When a run produces one,
  the shim prints a line listing what arrived — open those paths with your normal
  file tools. A download that lands too late to be caught is reported at the
  start of the *next* `ego-browser` call, never dropped. So if a download is the
  last thing you do, end the script with `await wait(3)` or run one more small
  round, otherwise you may not see it mentioned.
- **`uploadFile(path)`**: the path must exist *on the Mac*, not here. Push the
  file first:

  ```bash
  curl -s -X POST --data-binary @/local/file.pdf \
    -H "Authorization: Bearer $(sed -n 's/^EGO_BRIDGE_TOKEN=//p' /etc/ego-bridge.conf)" \
    "$(sed -n 's/^EGO_BRIDGE_URL=//p' /etc/ego-bridge.conf)/upload?name=file.pdf"
  ```

  It replies with `{"path": "<mac path>"}` — pass that path to `uploadFile()`.
- **The browser is the user's real Mac browser.** Task spaces keep the agent's
  tabs isolated, but the machine and login state belong to the user.

## Check the link is up

```bash
printf "cliLog('ego bridge ready')\n" | ego-browser nodejs
```

Printing `ego bridge ready` means the whole path is healthy.

## Configuration

`/etc/ego-bridge.conf` (mode 600):

```
EGO_BRIDGE_URL=http://<mac-ip>:8791
EGO_BRIDGE_TOKEN=<shared secret>
```

`EGO_BRIDGE_URL` and `EGO_BRIDGE_TOKEN` in the environment override the file.

## Troubleshooting

- **`cannot reach the ego-bridge`** — the Mac is asleep, on a different IP, or
  the bridge process is not running. Tell the user to wake the Mac and start the
  bridge; do not retry in a loop.
- **HTTP 401** — token in `/etc/ego-bridge.conf` no longer matches the Mac's
  `token` file.
- **HTTP 403 `source ip not allowed`** — this host's IP changed; it must be in
  the bridge's `allow_ips`.
- **HTTP 403 `subcommand not allowed`** — the bridge only permits `nodejs`,
  `help` and `--version`. Profile-import and upgrade subcommands are refused by
  design; they are the user's job on the Mac.
- **`user is controlling`** — unchanged from `SKILL.md`: a hard stop. Ask the
  user, do not take control back on your own.
