const { chromium } = require('/tmp/app-playwright/node_modules/playwright');
const fs = require('fs');
(async () => {
  // This is the disposable browser viewing the synthetic UI. The actual app
  // browser under test runs in its container with its sandbox enabled.
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', args: ['--no-sandbox'] });
  const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(process.argv[2]);
    await page.waitForFunction(() => document.querySelector('#previewState').textContent.startsWith('Connected'), {timeout: 30000});
    const canvas = page.locator('#screen canvas');
    await canvas.scrollIntoViewIfNeeded();
    const box = await canvas.boundingBox();
    await page.mouse.click(box.x + box.width * 180/1920, box.y + box.height * 570/1080);
    await page.keyboard.type('keyboard works');
    // Exercise an actual paste into the app's masked dialog, followed by
    // Unicode insertion into the focused field in the separate app browser.
    await page.getByRole('button', {name:'Paste', exact:true}).click();
    const secret = 'P@ss "quotes" \\ $ & <tag> café 🔑';
    const input = page.getByLabel('Text to paste', {exact:true});
    if (await input.getAttribute('type') !== 'password') throw Error('Paste is not masked');
    await page.context().grantPermissions(['clipboard-read','clipboard-write'], {origin:process.argv[2]});
    await page.evaluate(text => navigator.clipboard.writeText(text), secret);
    await input.focus();
    await page.keyboard.press('Control+V');
    if (await input.inputValue() !== secret) throw Error('Local clipboard paste failed');
    await page.getByRole('button', {name:'Send text', exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('#pasteDialog').open);
    if (await page.locator('#pasteValue').inputValue() !== '') throw Error('Paste dialog retained text');
    await canvas.scrollIntoViewIfNeeded();
    const afterPaste = await canvas.boundingBox();
    await page.mouse.click(afterPaste.x + afterPaste.width * 140/1920, afterPaste.y + afterPaste.height * 637/1080);
    await page.getByRole('button', {name:'Paste', exact:true}).click();
    await input.fill('cancelled fixture');
    await page.getByRole('button', {name:'Cancel paste', exact:true}).click();
    if (await page.locator('#pasteValue').inputValue() !== '') throw Error('Cancel retained text');
    await page.screenshot({path: process.argv[3] + '/app.png', fullPage: true});
    if (errors.length) throw Error(errors.join('\n'));
  } catch (error) {
    await page.screenshot({path: process.argv[3] + '/failure.png', fullPage: true});
    fs.writeFileSync(process.argv[3] + '/ui-errors.json', JSON.stringify(errors));
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
