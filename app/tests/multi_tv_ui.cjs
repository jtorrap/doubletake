// Synthetic backend only: no user profiles, credentials, or network receivers.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '/tmp/app-playwright/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const staticDir = path.join(__dirname, '../service/static');
const outputDir = process.argv[2];

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.CHROME_PATH || (process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : '/usr/bin/google-chrome'),
    // This disposable UI-test browser has no real app data.
    args: process.platform === 'linux' ? ['--no-sandbox'] : [],
  });
  const page = await browser.newPage({viewport: {width:745, height:1000}, colorScheme:'dark'});
  const errors = [], actions = [];
  let stateReads = 0;
  let channelGate = null;
  const fixture = {
    version:'test', csrf:'synthetic-csrf', mqtt_connected:true,
    channels:{version:1,devices:[{id:'10ABCDEF',name:'HDHomeRun',channels:[{number:'2.1',name:'Test HD',supported:true},{number:'4.1',name:'Second',supported:true},{number:'102.1',name:'ATSC 3',supported:false,reason:'ATSC 3.0 is not supported yet'}]}],favorites:[]},
    pages:[{id:'basement',name:'Basement dashboard',url:'https://example.test/basement'}, {id:'youtube',name:'Youtube',url:'https://example.test/youtube'}],
    tvs:[{id:'upstairs',name:'Upstairs',host:'192.0.2.1',port:7000}, {id:'cart',name:'Cart',host:'192.0.2.2',port:7000}],
    runtime:{browser:'ready',page_id:'youtube',control_mode:'native',error:null,audio_enabled:true,
      display:{width:1920,height:1080,fps:30},receivers:{cart:{state:'sending',error:null,audio:'active'}}},
  };
  page.on('pageerror', error => errors.push(error.message));
  await page.route('http://doubletake.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/state') {
      stateReads++;
      return route.fulfill({json:fixture});
    }
    if (url.pathname === '/api/preview') return route.fulfill({json:{password:'synthetic-preview'}});
    if (url.pathname === '/api/channels/favorite') { const body=route.request().postDataJSON(); fixture.channels.favorites=body.enabled?[body.device_id+':'+body.channel]:[]; return route.fulfill({json:fixture.channels}); }
    if (url.pathname.startsWith('/api/action/')) {
      const body = route.request().postDataJSON(), action = url.pathname.split('/').pop();
      actions.push({action,body});
      if (action === 'cast') {
        fixture.runtime.page_id = body.page_id;
        fixture.runtime.youtube = null; fixture.runtime.source_label = null;
        fixture.runtime.receivers = Object.fromEntries(body.tv_ids.map(id => [id,fixture.runtime.receivers[id] || {state:'pairing',error:null,audio:'starting'}]));
      } else if (action === 'open') {
        fixture.runtime.page_id = body.page_id; fixture.runtime.youtube = null; fixture.runtime.source_label = null;
      } else if (action === 'youtube') {
        fixture.runtime.page_id = null;
        fixture.runtime.source_label = body.mode === 'watch_later' ? 'Watch Later' : 'YouTube';
        fixture.runtime.youtube = {mode:body.mode,state:'loading'};
        if (body.tv_ids) fixture.runtime.receivers = Object.fromEntries(body.tv_ids.map(id => [id,fixture.runtime.receivers[id] || {state:'starting',error:null,audio:'starting'}]));
      } else if (action === 'channel') {
        const gate = channelGate;
        if (gate) {
          await gate.ready;
          if (gate.cancelled) return route.fulfill({status:409,json:{error:'Channel launch cancelled'}});
        }
        fixture.runtime.browser='closed'; fixture.runtime.source_kind='hdhomerun'; fixture.runtime.youtube=null;
        fixture.runtime.channel={device_id:body.device_id,number:body.channel,state:'playing'}; fixture.runtime.source_label='2.1 Test HD';
        fixture.runtime.receivers=Object.fromEntries(body.tv_ids.map(id=>[id,{state:'sending',audio:'active'}]));
      } else if (action === 'stop') {
        if (channelGate) { channelGate.cancelled=true; channelGate.release(); }
        if (body.tv_id) delete fixture.runtime.receivers[body.tv_id]; else fixture.runtime.receivers = {};
      } else if (action === 'pin') fixture.runtime.receivers[body.tv_id] = {state:'sending',error:null,audio:'active'};
      return route.fulfill({json:{ok:true}});
    }
    if (url.pathname === '/novnc/core/rfb.js') return route.fulfill({contentType:'text/javascript',body:
      'export default class RFB {constructor(el){const canvas=document.createElement("canvas");canvas.width=1920;canvas.height=1080;el.append(canvas);} addEventListener(name,callback){if(name==="connect")setTimeout(callback,0);}disconnect(){}focus(){}}'});
    const name = url.pathname === '/' ? 'index.html' : url.pathname.replace('/static/','');
    if (['index.html','app.js','style.css'].includes(name)) return route.fulfill({path:path.join(staticDir,name)});
    return route.fulfill({status:404,body:'Not found'});
  });
  async function afterPoll() {
    const previous = stateReads;
    const deadline = Date.now() + 6000;
    while (stateReads <= previous && Date.now() < deadline) await page.waitForTimeout(100);
    assert.ok(stateReads > previous, 'UI polling stopped');
    await page.waitForTimeout(100);
  }
  async function noOverflow() {
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Page overflowed viewport');
  }
  try {
    await page.goto('http://doubletake.test/');
    await page.waitForFunction(() => document.querySelector('#previewState').textContent.startsWith('Connected'));
    assert.equal(await page.locator('#pageChoice').inputValue(),'youtube');
    assert.equal(await page.getByRole('checkbox',{name:'Cart',exact:true}).isChecked(),true);
    assert.equal(await page.getByRole('checkbox',{name:'Upstairs',exact:true}).isChecked(),false);
    assert.match(await page.locator('#streamFormat').innerText(), /1920 × 1080 · 30 fps target/);
    await page.getByRole('button',{name:'Pause preview',exact:true}).click();
    await afterPoll();
    assert.equal(await page.locator('#screen canvas').count(),0,'Paused preview reconnected');
    assert.match(await page.locator('#previewState').innerText(),/Preview paused/);
    assert.equal(actions.length,0,'Pausing preview changed TV or browser playback');
    await page.getByRole('button',{name:'Resume preview',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#screen canvas'));
    await page.getByRole('checkbox',{name:'Upstairs',exact:true}).check();
    await page.locator('#pageChoice').selectOption('basement');
    await afterPoll();
    assert.equal(await page.getByRole('checkbox',{name:'Upstairs',exact:true}).isChecked(),true, 'Poll lost selected TV');
    assert.equal(await page.locator('#pageChoice').inputValue(),'basement','Poll lost selected page');
    await page.getByRole('button',{name:'Show on TVs',exact:true}).click();
    await page.getByRole('button',{name:'Enter code for Upstairs',exact:true}).waitFor();
    assert.deepEqual(actions.at(-1),{action:'cast',body:{page_id:'basement',tv_ids:['cart','upstairs']}});
    await noOverflow();
    if (outputDir) {
      fs.mkdirSync(outputDir,{recursive:true});
      await page.screenshot({path:path.join(outputDir,'multi-tv-745.png'),fullPage:true});
    }
    await page.setViewportSize({width:390,height:844});
    await noOverflow();
    if (outputDir) await page.screenshot({path:path.join(outputDir,'multi-tv-390.png'),fullPage:true});
    await page.getByRole('button',{name:'Enter code for Upstairs',exact:true}).click();
    assert.equal(await page.locator('#pairTarget').innerText(),'Pair Upstairs');
    assert.equal(await page.locator('#pairValue').getAttribute('type'),'password');
    await page.locator('#pairValue').fill('1234');
    await page.getByRole('button',{name:'Continue',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('#pairDialog').open);
    assert.deepEqual(actions.at(-1),{action:'pin',body:{tv_id:'upstairs',value:'1234'}});
    assert.equal(await page.locator('#pairValue').inputValue(),'');
    fixture.runtime.receivers.upstairs.audio = 'error';
    await afterPoll();
    assert.match(await page.locator('#receivers').innerText(),/Audio failed/);
    assert.equal(fixture.runtime.receivers.upstairs.state,'sending');
    await page.getByRole('button',{name:'Stop Upstairs',exact:true}).click();
    await page.getByRole('button',{name:'Stop Upstairs',exact:true}).waitFor({state:'detached'});
    assert.deepEqual(actions.at(-1),{action:'stop',body:{tv_id:'upstairs'}});
    assert.equal(fixture.runtime.receivers.cart.state,'sending');
    assert.equal(await page.getByRole('checkbox',{name:'Upstairs',exact:true}).isChecked(),false);
    // A second client's new receiver follows live state until this user edits.
    fixture.runtime.receivers.upstairs = {state:'error',error:'Could not connect to this TV.'};
    await afterPoll();
    assert.equal(await page.getByRole('checkbox',{name:'Upstairs',exact:true}).isChecked(),true);
    assert.match(await page.locator('#receivers').innerText(),/Could not connect to this TV/);
    await page.getByRole('checkbox',{name:'Cart',exact:true}).uncheck();
    await afterPoll();
    assert.equal(await page.getByRole('checkbox',{name:'Cart',exact:true}).isChecked(),false);
    // Explicit Stop all applies immediately even with an unsubmitted selection.
    await page.getByRole('button',{name:'Stop all',exact:true}).click();
    await page.locator('#receiverSection').waitFor({state:'hidden'});
    assert.deepEqual(actions.at(-1),{action:'stop',body:{}});
    assert.equal(await page.getByRole('checkbox',{name:'Upstairs',exact:true}).isChecked(),false);
    fixture.runtime.audio_enabled = false;
    await afterPoll();
    assert.match(await page.locator('#audioPreviewNote').innerText(),/TV audio is off/);
    // Password paste remains masked and clears on cancel after the UI redesign.
    await page.getByRole('button',{name:'Paste',exact:true}).click();
    await page.locator('#pasteValue').fill('synthetic cancelled text');
    assert.equal(await page.locator('#pasteValue').getAttribute('type'),'password');
    await page.getByRole('button',{name:'Cancel paste',exact:true}).click();
    assert.equal(await page.locator('#pasteValue').inputValue(),'');
    // YouTube drafts are independent of polling and the live shared source.
    fixture.runtime.audio_enabled = true;
    fixture.runtime.receivers = {cart:{state:'sending',error:null,audio:'active'}};
    await afterPoll();
    await page.getByRole('combobox',{name:'Source',exact:true}).selectOption('video');
    assert.equal(await page.getByRole('button',{name:'Open browser',exact:true}).isDisabled(),true);
    const videoURL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
    await page.getByLabel('YouTube video URL',{exact:true}).fill(videoURL);
    await page.getByRole('checkbox',{name:'Upstairs',exact:true}).check();
    await page.getByLabel('YouTube video URL',{exact:true}).focus();
    await afterPoll();
    assert.equal(await page.locator('#sourceChoice').inputValue(),'video');
    assert.equal(await page.locator('#youtubeURL').inputValue(),videoURL);
    assert.equal(await page.evaluate(() => document.activeElement.id),'youtubeURL','Polling stole source input focus');
    await page.getByRole('button',{name:'Open browser',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#youtubeState').textContent === 'YouTube · Loading');
    assert.deepEqual(actions.at(-1),{action:'youtube',body:{mode:'video',url:videoURL}});
    assert.deepEqual(Object.keys(fixture.runtime.receivers),['cart'],'Open browser changed receiver set');
    assert.equal(await page.getByRole('checkbox',{name:'Upstairs',exact:true}).isChecked(),true,'Open lost unsubmitted TV selection');
    assert.match(await page.locator('#sessionState').innerText(),/Sending to 1 TV/);
    assert.equal(await page.locator('#youtubeURL').inputValue(),videoURL);
    for (const [value,label] of [['playing','Playing'],['paused','Paused'],['finished','Finished'],['needs_interaction','Needs interaction'],['error','Needs attention']]) {
      fixture.runtime.youtube = {mode:'video',state:value,...(value==='needs_interaction'?{error:'Sign in to YouTube in the preview.'}:{})};
      await afterPoll();
      assert.equal(await page.locator('#youtubeState').innerText(),`YouTube · ${label}`);
      assert.match(await page.locator('#sessionState').innerText(),/Sending to 1 TV/,'Playback state replaced sender status');
      if (value==='needs_interaction') assert.equal(await page.locator('#youtubeDetail').innerText(),'Sign in to YouTube in the preview.');
    }
    await page.getByRole('combobox',{name:'Source',exact:true}).selectOption('watch_later');
    assert.equal(await page.getByRole('checkbox',{name:'Include partly watched videos',exact:true}).isChecked(),true);
    await page.getByRole('button',{name:'Show on TVs',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#sessionDetail').textContent.startsWith('Watch Later'));
    assert.deepEqual(actions.at(-1),{action:'youtube',body:{mode:'watch_later',resume:true,tv_ids:['cart','upstairs']}});
    await noOverflow();
    if (outputDir) await page.screenshot({path:path.join(outputDir,'youtube-watch-later-390.png'),fullPage:true});
    await page.getByRole('checkbox',{name:'Include partly watched videos',exact:true}).uncheck();
    await afterPoll();
    assert.equal(await page.getByRole('checkbox',{name:'Include partly watched videos',exact:true}).isChecked(),false);
    await page.getByRole('button',{name:'Open browser',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('#openBrowser').disabled);
    assert.deepEqual(actions.at(-1),{action:'youtube',body:{mode:'watch_later',resume:false}});
    await page.getByRole('combobox',{name:'Source',exact:true}).selectOption('video');
    assert.equal(await page.locator('#youtubeURL').inputValue(),videoURL,'Source switch cleared URL draft');
    await page.setViewportSize({width:745,height:1000});
    await page.getByRole('button',{name:'Show on TVs',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('#cast').disabled);
    assert.deepEqual(actions.at(-1),{action:'youtube',body:{mode:'video',url:videoURL,tv_ids:['cart','upstairs']}});
    await noOverflow();
    if (outputDir) await page.screenshot({path:path.join(outputDir,'youtube-video-745.png'),fullPage:true});
    await page.getByRole('combobox',{name:'Source',exact:true}).selectOption('page');
    await page.locator('#pageChoice').selectOption('youtube');
    await page.getByRole('button',{name:'Open browser',exact:true}).click();
    await page.locator('#youtubeStatus').waitFor({state:'hidden'});
    assert.deepEqual(actions.at(-1),{action:'open',body:{page_id:'youtube'}});
    fixture.runtime.page_id = null; fixture.runtime.source_label = 'Watch Later';
    fixture.runtime.youtube = {mode:'watch_later',state:'playing'};
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#sourceChoice').value === 'watch_later');
    assert.equal(await page.getByRole('checkbox',{name:'Include partly watched videos',exact:true}).isChecked(),true);
    await page.getByRole('combobox',{name:'Source',exact:true}).selectOption('hdhomerun');
    const countBeforeChannel=actions.length;
    await page.locator('#channelChoice').selectOption('10ABCDEF:4.1');
    await afterPoll();
    assert.equal(actions.length,countBeforeChannel,'Selecting a channel tuned immediately');
    assert.equal(await page.locator('#channelChoice').inputValue(),'10ABCDEF:4.1','Poll lost channel draft');
    assert.equal(await page.locator('#channelChoice option[value="10ABCDEF:102.1"]').evaluate(el => el.disabled),true);
    await page.locator('#channelSearch').fill('2.1');
    await page.locator('#channelChoice').selectOption('10ABCDEF:2.1');
    await page.getByRole('button',{name:'☆ Favorite',exact:true}).click();
    await page.getByRole('button',{name:'★ Favorite',exact:true}).waitFor();
    assert.equal(actions.length,countBeforeChannel,'Favoriting changed playback');
    await page.getByRole('checkbox',{name:'Upstairs',exact:true}).uncheck();
    await page.getByRole('button',{name:'Play channel',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#previewState').textContent.startsWith('HDHomeRun'));
    assert.deepEqual(actions.at(-1),{action:'channel',body:{device_id:'10ABCDEF',channel:'2.1',tv_ids:['cart']}});
    assert.equal(await page.locator('#screen canvas').count(),0,'Channel kept browser preview connected');
    assert.match(await page.locator('#previewEmpty').innerText(),/Channel video and sound/);
    await noOverflow();
    if(outputDir) await page.screenshot({path:path.join(outputDir,'channel-745.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844}); await noOverflow();
    await page.locator('#channelFields summary').click();
    await page.locator('#channelHost').fill('192.168.1.133');
    await noOverflow();
    if(outputDir) await page.screenshot({path:path.join(outputDir,'channel-390.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    // Stop remains available both when switching and on the first tune with
    // no active receivers. The cancelled response must not restore selection
    // or show a spurious failure after Stop has completed.
    for (let attempt=0;attempt<2;attempt++) {
      await page.getByRole('checkbox',{name:'Cart',exact:true}).check();
      channelGate={cancelled:false};
      channelGate.ready=new Promise(resolve=>channelGate.release=resolve);
      const before=actions.length;
      await page.getByRole('button',{name:'Play channel',exact:true}).click();
      await page.waitForFunction(() => document.querySelector('#cast').disabled);
      assert.equal(await page.getByRole('button',{name:'Stop all',exact:true}).isEnabled(),true,'Tuning disabled Stop');
      await page.getByRole('button',{name:'Stop all',exact:true}).click();
      await page.waitForFunction(() => !document.querySelector('#sourceChoice').disabled);
      assert.deepEqual(actions.slice(before).map(item=>item.action),['channel','stop']);
      assert.equal(await page.locator('#error').isVisible(),false,'Cancelled tuning showed a late error');
      assert.equal(await page.getByRole('checkbox',{name:'Cart',exact:true}).isChecked(),false);
      assert.equal(await page.getByRole('button',{name:'Stop all',exact:true}).isEnabled(),false);
      assert.deepEqual(fixture.runtime.receivers,{});
      channelGate=null;
    }
    console.log(JSON.stringify({initial_live_selection:true,poll_retains_edits:true,explicit_receiver_set:true,targeted_pin_and_stop:true,stop_all:true,cancel_pending_channel:true,per_tv_errors:true,per_tv_audio_failure:true,responsive_widths:[745,390],audio_switch_display:true,password_dialog_retained:true,youtube_drafts_and_focus:true,youtube_payloads:true,youtube_status_separate:true,watch_later_resume_default:true,saved_page_unchanged:true,initial_youtube_source:true}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
