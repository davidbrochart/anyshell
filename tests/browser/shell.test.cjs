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
    await page.waitForFunction(() => document.querySelector('#status').textContent.startsWith('Running'), null, {timeout: 120000});
    async function python(code) {
      return page.evaluate(code => window.anyshellDemo.pyodide.runPython(code), code);
    }
    assert.equal(await python("browser_app.query_one('Shell').prompt_position"), promptPosition);
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
    await page.keyboard.type('sleep 30');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CommandInput').value == 'sleep 30'"));
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython('session.running'));
    await page.keyboard.press('Control+c');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython('not session.running and session.last_status == 130'));
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
        "not browser_app.query_one('Shell')._busy and browser_app.query_one('CommandInput').value == ''"));
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
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query_one('Shell')._busy and browser_app.query_one('CommandInput').value == ''"));
    assert.equal(await python("[e.value for e in browser_app.query_one('Shell').output if e.kind == 'output'][-1] == 'first\\nsecond'"), true);
    await page.keyboard.type('s');
    await page.keyboard.press('Tab');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('Shell').completion_open"));
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('CommandInput').value == 'sleep' and not browser_app.query_one('Shell').completion_open"));
    assert.equal(await python("browser_app.query_one('CommandInput').value"), 'sleep');
    await page.keyboard.press('Control+c');
    await command('touch created.txt');
    assert.equal(await python("session.filesystem.stat('/created.txt')['size']"), 0);
    await page.keyboard.type('edit created.txt');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("bool(browser_app.query('FileEditor'))"));
    await page.keyboard.type('edited in the browser');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('FileEditor').query_one('TextArea').text == 'edited in the browser'"));
    await page.keyboard.press('Control+s');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("session.filesystem.read_text('/created.txt') == 'edited in the browser'"));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.screenshot({path: path.join(root, `.cache/browser-editor-${promptPosition}.png`)});
    await page.keyboard.press('Control+q');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query('FileEditor') and not browser_app.query_one('Shell')._busy"));
    await page.keyboard.type('edit created.txt');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("bool(browser_app.query('FileEditor')) and browser_app.query_one('FileEditor').query_one('TextArea').has_focus"));
    await page.keyboard.type(' unsaved');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("' unsaved' in browser_app.query_one('FileEditor').query_one('TextArea').text"));
    await page.keyboard.press('Control+q');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('FileEditor')._confirming_close"));
    await page.keyboard.press('Control+q');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query('FileEditor') and not browser_app.query_one('Shell')._busy"));
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
    await page.keyboard.type('cat notes/fruits.txt | more');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("bool(browser_app.query('Pager')) and browser_app.query_one('Pager').loaded"));
    assert.equal(await python("any('apple' in line.text for line in browser_app.query_one('PagerLog').lines)"), true);
    await page.keyboard.press('q');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query('Pager') and not browser_app.query_one('Shell')._busy"));
    await python("session.filesystem.write_text('/pager.txt', '\\n'.join(f'line {i}' for i in range(120)))");
    await page.keyboard.type('more pager.txt');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("bool(browser_app.query('Pager')) and browser_app.query_one('Pager').loaded"));
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('PagerLog').max_scroll_y > 0 and browser_app.query_one('Pager').has_focus"));
    await page.keyboard.press('Space');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("browser_app.query_one('PagerLog').scroll_y > 0"));
    await page.screenshot({path: path.join(root, `.cache/browser-pager-${promptPosition}.png`)});
    await page.keyboard.press('Control+q');
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query('Pager') and not browser_app.query_one('Shell')._busy"));
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
    await page.waitForFunction(() => window.anyshellDemo.pyodide.runPython("not browser_app.query('Pager') and not browser_app.query_one('Shell')._busy"));
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
    await page.keyboard.press('Control+q');
    await page.waitForFunction(() => document.querySelector('#status').textContent.startsWith('Stopped'));
    assert.equal(await page.locator('#error').isHidden(), true);
  } catch (error) {
    if (page) {
      try {
        console.error(await page.evaluate(() => window.anyshellDemo?.pyodide.runPython(
          "repr((session.history, session.running, session.last_status, browser_app.query_one('CommandInput').value, [(e.kind, e.value) for e in browser_app.query_one('Shell').output]))")));
        await page.screenshot({path: path.join(root, '.cache/browser-failure.png')});
      } catch (diagnosticError) { console.error(String(diagnosticError)); }
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
