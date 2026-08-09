"""MCP server that drives Safari on a physically attached iPhone via safaridriver.

Requirements on the phone: Settings -> Apps -> Safari -> Advanced ->
Web Inspector: ON, Remote Automation: ON. Phone plugged in, trusted, unlocked.
On the Mac, once: `safaridriver --enable`.

Run standalone to sanity-check the connection:
    uv run iphone-safari-mcp --selftest https://example.com
"""

from __future__ import annotations

import atexit
import io
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from PIL import Image as PILImage

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.utilities.types import Image
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Image

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.actions import interaction
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.common.by import By
from selenium.webdriver.safari.options import Options

mcp = _Server("iphone-safari")

_lock = threading.Lock()
_driver: webdriver.Safari | None = None

# Pin which phone to drive. Without this, safaridriver picks any matching paired
# host — including devices paired over the network that you never plugged in.
# Names and UDIDs are matched case-insensitively (see `man safaridriver`).
_DEVICE = {
    "safari:deviceName": os.environ.get("IPHONE_SAFARI_DEVICE_NAME"),
    "safari:deviceUDID": os.environ.get("IPHONE_SAFARI_DEVICE_UDID"),
    "safari:deviceType": os.environ.get("IPHONE_SAFARI_DEVICE_TYPE"),
}

# Optional: attach to an externally started safaridriver instead of launching one.
# Lets you run it under `--diagnose` to see why a device is or isn't discovered.
_DRIVER_URL = os.environ.get("IPHONE_SAFARI_DRIVER_URL")

_SAFARIDRIVER = "/usr/bin/safaridriver"
# How long to keep retrying New Session while safaridriver enumerates USB devices.
_DISCOVERY_TIMEOUT = float(os.environ.get("IPHONE_SAFARI_DISCOVERY_TIMEOUT", "45"))

_driver_proc: subprocess.Popen | None = None
_driver_url: str | None = None

# Patches console/onerror into a ring buffer we can read back later. Must be
# re-injected after every navigation, since the page context is torn down.
_CONSOLE_HOOK = """
return (function () {
  if (window.__mcpLogs) return 'already-installed';
  window.__mcpLogs = [];
  var push = function (level, args) {
    try {
      window.__mcpLogs.push({
        level: level,
        text: Array.prototype.map.call(args, function (a) {
          if (a instanceof Error) return a.stack || String(a);
          if (typeof a === 'object') { try { return JSON.stringify(a); } catch (e) { return String(a); } }
          return String(a);
        }).join(' ')
      });
      if (window.__mcpLogs.length > 500) window.__mcpLogs.shift();
    } catch (e) {}
  };
  ['log', 'info', 'warn', 'error', 'debug'].forEach(function (level) {
    var orig = console[level];
    console[level] = function () { push(level, arguments); return orig.apply(console, arguments); };
  });
  window.addEventListener('error', function (e) {
    push('uncaught', [e.message + ' @ ' + e.filename + ':' + e.lineno]);
  });
  window.addEventListener('unhandledrejection', function (e) {
    push('unhandledrejection', [String(e.reason)]);
  });
  return 'installed';
})();
"""

