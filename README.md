# iphone-safari-mcp

An MCP server that drives **Safari on a physically attached iPhone** through Apple's
`safaridriver` (`platformName: "ios"`). No Xcode, no Appium, no WebDriverAgent.

Gives a coding agent the closed loop: navigate → screenshot + DOM snapshot + console +
network timings → click/type/tap/scroll → repeat, all on the real device.

## Prerequisites

On the iPhone — **Settings → Apps → Safari → Advanced**:
- Web Inspector: ON
- Remote Automation: ON

On the Mac, once:
```bash
safaridriver --enable
```

At session time the phone must be **plugged in, unlocked, and trusted**. Only one
WebDriver session can exist at a time — close any Selenium script or Web Inspector
automation session first.

## Install

Needs [uv](https://docs.astral.sh/uv/). Nothing else — uv fetches Python and the
dependencies itself.

Run it straight from the repo, no clone and no venv:

```bash
uvx --from git+https://github.com/ikatkov/iphone-safari-mcp \
  iphone-safari-mcp --selftest https://example.com
```

Or work on it locally:

```bash
git clone https://github.com/ikatkov/iphone-safari-mcp
cd iphone-safari-mcp
uv sync                       # resolves dependencies into .venv
uv run iphone-safari-mcp --selftest https://example.com
```

The selftest prints the user agent (should say iPhone), writes
`/tmp/iphone-safari-selftest.png`, and reports how many interactive elements it found.
`--help` lists everything.

The examples below use `uv run iphone-safari-mcp` (from a clone); every one of them
works with the `uvx --from git+…` form too.

## How it talks to safaridriver

The server starts **one long-lived `safaridriver`** and reuses it, rather than letting
Selenium spawn a throwaway one per session.

This matters. `safaridriver` needs several seconds after launch to enumerate USB
devices. Selenium's `webdriver.Safari()` spawns a fresh driver and fires New Session
immediately — which lands inside that window, when the attached iPhone is still
invisible and only already-cached network-paired devices exist. The symptom is baffling:
session creation fails naming some *other* household device, and your cabled phone is
absent from the list entirely while sitting right there in Safari → Develop.

So session creation also retries for `IPHONE_SAFARI_DISCOVERY_TIMEOUT` seconds
(default 45) instead of failing on the first miss.

To debug discovery, run your own driver and attach to it:

```bash
safaridriver --diagnose -p 4444          # logs to ~/Library/Logs/com.apple.WebDriver/
IPHONE_SAFARI_DRIVER_URL=http://localhost:4444 uv run iphone-safari-mcp --verify
```

## Pick the right phone

`safaridriver` will use **any** paired host that matches, including devices paired over
the network that you never plugged in. Always pin the device:

```bash
uv run iphone-safari-mcp --list-devices     # what safaridriver can see, and why each was rejected
uv run iphone-safari-mcp --device "My iPhone" --selftest https://example.com
```

For the MCP server, pin it with an env var — `IPHONE_SAFARI_DEVICE_NAME`,
`IPHONE_SAFARI_DEVICE_UDID`, or `IPHONE_SAFARI_DEVICE_TYPE`. UDID is the robust choice
since names change. `session_info` reports both what you pinned and what you got.

A device missing from `--list-devices` entirely is not paired — check the cable, tap
Trust, and confirm it appears under **Safari → Develop** on the Mac. A device that is
listed but rejected tells you the reason ("Web Inspector is not enabled on device").

## Register with Claude Code

**Verify before you register.** Other household devices pair too, and a wrong pin fails
silently — it just drives someone else's phone. `--verify` paints a large random code on
whatever screen it actually controls:

```bash
uv run iphone-safari-mcp --verify --device "My iPhone"
```

Only register once the code appears on the phone you intend to drive:

```bash
claude mcp add iphone-safari --scope user \
  -e IPHONE_SAFARI_DEVICE_UDID=<udid confirmed by --verify> \
  -- uvx --from git+https://github.com/ikatkov/iphone-safari-mcp iphone-safari-mcp
```

That pins nothing to this directory — the agent can launch it from anywhere. uv caches
the environment, so only the first start needs the network.

To run your own checkout instead, point uv at it by absolute path, since the agent
won't launch from this directory:

```bash
claude mcp add iphone-safari --scope user \
  -e IPHONE_SAFARI_DEVICE_UDID=<udid confirmed by --verify> \
  -- uv run --directory /abs/path/to/iphone-safari-mcp iphone-safari-mcp
```

Prefer the UDID over the name: `--verify` prints the UDID that safaridriver actually
bound to, and unlike names it can't drift or collide.

Run this from a **real terminal** with Claude Code closed — it rewrites `~/.claude.json`
on exit and will clobber the edit otherwise.

Then in a session: *"Use iphone-safari. Report what device you're connected to, open
http://localhost:5173, screenshot it and list any console errors."*

## Tools

| Tool | Purpose |
| --- | --- |
| `session_info` | Confirm you're on the iPhone, not Mac Safari — user agent, viewport, DPR |
| `navigate` | Open a URL and arm console capture |
| `screenshot` | Downscaled PNG of the viewport |
| `snapshot` | **Start here.** URL, viewport, scroll, and every visible interactive element with a CSS selector, label and rect |
| `page_source` | Raw HTML, truncated |
| `click` / `type_text` | Act on an element by `css`, `xpath` or `text` |
| `tap` | Real single-finger touch at viewport coordinates |
| `scroll` | Scroll by pixel delta |
| `evaluate_javascript` | Run JS in the page |
| `console_logs` / `install_console_capture` | Console + uncaught errors + rejections |
| `network_timings` | Resource timings; `transferSize: 0` usually means a failed request |
| `go_back` / `reload_page` / `close_session` | Session control |

Prefer `snapshot` over `page_source` — it's a fraction of the tokens and gives you
selectors you can act on directly.

## Limits

Apple's Safari WebDriver is deliberately **web-content automation, not device
automation**. No Home screen, Settings, app switching, rotation, or system dialogs —
Apple calls general device automation a non-goal. If you need those, use
[mobile-next/mobile-mcp](https://github.com/mobile-next/mobile-mcp), which drives the
whole device but requires go-ios + WebDriverAgent provisioned onto the phone.

Safari 27 / STP 247+ ship an MCP server inside `safaridriver` itself
(`safaridriver --mcp`), but as of Safari 26.2 that flag does not exist, and Apple only
documents it against Safari on the Mac.
