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
    await page.mouse.click(box.x + box.width * 140/1920, box.y + box.height * 637/1080);
    await page.screenshot({path: process.argv[3] + '/app.png', fullPage: true});
    if (errors.length) throw Error(errors.join('\n'));
  } catch (error) {
    await page.screenshot({path: process.argv[3] + '/failure.png', fullPage: true});
    fs.writeFileSync(process.argv[3] + '/ui-errors.json', JSON.stringify(errors));
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