# Builds a compact list of interactive elements with stable CSS selectors.
# Far cheaper for a model to reason over than full page_source.
_SNAPSHOT_JS = """
return (function () {
  function cssPath(el) {
    if (el.id && document.querySelectorAll(CSS.escape ? '#' + CSS.escape(el.id) : '#' + el.id).length === 1) {
      return '#' + (CSS.escape ? CSS.escape(el.id) : el.id);
    }
    var parts = [];
    while (el && el.nodeType === 1 && parts.length < 8) {
      var part = el.tagName.toLowerCase();
      var parent = el.parentElement;
      if (!parent) { parts.unshift(part); break; }
      var same = Array.prototype.filter.call(parent.children, function (c) { return c.tagName === el.tagName; });
      if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
      parts.unshift(part);
      if (el.id) { parts.unshift('#' + (CSS.escape ? CSS.escape(el.id) : el.id)); break; }
      el = parent;
    }
    return parts.join(' > ');
  }

  var sel = 'a,button,input,select,textarea,summary,[role=button],[role=link],[role=tab],' +
            '[role=checkbox],[role=switch],[onclick],[contenteditable=true],[tabindex]:not([tabindex="-1"])';
  var out = [];
  var nodes = document.querySelectorAll(sel);
  for (var i = 0; i < nodes.length && out.length < 200; i++) {
    var el = nodes[i];
    var r = el.getBoundingClientRect();
    var style = window.getComputedStyle(el);
    var onscreen = r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    if (!onscreen) continue;
    var label = (el.getAttribute('aria-label') || el.value || el.placeholder ||
                 el.innerText || el.getAttribute('title') || '').trim().replace(/\\s+/g, ' ');
    out.push({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || undefined,
      role: el.getAttribute('role') || undefined,
      name: el.getAttribute('name') || undefined,
      text: label.slice(0, 120),
      disabled: el.disabled || undefined,
      checked: el.type === 'checkbox' || el.type === 'radio' ? el.checked : undefined,
      css: cssPath(el),
      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      inViewport: r.bottom > 0 && r.top < window.innerHeight
    });
  }
  return {
    url: location.href,
    title: document.title,
    scrollY: Math.round(window.scrollY),
    pageHeight: Math.round(document.documentElement.scrollHeight),
    viewport: { w: window.innerWidth, h: window.innerHeight },
    elements: out
  };
})();
"""


