const status = document.querySelector("#status");
const errorBox = document.querySelector("#error");
document.querySelector("#reload").addEventListener("click", () => location.reload());
const promptPosition = new URLSearchParams(location.search).get("prompt") || "inline";
const promptSelect = document.querySelector("#prompt-position");
promptSelect.value = promptPosition;
promptSelect.addEventListener("change", () => {
  const url = new URL(location.href);
  url.searchParams.set("prompt", promptSelect.value);
  location.href = url.href;
});
let ready = false;
let observer;
const disposables = [];
const proxies = [];
try {
  const terminal = new Terminal({ cursorBlink: true, fontSize: 15,
    fontFamily: "ui-monospace, monospace", allowProposedApi: true,
    customGlyphs: true, scrollback: 0, theme: { background: "#0c0e14" } });
  terminal.loadAddon(new Unicode11Addon.Unicode11Addon());
  terminal.unicode.activeVersion = "11";
  const fit = new FitAddon.FitAddon();
  terminal.loadAddon(fit);
  // Fit against an unpadded inner element, not the bordered outer container.
  const terminalScreen = document.querySelector("#terminal-screen");
  terminal.open(terminalScreen);
  // Draw block and box glyphs as shapes so font metrics cannot leave row seams.
  terminal.loadAddon(new CanvasAddon.CanvasAddon());
  // Forward the shell's terminal clipboard writes to the browser clipboard.
  disposables.push(terminal.parser.registerOscHandler(52, (data) => {
    const encoded = data.slice(data.indexOf(";") + 1);
    if (encoded === "?" || !navigator.clipboard?.writeText) return false;
    try {
      const text = new TextDecoder().decode(Uint8Array.from(atob(encoded), c => c.charCodeAt(0)));
      navigator.clipboard.writeText(text).catch(() => {});
      return true;
    } catch { return false; }
  }));
  fit.fit();
  observer = new ResizeObserver(() => fit.fit());
  observer.observe(terminalScreen);
  const { loadPyodide } = await import("https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs");
  const pyodide = await loadPyodide();
  window.anyshellDemo = { pyodide, terminal };
  status.textContent = "Installing Python packages…";
  await pyodide.loadPackage("micropip");
  const response = await fetch("wheel.json", { cache: "no-store" });
  if (!response.ok) throw new Error("Could not load wheel.json");
  const manifest = await response.json();
  pyodide.globals.set("wheel_url", new URL(manifest.wheel, location.href).href);
  await pyodide.runPythonAsync("import micropip; await micropip.install(['anytext>=9.1,<10', wheel_url])");
  pyodide.globals.set("terminal_write", (data) => terminal.write(data));
  pyodide.globals.set("prompt_position", promptPosition);
  pyodide.globals.set("terminal_cols", terminal.cols);
  pyodide.globals.set("terminal_rows", terminal.rows);
  let resize;
  pyodide.globals.set("browser_ready", () => {
    ready = true;
    resize(terminal.cols, terminal.rows);
    status.textContent = "Running";
    terminal.focus();
  });
  const source = await fetch("app.py", { cache: "no-store" });
  if (!source.ok) throw new Error("Could not load app.py");
  pyodide.runPython(await source.text());
  const input = pyodide.globals.get("browser_input");
  resize = pyodide.globals.get("browser_resize");
  proxies.push(input, resize);
  terminal.attachCustomKeyEventHandler((event) => {
    if (ready && event.key === "Enter" && !event.altKey && !event.metaKey &&
        (event.shiftKey || event.ctrlKey)) {
      event.preventDefault();
      if (event.type === "keydown") {
        const modifier = 1 + (event.shiftKey ? 1 : 0) + (event.ctrlKey ? 4 : 0);
        input(`\x1b[13;${modifier}u`);
      }
      return false;
    }
    return true;
  });
  disposables.push(terminal.onData((data) => { if (ready) input(data); }));
  disposables.push(terminal.onResize(({ cols, rows }) => { if (ready) resize(cols, rows); }));
  await pyodide.runPythonAsync("await browser_app.run_async(size=(terminal_cols, terminal_rows))");
  status.textContent = "Stopped · Reload to start again";
} catch (error) {
  status.textContent = "Could not start Anyshell";
  errorBox.textContent = String(error);
  errorBox.hidden = false;
  console.error(error);
} finally {
  ready = false;
  for (const value of disposables) value.dispose();
  for (const value of proxies) value.destroy();
  observer?.disconnect();
}
