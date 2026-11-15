const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const path = require('node:path');
const { test } = require('node:test');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../..');
for (const promptPosition of ['inline', 'bottom']) {
test(`Python commands, pipelines and cancellation work with a ${promptPosition} prompt in a thread-free browser`, { timeout: 180000 }, async () => {
  const server = spawn(process.env.ANYSHELL_TEST_PYTHON || 'python',
    ['-u', '-m', 'http.server', '0', '--bind', '127.0.0.1', '--directory', path.join(root, '.cache/browser-site')],
    { stdio: ['ignore', 'pipe', 'pipe'] });
  let log = '';
  const port = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('HTTP server startup timed out')), 10000);
    server.stdout.on('data', data => {
      log += data;
      const match = log.match(/port (\d+)/);
      if (match) { clearTimeout(timer); resolve(Number(match[1])); }
    });
    server.on('exit', code => { clearTimeout(timer); reject(new Error(`Server exited ${code}`)); });
  });
  let browser;
  let page;
  try {
    browser = await chromium.launch({headless: true});
    page = await browser.newPage({viewport: {width: 1100, height: 760}});
    await page.goto(`http://127.0.0.1:${port}/?prompt=${promptPosition}`);
    await page.waitForFunction(() => /^(Running|Could not start)/.test(document.querySelector('#status').textContent), null, {timeout: 120000});
    assert.equal(await page.locator('#error').isHidden(), true, await page.locator('#error').textContent());
    async function python(code) {
      return page.evaluate(code => window.anyshellDemo.pyodide.runPython(code), code);
    }
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython(
      "bool(browser_app.query('CommandInput')) and browser_app.query_one('CommandInput').has_focus"));
    assert.equal(await python("browser_app.query_one('Shell').prompt_position"), promptPosition);
    for (const viewport of [{width: 390, height: 640}, {width: 800, height: 400}, {width: 390, height: 420}, {width: 1100, height: 760}]) {
      await page.setViewportSize(viewport);
      await page.waitForFunction(() => {
        const terminal = window.anyshellDemo.terminal;
        const host = document.querySelector('#terminal-screen').getBoundingClientRect();
        const screen = document.querySelector('.xterm-screen').getBoundingClientRect();
        const cellHeight = screen.height / terminal.rows;
        return screen.height > 0 && screen.bottom <= host.bottom + 0.5 &&
          screen.right <= host.right + 0.5 && host.width - screen.width < screen.width / terminal.cols + 0.5 &&
          terminal.rows === Math.floor(host.height / cellHeight + 0.001) &&
          window.anyshellDemo.pyodide.runPython(`browser_app.size.height == ${terminal.rows} and browser_app.size.width == ${terminal.cols}`) &&
          terminal.buffer.active.getLine(terminal.buffer.active.baseY + terminal.rows - 1)?.translateToString().includes('Quit');
      });
    }
    await page.keyboard.type('cat notes/fruits.txt | grep ^a | sort | count');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython(
      "len(session.history) == 1 and not session.running and not browser_app.query_one('Shell')._busy"));
    assert.equal(await python("[e.value for e in browser_app.query_one('Shell').output if e.kind == 'output'] == [2]"), true);
    await page.keyboard.press('ArrowUp');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CommandInput').value == 'cat notes/fruits.txt | grep ^a | sort | count'"));
    assert.equal(await python("browser_app.query_one('CommandInput').value"), 'cat notes/fruits.txt | grep ^a | sort | count');
    await page.keyboard.press('ArrowDown');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CommandInput').value == ''"));
    await page.keyboard.press('Control+r');
    await page.keyboard.type('fruits');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('Shell').history_search_open and browser_app.query_one('CompletionPopup').display and browser_app.query_one('CompletionMenu').option_count == 1 and browser_app.query_one('Shell')._search_index == 0"));
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query_one('Shell').history_search_open and browser_app.query_one('CommandInput').value == session.history[0] and len(session.history) == 1 and not session.running"));
    await page.keyboard.press('Control+c');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CommandInput').value == ''"));
    await page.keyboard.type('sleep 30');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CommandInput').value == 'sleep 30'"));
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython('session.running'));
    assert.equal(await python("browser_app.query_one('#command-line').display"), promptPosition === 'bottom');
    const foregroundCursor = promptPosition === 'bottom' ? 'CommandInput' : 'RunningCursor';
    await page.waitForFunction(name => window.anyshellDemo.pyodide.runPython(
      `browser_app.query_one('${name}').has_focus and browser_app.cursor_position == browser_app.query_one('${name}').cursor_screen_offset`), foregroundCursor);
    await page.waitForFunction(name => window.anyshellDemo.pyodide.runPython(
      `not browser_app.query_one('${name}')._draw_cursor`), foregroundCursor);
    await page.waitForFunction(name => window.anyshellDemo.pyodide.runPython(
      `browser_app.query_one('${name}')._draw_cursor`), foregroundCursor);

    await page.keyboard.press('Control+c');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython('not session.running and session.last_status == 130'));
    await page.keyboard.type('sleep 30 &');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython(
      "len(session.jobs) == 1 and not session.running and not browser_app.query_one('Shell')._busy"));
    assert.equal(await python("session.jobs[1].result is None"), true);
    assert.equal(await python("browser_app.query_one('#command-line').display and browser_app.query_one('CommandInput').has_focus"), true);
    await page.keyboard.type('kill 1');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython(
      "session.jobs[1].done.is_set() and not browser_app.query_one('Shell')._busy"));
    assert.equal(await python("session.jobs[1].result.cancelled"), true);

    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.keyboard.type('echo select me');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython(
      "not browser_app.query_one('Shell')._busy and session.history[-1] == 'echo select me'"));
    const selectionCell = JSON.parse(await python("__import__('json').dumps((browser_app.query_one('Transcript').content_region.x, browser_app.query_one('Transcript').content_region.y + len(browser_app.query_one('Transcript').lines) - 1 - browser_app.query_one('Transcript').scroll_offset.y))"));
    const selectionScreen = await page.locator('.xterm-screen').boundingBox();
    const columns = await page.evaluate(() => window.anyshellDemo.terminal.cols);
    const rows = await page.evaluate(() => window.anyshellDemo.terminal.rows);
    const cellWidth = selectionScreen.width / columns;
    const cellHeight = selectionScreen.height / rows;
    await page.mouse.click(selectionScreen.x + (columns - 4) * cellWidth,
      selectionScreen.y + 5.5 * cellHeight);
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython(
      "browser_app.query_one('CommandInput').has_focus"));
    const originalSelectionColors = await page.evaluate(({x, y}) => {
      const buffer = window.anyshellDemo.terminal.buffer.active;
      const cell = buffer.getLine(buffer.baseY + y).getCell(x);
      return {foreground: cell.getFgColor(), background: cell.getBgColor()};
    }, {x: selectionCell[0], y: selectionCell[1]});
    await page.mouse.move(selectionScreen.x + (selectionCell[0] + 0.2) * cellWidth,
      selectionScreen.y + (selectionCell[1] + 0.5) * cellHeight);
    await page.mouse.down();
    await page.mouse.move(selectionScreen.x + (selectionCell[0] + 8.2) * cellWidth,
      selectionScreen.y + (selectionCell[1] + 0.5) * cellHeight, {steps: 8});
    await page.mouse.up();
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython(
      "browser_app.screen.get_selected_text() == 'select me'"));
    await page.waitForFunction(({x, y, original}) => {
      const buffer = window.anyshellDemo.terminal.buffer.active;
      const cell = buffer.getLine(buffer.baseY + y)?.getCell(x);
      return cell?.getChars() === 's' && cell.getFgColor() === original.foreground &&
        cell.getBgColor() !== original.background;
    }, {x: selectionCell[0], y: selectionCell[1], original: originalSelectionColors});
    await page.keyboard.press('Control+c');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.clipboard == 'select me'"));
    await page.waitForFunction(async () => (await navigator.clipboard.readText()) === 'select me');
    await python("browser_app.screen.clear_selection()");
    await page.keyboard.type('write browser.txt "hello browser"');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("'browser.txt' in [r['name'] for r in session.filesystem.listdir('/')]"));
    assert.equal(await python("list(session.filesystem.read_lines('/browser.txt')) == ['hello browser']"), true);
    assert.equal(await python("not constants.THREADS_ENABLED"), true);
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython('not session.running'));
    await page.keyboard.type('ls');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython(
      "session.history[-1] == 'ls' and not browser_app.query_one('Shell')._busy"));
    assert.equal(await python("any('notes/' in line.text and 'readme.txt' in line.text for line in browser_app.query_one('RichLog').lines)"), true);
    await page.waitForFunction(() => {
      const buffer = window.anyshellDemo.terminal.buffer.active;
      const lines = Array.from({length: buffer.length}, (_, i) => buffer.getLine(i)?.translateToString() || '').join('\n');
      return lines.includes('notes/') && lines.includes('readme.txt');
    });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.screenshot({path: path.join(root, promptPosition === 'inline' ? '.cache/browser-demo.png' : '.cache/browser-bottom.png')});
    async function command(source) {
      await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython(
        "not browser_app.query_one('Shell')._busy and browser_app.query_one('CommandInput').value == '' and browser_app.query_one('CommandInput').has_focus"));
      await page.keyboard.type(source);
      await page.keyboard.press('Enter');
      await page.waitForFunction(source => window.anyshellDemo.pyodide.runPython(
        `session.history[-1] == ${JSON.stringify(source)} and not browser_app.query_one('Shell')._busy`), source);
    }
    await page.keyboard.type('echo first');
    await page.keyboard.press('Shift+Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CommandInput').value == 'echo first\\n'"));
    await page.keyboard.type('echo second');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CommandInput').value == 'echo first\\necho second'"));
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("session.history[-1] == 'echo first\\necho second' and not browser_app.query_one('Shell')._busy"));
    assert.equal(await python("[e.value for e in browser_app.query_one('Shell').output if e.kind == 'output'][-2:] == ['first', 'second']"), true);
    await page.keyboard.type("echo 'first");
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CommandInput').value.endswith('\\n')"));
    await page.keyboard.type("second'");
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query_one('Shell')._busy and browser_app.query_one('CommandInput').value == '' and browser_app.query_one('CommandInput').has_focus"));
    assert.equal(await python("[e.value for e in browser_app.query_one('Shell').output if e.kind == 'output'][-1] == 'first\\nsecond'"), true);
    await page.keyboard.type('s');
    await page.keyboard.press('Tab');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('Shell').completion_open"));
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CommandInput').value == 'sleep ' and browser_app.query_one('CommandInput').cursor_position == 6 and not browser_app.query_one('Shell').completion_open"));
    assert.equal(await python("browser_app.query_one('CommandInput').value"), 'sleep ');
    await page.keyboard.press('Control+c');
    await command('touch created.txt');
    assert.equal(await python("session.filesystem.stat('/created.txt')['size']"), 0);
    await command('ln -s link-target.txt linked.txt');
    assert.equal(await python("session.filesystem.entry_type('/linked.txt')"), 'symlink');
    await command('write linked.txt "through link"');
    assert.equal(await python("session.filesystem.read_text('/link-target.txt')"), 'through link\n');
    await command('rm linked.txt link-target.txt');
    await command('echo first >redirected.txt');
    await command('echo second >>redirected.txt');
    await command('cat <redirected.txt | count >line-count.txt');
    assert.equal(await python("session.filesystem.read_text('/redirected.txt')"), 'first\nsecond\n');
    assert.equal(await python("session.filesystem.read_text('/line-count.txt')"), '2\n');
    await command('tail -n 1 redirected.txt');
    assert.equal(await python("[e.value for e in browser_app.query_one('Shell').output if e.kind == 'output'][-1]"), 'second');
    await page.keyboard.type('tail -n 1 -f redirected.txt');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("session.running and browser_app.query_one('Shell')._busy and browser_app.query_one('Shell').output[-1].value == 'second'"));
    await python("session.filesystem.append_text('/redirected.txt', 'followed entry\\n')");
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("any(e.kind == 'output' and e.value == 'followed entry' for e in browser_app.query_one('Shell').output)"));
    await page.keyboard.press('Control+c');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not session.running and not browser_app.query_one('Shell')._busy and session.last_status == 130"));
    await command('rm redirected.txt line-count.txt');
    await page.keyboard.type('edit created.txt');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("any(area.has_focus for editor in browser_app.query('FileEditor') for area in editor.query('TextArea'))"));
    await page.keyboard.type('edited in the browser');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('FileEditor').query_one('TextArea').text == 'edited in the browser'"));
    await page.keyboard.press('Control+s');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("session.filesystem.read_text('/created.txt') == 'edited in the browser'"));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.screenshot({path: path.join(root, `.cache/browser-editor-${promptPosition}.png`)});
    await page.keyboard.press('Control+q');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query('FileEditor') and not browser_app.query_one('Shell')._busy and browser_app.query_one('CommandInput').has_focus"));
    await page.keyboard.type('edit created.txt');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("any(area.has_focus for editor in browser_app.query('FileEditor') for area in editor.query('TextArea'))"));
    await page.keyboard.type(' unsaved');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("' unsaved' in browser_app.query_one('FileEditor').query_one('TextArea').text"));
    await page.keyboard.press('Control+q');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('FileEditor')._confirming_close"));
    await page.keyboard.press('Control+q');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query('FileEditor') and not browser_app.query_one('Shell')._busy and browser_app.query_one('CommandInput').has_focus"));
    assert.equal(await python("session.filesystem.read_text('/created.txt')"), 'edited in the browser');
    await command('mkdir empty');
    await command('rmdir empty');
    assert.equal(await python("session.last_status == 0 and not session.filesystem.is_dir('/empty')"), true);
    await command('cp created.txt duplicate.txt');
    await command('mv duplicate.txt renamed.txt');
    assert.equal(await python("session.last_status == 0 and session.filesystem.read_text('/renamed.txt') == 'edited in the browser' and session.filesystem.read_text('/created.txt') == 'edited in the browser'"), true);
    await command('cp -r notes notes-copy');
    await command('mv notes-copy moved-notes');
    assert.equal(await python("session.last_status == 0 and not session.filesystem.is_dir('/notes-copy') and session.filesystem.read_text('/moved-notes/fruits.txt') == session.filesystem.read_text('/notes/fruits.txt')"), true);
    const pageEventsBeforeFailure = await python("sum(event.kind == 'page' for event in browser_app.query_one('Shell').output)");
    for (const pagerCommand of ['more', 'less']) {
      await command(`${pagerCommand} missing-pager.txt`);
      assert.equal(await python(`session.last_status == 1 and "${pagerCommand}: 'missing-pager.txt': no such file or directory" in [line.text.rstrip() for line in browser_app.query_one('Transcript').lines]`), true);
      assert.equal(await python("sum(event.kind == 'page' for event in browser_app.query_one('Shell').output)"), pageEventsBeforeFailure);
    }
    await page.keyboard.type('cat notes/fruits.txt | more');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython(
      "bool(browser_app.query('Pager')) and browser_app.query_one('Pager').loaded and browser_app.query_one('Pager').has_focus and [line.text.strip() for line in browser_app.query_one('PagerLog').lines] == ['pear', 'apple', 'banana', 'apricot']"));
    assert.equal(await python("any('apple' in line.text for line in browser_app.query_one('PagerLog').lines)"), true);
    await page.keyboard.press('q');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query('Pager') and not browser_app.query_one('Shell')._busy and browser_app.query_one('CommandInput').has_focus"));
    await python("session.filesystem.write_text('/pager.txt', '\\n'.join(f'line {i}' for i in range(120)))");
    await page.keyboard.type('more pager.txt');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("bool(browser_app.query('Pager')) and browser_app.query_one('Pager').loaded"));
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('PagerLog').max_scroll_y > 0 and browser_app.query_one('PagerLog').scrollable_content_region.height > 0 and len(browser_app.query_one('PagerLog').lines) == 120 and browser_app.query_one('Pager').has_focus"));
    await page.keyboard.press('Space');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('PagerLog').scroll_y > 0"));
    await page.screenshot({path: path.join(root, `.cache/browser-pager-${promptPosition}.png`)});
    await page.keyboard.press('Control+q');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query('Pager') and not browser_app.query_one('Shell')._busy and browser_app.query_one('CommandInput').has_focus"));
    await page.keyboard.type('cat pager.txt | less');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("bool(browser_app.query('Pager')) and browser_app.query_one('Pager').loaded and browser_app.query_one('PagerLog').max_scroll_y > 0"));
    await page.keyboard.type('/');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('PagerSearchInput').has_focus"));
    await page.keyboard.type('^line (30|60)$');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('PagerSearchInput').value == '^line (30|60)$'"));
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('Pager')._match_line == 30"));
    await page.keyboard.press('n');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('Pager')._match_line == 60"));
    await page.keyboard.press('Shift+n');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('Pager')._match_line == 30"));
    await page.screenshot({path: path.join(root, `.cache/browser-less-${promptPosition}.png`)});
    await page.keyboard.press('Control+q');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query('Pager') and not browser_app.query_one('Shell')._busy and browser_app.query_one('CommandInput').has_focus"));
    await command("find notes -name '*.txt' -type f | select path");
    assert.equal(await python("session.last_status == 0 and [e.value for e in browser_app.query_one('Shell').output if e.kind == 'output'][-1] == 'notes/fruits.txt'"), true);
    await command('find notes -maxdepth 0');
    assert.equal(await python("[e.value for e in browser_app.query_one('Shell').output if e.kind == 'output'][-1] == {'name': 'notes', 'path': 'notes', 'type': 'directory'}"), true);
    await command('ls --help');
    assert.equal(await python("any(e.kind == 'output' and isinstance(e.value, str) and '--long' in e.value for e in browser_app.query_one('Shell').output)"), true);
    await command('ls --bogus');
    assert.equal(await python('session.last_status'), 1);
    await command('clear');
    await command('ls -l');
    assert.equal(await python("session.last_status == 0 and any(e.kind == 'output' and isinstance(e.value, dict) and e.value.get('name') == 'browser.txt' and e.value.get('size') == 14 and isinstance(e.value.get('modified'), float) for e in browser_app.query_one('Shell').output)"), true);
    assert.equal(await python("any('Modified (UTC)' in line.text for line in browser_app.query_one('RichLog').lines)"), true);
    await page.waitForFunction(() => {
      const buffer = window.anyshellDemo.terminal.buffer.active;
      return Array.from({length: buffer.length}, (_, i) => buffer.getLine(i)?.translateToString() || '').join('\n').includes('Modified (UTC)');
    });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.screenshot({path: path.join(root, `.cache/browser-long-${promptPosition}.png`)});
    await python("session.filesystem.mkdir('/cursor-check'); [session.filesystem.write_text(f'/cursor-check/file-{i:03}.txt', '') for i in range(400)]");
    await page.keyboard.type('ls -l cursor-check');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython(
      "session.history[-1] == 'ls -l cursor-check' and not browser_app.query_one('Shell')._busy and browser_app.query_one('CommandInput').has_focus and browser_app.cursor_position == browser_app.query_one('CommandInput').cursor_screen_offset and browser_app.query_one('Shell').content_region.contains_region(browser_app.query_one('CommandInput').region) and (browser_app.query_one('Shell').prompt_position != 'inline' or browser_app.query_one('#shell-scroll').is_vertical_scroll_end)"));

    if (promptPosition === 'inline') {
      await python("browser_app.query_one('Shell')._request_prompt_scroll(); browser_app.query_one('#shell-scroll').scroll_home(animate=False, immediate=True)");
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await python("browser_app.query_one('#shell-scroll').scroll_y == 0"), true);
    }
    await python("session.filesystem.write_text('/long-' + 'x' * 180 + '-first.txt', ''); session.filesystem.write_text('/long-' + 'x' * 180 + '-second.txt', '')");
    await page.keyboard.type('cat long-');
    await page.keyboard.press('Tab');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CommandInput').value == 'cat long-' + 'x' * 180 + '-' and not browser_app.query_one('Shell').completion_open"));
    await page.keyboard.press('Tab');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CompletionPopup').show_horizontal_scrollbar and browser_app.query_one('CompletionPopup').max_scroll_x > 0"));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.screenshot({path: path.join(root, `.cache/browser-completion-${promptPosition}.png`)});
    await python("browser_app.query_one('CompletionPopup').scroll_to(x=1000, animate=False, immediate=True)");
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CompletionPopup').scroll_x > 0"));
    await page.keyboard.press('Escape');
    await page.keyboard.press('Control+c');
    await page.keyboard.press('Alt+v');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("len(browser_app.query('Shell')) == 2 and browser_app.query_one('ShellWorkspace').active is browser_app.query_one('ShellWorkspace').panes[-1] and browser_app.query_one('ShellWorkspace').active.query_one('CommandInput').has_focus"));
    assert.equal(await python("browser_app.query_one('SplitPane').first.query_one('Shell') is browser_app.query_one('ShellWorkspace').panes[0] and browser_app.query_one('SplitPane').second.query_one('Shell') is browser_app.query_one('ShellWorkspace').panes[1]"), true);
    const gestureScreen = await page.locator('.xterm-screen').boundingBox();
    const gestureCellWidth = gestureScreen.width / await page.evaluate(() => window.anyshellDemo.terminal.cols);
    const gestureCellHeight = gestureScreen.height / await page.evaluate(() => window.anyshellDemo.terminal.rows);
    function mousePoint(x, y) {
      return [gestureScreen.x + (x + 0.5) * gestureCellWidth,
        gestureScreen.y + (y + 0.5) * gestureCellHeight];
    }
    async function panePoint(index) {
      const [x, y] = JSON.parse(await python(`__import__('json').dumps((browser_app.query_one('ShellWorkspace').panes[${index}].query_one('CommandInput').region.x + 2, browser_app.query_one('ShellWorkspace').panes[${index}].query_one('CommandInput').region.y))`));
      return mousePoint(x, y);
    }
    for (const hidden of [false, true]) {
      if (hidden) {
        await page.keyboard.press('Alt+d');
        await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query_one('SplitPane').show_divider and browser_app.query_one('ShellWorkspace').panes[0].region.right == browser_app.query_one('ShellWorkspace').panes[1].region.x"));
      }
      const [beforeSize, x, y] = JSON.parse(await python("__import__('json').dumps((browser_app.query_one('SplitPane').first_size, browser_app.query_one('ShellWorkspace').panes[1].region.x + 4, browser_app.query_one('ShellWorkspace').panes[1].region.y + 4))"));
      await page.keyboard.down('Alt');
      await page.mouse.move(...mousePoint(x, y));
      await page.mouse.down({button: 'right'});
      await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.mouse_captured is browser_app.query_one('SplitPane').divider"));
      await page.mouse.move(...mousePoint(x + 4, y), {steps: 4});
      await page.mouse.up({button: 'right'});
      await page.keyboard.up('Alt');
      await page.waitForFunction(before => window.anyshellDemo.pyodide.runPython(`browser_app.query_one('SplitPane').first_size == ${before + 4} and browser_app.mouse_captured is None`), beforeSize);
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('SplitPane').position == 0.5"));
    }
    await page.keyboard.press('Alt+d');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('SplitPane').show_divider"));
    await python("gesture_panes = list(browser_app.query_one('ShellWorkspace').panes); None");
    for (const [source, destination] of [[0, 1], [1, 0]]) {
      const start = await panePoint(source);
      const end = await panePoint(destination);
      await page.keyboard.down('Alt');
      await page.mouse.move(...start);
      await page.mouse.down();
      await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.mouse_captured is browser_app.query_one('SplitPane')"));
      await page.mouse.move(...end, {steps: 6});
      await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('SplitPane')._drag_preview is not None and browser_app.query_one('SplitPane')._drag_preview.is_mounted"));
      await page.mouse.up();
      await page.keyboard.up('Alt');
      // Spatial navigation needs the layout after reparenting, as well as the tree.
      await page.waitForFunction(reversed => window.anyshellDemo.pyodide.runPython(`browser_app.query_one('ShellWorkspace').panes == gesture_panes[::${reversed ? -1 : 1}] and browser_app.mouse_captured is None and browser_app.query_one('ShellWorkspace').panes[0].region.right <= browser_app.query_one('ShellWorkspace').panes[1].region.x`), source === 0);
    }
    await page.keyboard.press('Alt+ArrowRight');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('ShellWorkspace').active is gesture_panes[1] and gesture_panes[1].query_one('CommandInput').has_focus"));
    await page.keyboard.type('echo split >shared.txt');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('ShellWorkspace').active.session.history == ['echo split >shared.txt'] and not browser_app.query_one('ShellWorkspace').active._busy and session.filesystem.read_text('/shared.txt') == 'split\\n'"));
    await page.keyboard.press('Alt+h');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("len(browser_app.query('Shell')) == 3 and browser_app.query_one('ShellWorkspace').active is browser_app.query_one('ShellWorkspace').panes[2] and browser_app.query_one('ShellWorkspace').active.query_one('CommandInput').has_focus and browser_app.query_one('ShellWorkspace').panes[2].region.y >= browser_app.query_one('ShellWorkspace').panes[1].region.bottom"));
    const cornerBefore = JSON.parse(await python("__import__('json').dumps([split.first_size for split in browser_app.query('SplitPane')])"));
    const [cornerX, cornerY] = JSON.parse(await python("__import__('json').dumps(tuple(browser_app.query_one('ShellWorkspace').active.region.offset))"));
    await page.mouse.move(...mousePoint(cornerX, cornerY));
    await page.mouse.down();
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.mouse_captured is list(browser_app.query('SplitPane'))[-1].divider"));
    await page.mouse.move(...mousePoint(cornerX + 2, cornerY + 2), {steps: 3});
    await page.waitForFunction(before => window.anyshellDemo.pyodide.runPython(`[split.first_size for split in browser_app.query('SplitPane')] == [${before[0] + 2}, ${before[1] + 2}]`), cornerBefore);
    await page.keyboard.press('Escape');
    // Wait for the terminal to decode the standalone Escape before mouse-up.
    await page.waitForFunction(before => window.anyshellDemo.pyodide.runPython(`browser_app.mouse_captured is None and [split.first_size for split in browser_app.query('SplitPane')] == [${before[0]}, ${before[1]}]`), cornerBefore);
    await page.mouse.up();
    await page.keyboard.press('Alt+d');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("all(not split.show_divider for split in browser_app.query('SplitPane')) and browser_app.query_one('ShellWorkspace').panes[0].region.right == browser_app.query_one('ShellWorkspace').panes[1].region.x"));
    await page.keyboard.press('Alt+d');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("all(split.show_divider for split in browser_app.query('SplitPane')) and browser_app.query_one('ShellWorkspace').panes[0].region.right + 1 == browser_app.query_one('ShellWorkspace').panes[1].region.x"));
    await page.keyboard.press('Alt+ArrowUp');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('ShellWorkspace').active is browser_app.query_one('ShellWorkspace').panes[1] and browser_app.query_one('ShellWorkspace').active.query_one('CommandInput').has_focus"));
    await page.keyboard.press('Alt+ArrowLeft');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('ShellWorkspace').active is browser_app.query_one('ShellWorkspace').panes[0]"));
    await page.keyboard.press('Alt+ArrowRight');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('ShellWorkspace').active is browser_app.query_one('ShellWorkspace').panes[1]"));
    await page.keyboard.press('Alt+ArrowDown');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('ShellWorkspace').active is browser_app.query_one('ShellWorkspace').panes[2]"));
    await page.keyboard.press('Alt+ArrowUp');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('ShellWorkspace').active is browser_app.query_one('ShellWorkspace').panes[1]"));
    await page.keyboard.type('exit');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("len(browser_app.query('Shell')) == 2 and browser_app.query_one('ShellWorkspace').active is browser_app.query_one('ShellWorkspace').panes[-1] and browser_app.query_one('ShellWorkspace').active.query_one('CommandInput').has_focus"));
    await page.keyboard.press('Alt+w');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("len(browser_app.query('Shell')) == 1 and browser_app.query_one('CommandInput').has_focus"));
    await page.keyboard.type('exit 17');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelector('#status').textContent.startsWith('Stopped'));
    assert.equal(await python('browser_app.return_value'), 17);
    assert.equal(await page.locator('#error').isHidden(), true);
  } catch (error) {
    if (page) {
      try {
        console.error(await page.evaluate(() => ({
          status: document.querySelector('#status')?.textContent,
          error: document.querySelector('#error')?.textContent,
        })));
        console.error(await page.evaluate(() => window.anyshellDemo?.pyodide.runPython(
          "repr((session.history, session.running, session.last_status, [(type(node).__name__) for node in browser_app.query('Shell, FileEditor, Pager')])) if 'session' in globals() else 'Python shell has not initialized'")));
      } catch (diagnosticError) { console.error(String(diagnosticError)); }
      try {
        await page.screenshot({path: path.join(root, '.cache/browser-failure.png')});
      } catch (screenshotError) { console.error(String(screenshotError)); }
    }
    throw error;
  } finally {
    await browser?.close();
    if (server.exitCode === null && server.signalCode === null) {
      const exited = once(server, 'exit');
      server.kill();
      await exited;
    }
  }
});
}