def _ensure_driver() -> str:
    """URL of a running safaridriver, starting a long-lived one if needed.

    Deliberately NOT webdriver.Safari(), which spawns a throwaway safaridriver per
    session and fires New Session immediately. safaridriver needs several seconds to
    enumerate USB devices; until it has, an attached iPhone is invisible and only
    already-cached network-paired devices show up. One driver, reused, stays warm.
    """
    global _driver_proc, _driver_url
    if _DRIVER_URL:
        return _DRIVER_URL
    if _driver_proc is not None and _driver_proc.poll() is None:
        return _driver_url

    with socket.socket() as probe:  # let the OS pick a free port
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    _driver_proc = subprocess.Popen(
        [_SAFARIDRIVER, "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _driver_url = f"http://localhost:{port}"

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _driver_proc.poll() is not None:
            raise RuntimeError(
                f"{_SAFARIDRIVER} exited immediately (status {_driver_proc.returncode}). "
                "Run `safaridriver --enable` once, and check this process is not sandboxed."
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return _driver_url
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"{_SAFARIDRIVER} did not start listening on port {port} within 15s")


def _get_driver() -> webdriver.Safari:
    """Return the live iOS session, creating it on first use."""
    global _driver
    with _lock:
        if _driver is not None:
            try:
                _driver.current_url  # cheap liveness probe
                return _driver
            except WebDriverException:
                try:
                    _driver.quit()
                except Exception:
                    pass
                _driver = None

        options = Options()
        # The capability that makes safaridriver target the attached iPhone
        # instead of Safari on this Mac.
        options.set_capability("platformName", "ios")
        for cap, value in _DEVICE.items():
            if value:
                options.set_capability(cap, value)

        url = _ensure_driver()
        deadline = time.monotonic() + _DISCOVERY_TIMEOUT
        last: WebDriverException | None = None
        while True:
            try:
                _driver = webdriver.Remote(command_executor=url, options=options)
                break
            except WebDriverException as exc:
                last = exc
                # USB enumeration takes a few seconds after safaridriver starts. Until
                # it finishes, the phone is simply absent from the candidate list and
                # session creation fails — so keep asking until the deadline.
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Could not start an iOS Safari session after "
                        f"{_DISCOVERY_TIMEOUT:.0f}s: {last.msg or last}\n"
                        "Checklist:\n"
                        "  - iPhone plugged in, UNLOCKED (screen on) and trusted\n"
                        "  - Settings > Apps > Safari > Advanced > Web Inspector + "
                        "Remote Automation both ON\n"
                        "  - `safaridriver --enable` run once on this Mac\n"
                        "  - wrong phone in the list? pin yours with IPHONE_SAFARI_DEVICE_UDID "
                        "(see `--list-devices`, confirm with `--verify`)\n"
                        "  - stale session: `pkill -f safaridriver`, close any Web Inspector "
                        "window, force-quit Safari on the phone, then retry"
                    ) from last
                time.sleep(1.5)

        _driver.set_page_load_timeout(60)
        return _driver


def _install_console_hook(driver: webdriver.Safari) -> None:
    try:
        driver.execute_script(_CONSOLE_HOOK)
    except WebDriverException:
        pass  # e.g. about:blank or a cross-origin error page


def _find(driver: webdriver.Safari, selector: str, by: str):
    strategies = {
        "css": (By.CSS_SELECTOR, selector),
        "xpath": (By.XPATH, selector),
        "text": (By.XPATH, f"//*[not(self::script)][contains(normalize-space(.), {_xpath_lit(selector)})][not(.//*[contains(normalize-space(.), {_xpath_lit(selector)})])]"),
    }
    if by not in strategies:
        raise ValueError(f"by must be one of {sorted(strategies)}, got {by!r}")
    how, what = strategies[by]
    els = driver.find_elements(how, what)
    if not els:
        raise RuntimeError(f"No element matched {by}={selector!r}. Call snapshot() to see what is on the page.")
    return els[0]


def _xpath_lit(value: str) -> str:
    """Quote a string for XPath, handling embedded quotes."""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


def _shrink(png: bytes, max_width: int = 800) -> bytes:
    """iPhone screenshots come back at device scale; downscale to keep tokens sane."""
    img = PILImage.open(io.BytesIO(png))
    if img.width > max_width:
        height = round(img.height * max_width / img.width)
        img = img.resize((max_width, height), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool()
def session_info() -> dict:
    """Report what device/browser this session is actually attached to.

    Use this first to confirm you are driving the real iPhone and not Mac Safari.
    """
    driver = _get_driver()
    info = driver.execute_script(
        "return {userAgent: navigator.userAgent, platform: navigator.platform, "
        "width: window.innerWidth, height: window.innerHeight, dpr: window.devicePixelRatio, "
        "touchPoints: navigator.maxTouchPoints};"
    )
    return {
        "url": driver.current_url,
        "title": driver.title,
        "capabilities": {
            k: driver.capabilities.get(k)
            for k in ("browserName", "browserVersion", "platformName", "safari:deviceName", "safari:deviceUDID")
        },
        "pinned_to": {k.split(":")[-1]: v for k, v in _DEVICE.items() if v} or "nothing (any matching host)",
        **info,
    }


@mcp.tool()
def navigate(url: str) -> dict:
    """Open a URL in Safari on the iPhone and wait for load."""
    driver = _get_driver()
    driver.get(url)
    _install_console_hook(driver)
    return {"url": driver.current_url, "title": driver.title}


@mcp.tool()
def screenshot(max_width: int = 800) -> Image:
    """Screenshot the current iPhone Safari viewport."""
    driver = _get_driver()
    return Image(data=_shrink(driver.get_screenshot_as_png(), max_width), format="png")


@mcp.tool()
def snapshot() -> dict:
    """Compact structured view of the page: URL, viewport, scroll position, and every
    visible interactive element with a CSS selector, label and on-screen rect.

    Prefer this over page_source() — it is the cheap way to decide what to click.
    """
    return _get_driver().execute_script(_SNAPSHOT_JS)


@mcp.tool()
def page_source(max_chars: int = 20000) -> str:
    """Raw HTML of the current page, truncated to max_chars."""
    html = _get_driver().page_source
    if len(html) > max_chars:
        return html[:max_chars] + f"\n<!-- truncated, {len(html) - max_chars} more chars -->"
    return html


@mcp.tool()
def click(selector: str, by: str = "css") -> dict:
    """Click/tap an element. `by` is one of css, xpath, text."""
    driver = _get_driver()
    el = _find(driver, selector, by)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
    el.click()
    return {"clicked": selector, "url": driver.current_url}


@mcp.tool()
def type_text(selector: str, text: str, by: str = "css", clear: bool = True, submit: bool = False) -> dict:
    """Type into an input on the phone, optionally clearing it first or submitting the form."""
    driver = _get_driver()
    el = _find(driver, selector, by)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
    if clear:
        el.clear()
    el.send_keys(text)
    if submit:
        el.submit()
    return {"typed_into": selector, "submitted": submit, "url": driver.current_url}


@mcp.tool()
def tap(x: int, y: int) -> dict:
    """Send a real single-finger touch at viewport coordinates (CSS pixels).

    Use when there is no sensible selector — e.g. a canvas or a custom overlay.
    """
    driver = _get_driver()
    builder = ActionBuilder(driver, mouse=PointerInput(interaction.POINTER_TOUCH, "finger"))
    builder.pointer_action.move_to_location(x, y).pointer_down().pause(0.05).pointer_up()
    builder.perform()
    return {"tapped": [x, y], "url": driver.current_url}


@mcp.tool()
def scroll(dy: int = 400, dx: int = 0) -> dict:
    """Scroll the page by a pixel delta. Positive dy scrolls down."""
    driver = _get_driver()
    driver.execute_script("window.scrollBy(arguments[0], arguments[1]);", dx, dy)
    return driver.execute_script(
        "return {scrollY: Math.round(window.scrollY), pageHeight: Math.round(document.documentElement.scrollHeight)};"
    )


@mcp.tool()
def evaluate_javascript(code: str) -> str:
    """Run JavaScript in the page and return the result as a string.

    Wrap in `return ...` to get a value back, e.g. `return document.title;`
    """
    return str(_get_driver().execute_script(code))


@mcp.tool()
def console_logs(clear: bool = False) -> dict:
    """Console messages and uncaught errors captured since the last navigation.

    The capture hook is installed by navigate(); if a page navigated itself,
    call install_console_capture() to re-arm it.
    """
    driver = _get_driver()
    result = driver.execute_script(
        "return {installed: !!window.__mcpLogs, logs: (window.__mcpLogs || []).slice()};"
    )
    if clear:
        driver.execute_script("if (window.__mcpLogs) window.__mcpLogs.length = 0;")
    if not result["installed"]:
        result["note"] = "capture hook not on this page; the page likely navigated itself — call install_console_capture()"
    return result


@mcp.tool()
def install_console_capture() -> str:
    """(Re)install the console/error capture hook on the current page."""
    driver = _get_driver()
    return str(driver.execute_script(_CONSOLE_HOOK))


@mcp.tool()
def network_timings(max_entries: int = 60) -> list:
    """Resource timings from the Performance API — URL, type, duration, transfer size.

    A zero transferSize on a non-cached entry usually means the request failed.
    """
    return _get_driver().execute_script(
        """
        return performance.getEntriesByType('resource').slice(-arguments[0]).map(function (e) {
          return {
            name: e.name, type: e.initiatorType,
            duration: Math.round(e.duration), transferSize: e.transferSize,
            status: e.responseStatus
          };
        });
        """,
        max_entries,
    )


@mcp.tool()
def go_back() -> dict:
    """Navigate back in Safari's history."""
    driver = _get_driver()
    driver.back()
    return {"url": driver.current_url, "title": driver.title}


@mcp.tool()
def reload_page() -> dict:
    """Reload the current page and re-arm console capture."""
    driver = _get_driver()
    driver.refresh()
    _install_console_hook(driver)
    return {"url": driver.current_url, "title": driver.title}


@mcp.tool()
def close_session() -> str:
    """End the automation session on the phone. A later call reconnects automatically."""
    global _driver
    with _lock:
        if _driver is None:
            return "no active session"
        try:
            _driver.quit()
        finally:
            _driver = None
    return "session closed"


def _verify() -> int:
    """Prove which physical phone this session drives, before trusting it.

    Device names cannot settle this — safaridriver and Safari's Develop menu read
    them from different places. So paint an unmissable marker on the screen and let
    the human confirm which handset lights up.
    """
    code = os.urandom(3).hex().upper()
    print("Opening a session with the current pinning…", file=sys.stderr)
    try:
        driver = _get_driver()
    except RuntimeError as exc:
        print(f"FAILED\n{exc}", file=sys.stderr)
        return 1
    try:
        caps = driver.capabilities
        print("\nsafaridriver reports:", file=sys.stderr)
        for key in ("safari:deviceName", "safari:deviceUDID", "safari:deviceType",
                    "safari:platformVersion", "browserVersion"):
            print(f"  {key:26} {caps.get(key)}", file=sys.stderr)
        print(f"  pinned to                  {[f'{k}={v}' for k, v in _DEVICE.items() if v] or 'nothing'}",
              file=sys.stderr)

        driver.get("https://example.com")
        driver.execute_script(
            """
            document.title = 'VERIFY ' + arguments[0];
            document.body.innerHTML =
              '<div style="font:700 15vw/1.1 -apple-system;text-align:center;padding:8vh 4vw;' +
              'background:#b00020;color:#fff;min-height:100vh">' + arguments[0] +
              '<div style="font-size:5vw;margin-top:6vh">iphone-safari-mcp</div></div>';
            """,
            code,
        )
        print(
            f"\n>>> LOOK AT YOUR PHONES. One screen is now solid red showing:  {code}\n"
            f">>> Bound to {caps.get('safari:deviceName')} "
            f"({caps.get('safari:deviceUDID')}).\n"
            ">>> If that is the handset you meant, the pinning is correct.\n"
            ">>> If another phone lit up, or none did, it is wrong — do NOT register.",
            file=sys.stderr,
        )
        input("\nPress Enter once you've looked… ")
    finally:
        print(f"cleanup: {close_session()}", file=sys.stderr)
    return 0


def _list_devices() -> int:
    """Enumerate candidate hosts.

    safaridriver has no list command, but a New Session for a UDID that cannot
    match anything makes it report every host it considered, and why each was
    rejected. That report is the device list.
    """
    global _driver
    _DEVICE["safari:deviceUDID"] = "00000000-0000000000000000"
    _DEVICE["safari:deviceName"] = None
    try:
        _get_driver()
    except RuntimeError as exc:
        first = str(exc).split("Checklist:")[0].strip()
        print(first, file=sys.stderr)
        print(
            "\nNOTE: this lists only devices safaridriver considered a candidate, i.e. ones with\n"
            "Remote Automation ON. A phone you see under Safari > Develop but NOT here has\n"
            "Remote Automation off (Settings > Apps > Safari > Advanced).\n"
            "\nPin the one you want with:\n"
            "  export IPHONE_SAFARI_DEVICE_NAME='<name from the list above>'",
            file=sys.stderr,
        )
        return 0
    # A session actually opened, so the sentinel UDID matched something real.
    close_session()
    print("unexpected: sentinel UDID matched a device", file=sys.stderr)
    return 1


def _selftest(url: str) -> int:
    print("Starting iOS Safari session…", file=sys.stderr)
    try:
        info = session_info()
    except RuntimeError as exc:
        print(f"FAILED\n{exc}", file=sys.stderr)
        return 1
    print(f"connected: {info}", file=sys.stderr)
    try:
        print(f"navigating to {url}", file=sys.stderr)
        print(navigate(url), file=sys.stderr)
        shot = _shrink(_get_driver().get_screenshot_as_png())
        out = "/tmp/iphone-safari-selftest.png"
        with open(out, "wb") as fh:
            fh.write(shot)
        print(f"screenshot -> {out} ({len(shot)} bytes)", file=sys.stderr)
        snap = snapshot()
        print(f"snapshot: {len(snap['elements'])} interactive elements, viewport {snap['viewport']}", file=sys.stderr)
        for el in snap["elements"][:5]:
            print(f"    {el['tag']:8} {el['css'][:40]:42} {el['text'][:40]!r}", file=sys.stderr)
        evaluate_javascript("console.warn('selftest warning'); console.error('selftest error');")
        logs = console_logs()
        print(f"console: installed={logs['installed']} {logs['logs']}", file=sys.stderr)
        print(f"network: {len(network_timings())} resource timings", file=sys.stderr)
        print(f"scroll: {scroll(200)}", file=sys.stderr)
    finally:
        # Never leave the automation session open on the phone; a leaked session
        # makes the next run fail with "devices were found, but could not be used".
        print(f"cleanup: {close_session()}", file=sys.stderr)
    print("OK", file=sys.stderr)
    return 0


def _cleanup_on_exit() -> None:
    global _driver, _driver_proc
    if _driver is not None:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None
    if _driver_proc is not None and _driver_proc.poll() is None:
        _driver_proc.terminate()
        try:
            _driver_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _driver_proc.kill()
    _driver_proc = None


atexit.register(_cleanup_on_exit)


def _on_signal(signum, _frame):
    _cleanup_on_exit()
    raise SystemExit(128 + signum)


_USAGE = """\
iphone-safari-mcp — drive Safari on a physically attached iPhone over safaridriver.

With no arguments, runs as an MCP server on stdio (how agents launch it).

  --verify                  Paint a random code on the phone to prove which handset
                            this session controls. Do this before trusting a pin.
  --selftest [URL]          Full round trip: connect, navigate, screenshot, snapshot,
                            console, network, scroll. Default https://example.com
  --list-devices            Show the hosts safaridriver currently sees, and why each
                            was rejected.
  --device NAME             Pin by device name (safari:deviceName).
  --udid UDID               Pin by UDID (safari:deviceUDID). Preferred — names drift.
  --driver-url URL          Attach to a safaridriver you started yourself, e.g.
                            `safaridriver --diagnose -p 4444`, for discovery logs.
  -h, --help                This message.

Environment (same meanings, for MCP registration):
  IPHONE_SAFARI_DEVICE_UDID / _NAME / _TYPE
  IPHONE_SAFARI_DRIVER_URL
  IPHONE_SAFARI_DISCOVERY_TIMEOUT   seconds to retry while USB enumeration
                                    completes (default 45)

Requires on the phone: Settings > Apps > Safari > Advanced > Web Inspector AND
Remote Automation both ON, plugged in, unlocked, trusted. On the Mac, once:
`safaridriver --enable`.
"""


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point. With no arguments, serve MCP over stdio."""
    global _DRIVER_URL
    argv = sys.argv[1:] if argv is None else argv
    if "-h" in argv or "--help" in argv:
        print(_USAGE)
        return 0
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass
    if "--device" in argv:
        _DEVICE["safari:deviceName"] = argv[argv.index("--device") + 1]
    if "--udid" in argv:
        _DEVICE["safari:deviceUDID"] = argv[argv.index("--udid") + 1]
    if "--driver-url" in argv:
        _DRIVER_URL = argv[argv.index("--driver-url") + 1]
    if "--list-devices" in argv:
        return _list_devices()
    if "--verify" in argv:
        return _verify()
    if "--selftest" in argv:
        idx = argv.index("--selftest")
        target = argv[idx + 1] if len(argv) > idx + 1 else "https://example.com"
        if target.startswith("-"):
            target = "https://example.com"
        return _selftest(target)
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
