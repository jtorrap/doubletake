// Disposable fixtures only. Every browser request is intercepted locally.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '/tmp/app-playwright/node_modules/playwright');
const extension = path.join(__dirname,'../service/youtube_extension');
const output = process.argv[2];
const ids = ['AAAAAAAAAAA','BBBBBBBBBBB','CCCCCCCCCCC','DDDDDDDDDDD','EEEEEEEEEEE'];
const watch = id => `https://www.youtube.com/watch?v=${id}`;
const event = () => ({listeners:[],addListener(fn){this.listeners.push(fn);},emit(...args){return this.listeners.map(fn=>fn(...args));}});
async function backgroundTests() {
  const posted=[],updated=[],sent=[], native={onMessage:event(),onDisconnect:event(),postMessage(value){posted.push(value);}};
  let delayedUpdate=null, delayedQuery=null;
  const chrome={runtime:{id:'fixture-extension',onMessage:event(),connectNative(){return native;}},tabs:{
    onUpdated:event(),onRemoved:event(),async query(){if(delayedQuery)await delayedQuery;return[{id:9}];},
    async update(id,value){updated.push({id,...value});if(delayedUpdate)await delayedUpdate;},
    async sendMessage(id,value,options){sent.push({id,value,options});return{ok:true};},
  }};
  const intervals=[];
  const context=vm.createContext({chrome,URL,Promise,setTimeout(){},setInterval(fn){intervals.push(fn);}});
  vm.runInContext(fs.readFileSync(path.join(extension,'background.js'),'utf8'),context);
  async function flush(){await vm.runInContext('serial',context);await Promise.resolve();}
  async function command(value){native.onMessage.emit(value);await flush();}
  const sender=(url,documentId='document-one',extra={})=>({id:chrome.runtime.id,frameId:0,documentLifecycle:'active',tab:{id:9},url,documentId,...extra});
  async function message(value,from){chrome.runtime.onMessage.emit(value,from,()=>{});await flush();}
  await command({id:1,action:'launch',launch_id:1,mode:'watch_later',resume:true});
  assert.equal(updated.at(-1).url,'https://www.youtube.com/playlist?list=WL');
  const playlist=sender(updated.at(-1).url);
  await message({type:'ready'},playlist);
  const queue={type:'queue',launch_id:1,index:0,items:[{id:ids[0],start:null},{id:ids[1],start:75},{id:ids[1],start:75},{id:ids[2],start:null}]};
  await message(queue,playlist);
  assert.equal(updated.at(-1).url,watch(ids[0]));
  const beforeDuplicate=updated.length;
  await message(queue,playlist);
  assert.equal(updated.length,beforeDuplicate,'Duplicate collection advanced queue');
  const first=sender(watch(ids[0]),'document-two');
  await message({type:'ready'},first);
  await message({type:'status',launch_id:1,index:0,state:'playing'},first);
  assert.equal(posted.at(-1).state,'playing');
  const beforeInvalid=posted.length;
  for(const invalid of [sender(watch(ids[0]),'old-document'),sender(watch(ids[0]),'document-two',{frameId:2}),sender('https://evil.test/watch?v='+ids[0]),sender(watch(ids[0]),'document-two',{id:'other-extension'}),sender(watch(ids[0]),'document-two',{documentLifecycle:'cached'})]) {
    await message({type:'status',launch_id:1,index:0,state:'paused'},invalid);
  }
  assert.equal(posted.length,beforeInvalid,'Untrusted or stale document reported status');
  const ended={type:'ended',launch_id:1,index:0,video_id:ids[0]};
  await message(ended,first); await message(ended,first);
  assert.equal(updated.length,beforeDuplicate+1,'Ended advanced more than once');
  assert.equal(updated.at(-1).url,watch(ids[1])+'&t=75');
  await message({type:'unavailable',launch_id:1,index:1,video_id:ids[1]},sender(watch(ids[1]),'document-three'));
  assert.equal(updated.at(-1).url,watch(ids[2]));
  const third=sender(watch(ids[2]),'document-four');
  await message({type:'ended',launch_id:1,index:2,video_id:ids[2]},third);
  assert.equal(posted.at(-1).state,'finished');
  await message({type:'status',launch_id:1,index:2,state:'playing'},third);
  assert.equal(posted.at(-1).state,'finished','Late status replaced finished');
  await command({id:2,action:'launch',launch_id:2,mode:'video',resume:true,url:watch(ids[3])});
  const beforeBadCommand=posted.filter(value=>value.type==='status').length;
  await command({id:3,action:'launch',launch_id:3,mode:'video',resume:true,url:'https://evil.test/watch?v='+ids[0]});
  assert.equal(posted.at(-1).ok,false);
  assert.equal(posted.filter(value=>value.type==='status').length,beforeBadCommand,'Bad command corrupted current launch');
  await command({id:4,action:'cancel'});
  const beforeCancel=sent.length;
  intervals.forEach(fn=>fn()); await flush();
  assert.equal(sent.length,beforeCancel,'Cancelled controller kept dispatching');
  await command({id:5,action:'launch',launch_id:5,mode:'watch_later',resume:true});
  let release;
  delayedUpdate=new Promise(resolve=>{release=resolve;});
  chrome.runtime.onMessage.emit({...queue,launch_id:5},playlist,()=>{});
  await Promise.resolve(); await Promise.resolve();
  native.onMessage.emit({id:6,action:'launch',launch_id:6,mode:'video',resume:true,url:watch(ids[4])});
  release(); delayedUpdate=null; await flush();
  assert.equal(updated.at(-1).url,watch(ids[4]),'Stale queue navigation won over new launch');
  const beforeStale=updated.length;
  await message({...ended,launch_id:5},first);
  assert.equal(updated.length,beforeStale);
  let releaseQuery;
  delayedQuery=new Promise(resolve=>{releaseQuery=resolve;});
  native.onMessage.emit({id:7,action:'launch',launch_id:7,mode:'video',resume:true,url:watch(ids[0])});
  await Promise.resolve();await Promise.resolve();
  native.onDisconnect.emit();
  assert.equal(vm.runInContext('active',context),null,'Disconnect waited for hung command before invalidating playback');
  releaseQuery();delayedQuery=null;await flush();
  assert.equal(updated.length,beforeStale,'Disconnected native launch resumed after pending Chrome request');
  intervals.forEach(fn=>fn()); await flush();
  assert.equal(updated.length,beforeStale);
  for(const value of posted.filter(item=>item.type==='status')) {
    assert.ok(Object.keys(value).every(key=>['type','launch_id','mode','state','error'].includes(key)),'Native status leaked page data');
  }
}
function row(id,percent=0,query='',title='Synthetic video') {
  return `<ytd-playlist-video-renderer style="display:block;height:100px"><a id="video-title" href="/watch?v=${id}${query}">${title}</a><ytd-thumbnail-overlay-resume-playback-renderer style="display:block;width:100px"><div id="progress" style="height:4px;width:${percent}%"></div></ytd-thumbnail-overlay-resume-playback-renderer></ytd-playlist-video-renderer>`;
}
const playerHTML='<style>body{margin:0}#outer{transform:translate(80px,60px);contain:paint;width:320px;height:180px}#movie_player{width:320px;height:180px;background:#14283c}video{background:#18324b}</style><div id="outer"><div id="movie_player"><div class="html5-video-container"><video></video></div></div></div>';
async function browserTests() {
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH || (process.platform==='darwin'?'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome':'/usr/bin/google-chrome'),args:process.platform==='linux'?['--no-sandbox']:[]});
  const errors=[];
  async function fixture(url,html) {
    const page=await browser.newPage({viewport:{width:960,height:540}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>route.request().resourceType()==='document'?route.fulfill({contentType:'text/html',body:html}):route.abort());
    await page.goto(url);
    await page.clock.install();
    await page.evaluate(()=>{
      window.fixtureMessages=[];window.fixtureListener=null;
      window.chrome={runtime:{id:'fixture-extension',onMessage:{addListener(fn){window.fixtureListener=fn;}},sendMessage(value){window.fixtureMessages.push(value);return Promise.resolve();}}};
      window.fixtureApply=plan=>window.fixtureListener({type:'apply',plan},{id:'fixture-extension'},()=>{});
      window.fixtureCancel=()=>window.fixtureListener({type:'cancel'},{id:'fixture-extension'},()=>{});
      const video=document.querySelector('video');
      if(video){
        window.media={readyState:4,currentTime:75,paused:true,ended:false,playCalls:0,pauseCalls:0};
        for(const key of ['readyState','currentTime','paused','ended'])Object.defineProperty(video,key,{get:()=>media[key],configurable:true});
        video.play=()=>{media.playCalls++;media.paused=false;return window.playPromise || Promise.resolve();};
        video.pause=()=>{media.pauseCalls++;media.paused=true;};
      }
    });
    await page.addStyleTag({path:path.join(extension,'player.css')});
    await page.addScriptTag({path:path.join(extension,'content.js')});
    return page;
  }
  const playPlan={launch_id:1,mode:'video',phase:'play',resume:true,index:0,video_id:ids[0]};
  const collectPlan={...playPlan,mode:'watch_later',phase:'collect',video_id:null};
  try {
    let page=await fixture('https://www.youtube.com/playlist?list=WL',row(ids[0],100)+row(ids[1],99)+row(ids[2],35,'&t=1m15s')+row(ids[3],0)+row(ids[4],0,'','[Private video]'));
    await page.evaluate(plan=>fixtureApply(plan),collectPlan);
    await page.clock.runFor(9000);
    let queues=await page.evaluate(()=>fixtureMessages.filter(value=>value.type==='queue'));
    assert.equal(queues.length,1);
    assert.deepEqual(queues[0].items,[{id:ids[2],start:75},{id:ids[3],start:null}]);
    assert.equal(await page.locator('ytd-playlist-video-renderer').count(),5,'Collection removed playlist items');
    await page.evaluate(plan=>fixtureApply(plan),{...collectPlan,launch_id:2,resume:false});
    await page.clock.runFor(9000);
    queues=await page.evaluate(()=>fixtureMessages.filter(value=>value.type==='queue'));
    assert.deepEqual(queues.at(-1).items,[{id:ids[3],start:null}]);
    await page.close();
    // A continuation replaces rows without changing total row count or height.
    page=await fixture('https://www.youtube.com/playlist?list=WL',row(ids[0],0));
    await page.evaluate(html=>setTimeout(()=>{document.querySelector('ytd-playlist-video-renderer').outerHTML=html;},2500),row(ids[1],20));
    await page.evaluate(plan=>fixtureApply(plan),collectPlan); await page.clock.runFor(11000);
    queues=await page.evaluate(()=>fixtureMessages.filter(value=>value.type==='queue'));
    assert.deepEqual(queues[0].items,[{id:ids[0],start:null},{id:ids[1],start:null}]);
    await page.close();
    page=await fixture('https://www.youtube.com/playlist?list=WL',row(ids[0],0)+row(ids[1],0));
    await page.evaluate(()=>setTimeout(()=>{document.querySelector('#progress').style.width='100%';},2000));
    await page.evaluate(plan=>fixtureApply(plan),collectPlan);await page.clock.runFor(10000);
    queues=await page.evaluate(()=>fixtureMessages.filter(value=>value.type==='queue'));
    assert.deepEqual(queues[0].items,[{id:ids[1],start:null}],'Late watched overlay left completed video in queue');
    await page.close();
    page=await fixture('https://www.youtube.com/playlist?list=WL','<ytd-background-promo-renderer style="display:block">Sign in to see your Watch Later videos</ytd-background-promo-renderer>');
    await page.evaluate(plan=>fixtureApply(plan),collectPlan);await page.clock.runFor(3500);
    assert.ok(await page.evaluate(()=>fixtureMessages.some(value=>value.state==='needs_interaction')));
    assert.equal(await page.evaluate(()=>fixtureMessages.some(value=>value.type==='queue')),false);
    await page.close();
    page=await fixture('https://www.youtube.com/playlist?list=WL','<ytd-message-renderer style="display:block">This playlist is empty</ytd-message-renderer>');
    await page.evaluate(plan=>fixtureApply(plan),collectPlan);
    assert.deepEqual(await page.evaluate(()=>fixtureMessages.find(value=>value.type==='queue').items),[]);
    await page.close();
    page=await fixture('https://www.youtube.com/playlist?list=WL','<ytd-continuation-item-renderer><tp-yt-paper-spinner style="display:block;width:20px;height:20px"></tp-yt-paper-spinner></ytd-continuation-item-renderer>');
    await page.evaluate(plan=>fixtureApply(plan),collectPlan);await page.clock.runFor(181000);
    assert.ok(await page.evaluate(()=>fixtureMessages.some(value=>value.state==='needs_interaction')),'Collection exceeded three-minute deadline');
    assert.equal(await page.evaluate(()=>fixtureMessages.some(value=>value.type==='queue')),false);
    await page.close();
    page=await fixture('https://www.youtube.com/playlist?list=WL',row(ids[0]));
    await page.evaluate(plan=>fixtureApply(plan),collectPlan);await page.clock.runFor(1000);
    await page.evaluate(()=>fixtureCancel());await page.clock.runFor(9000);
    assert.equal(await page.evaluate(()=>fixtureMessages.some(value=>value.type==='queue')),false,'Cancelled collector exported a queue');
    await page.close();
    page=await fixture(watch(ids[0]),playerHTML);
    await page.evaluate(plan=>fixtureApply(plan),playPlan);await page.clock.runFor(250);
    assert.equal(await page.evaluate(()=>media.playCalls),1,'Initial playback not started exactly once');
    assert.equal(await page.evaluate(()=>media.currentTime),75,'Companion replaced YouTube remembered position');
    const boxes=await page.evaluate(()=>['#movie_player','#movie_player video'].map(selector=>{const b=document.querySelector(selector).getBoundingClientRect();return{x:b.x,y:b.y,width:b.width,height:b.height};}));
    for(const box of boxes)assert.deepEqual(box,{x:0,y:0,width:960,height:540},'Player or video does not fill viewport');
    if(output){fs.mkdirSync(output,{recursive:true});await page.screenshot({path:path.join(output,'youtube-fullscreen.png')});}
    await page.evaluate(()=>{media.paused=true;});await page.clock.runFor(2000);
    assert.equal(await page.evaluate(()=>media.playCalls),1,'Controller undid user pause');
    assert.equal(await page.evaluate(()=>fixtureMessages.filter(value=>value.type==='status').at(-1).state),'paused');
    await page.evaluate(()=>{document.querySelector('#movie_player').classList.add('ad-showing');media.ended=true;});await page.clock.runFor(500);
    await page.evaluate(()=>document.querySelector('#movie_player').classList.remove('ad-showing'));await page.clock.runFor(500);
    assert.equal(await page.evaluate(()=>fixtureMessages.filter(value=>value.type==='ended').length),0,'Ad end advanced content queue');
    await page.evaluate(()=>{media.ended=false;media.paused=false;media.currentTime=80;});await page.clock.runFor(250);
    await page.evaluate(()=>{media.ended=true;media.paused=true;});await page.clock.runFor(1500);
    assert.equal(await page.evaluate(()=>fixtureMessages.filter(value=>value.type==='ended').length),1,'Content end did not advance exactly once');
    await page.evaluate(()=>fixtureCancel());
    assert.equal(await page.locator('html[data-doubletake-player]').count(),0);
    await page.evaluate(plan=>fixtureApply(plan),playPlan);await page.clock.runFor(500);
    assert.equal(await page.locator('html[data-doubletake-player]').count(),0,'Cancelled generation reactivated');
    await page.close();
    page=await fixture(watch(ids[0]),playerHTML);
    await page.evaluate(()=>fixtureListener({type:'cancel',launch_id:1},{id:'fixture-extension'},()=>{}));
    await page.evaluate(plan=>fixtureApply(plan),playPlan);await page.clock.runFor(500);
    assert.equal(await page.evaluate(()=>media.playCalls),0,'Delayed apply started after matching cancellation');
    await page.close();
    page=await fixture(watch(ids[0]),playerHTML);
    await page.evaluate(()=>{window.playPromise=new Promise((_resolve,reject)=>{window.rejectPlay=reject;});});
    await page.evaluate(plan=>fixtureApply(plan),playPlan);
    await page.evaluate(()=>{fixtureCancel();rejectPlay(new Error('Synthetic rejection'));});
    await page.clock.runFor(500);
    assert.equal(await page.evaluate(()=>fixtureMessages.some(value=>value.state==='needs_interaction')),false,'Stale play rejection changed cancelled status');
    await page.close();
    page=await fixture(watch(ids[0]),playerHTML+'<yt-playability-error-supported-renderers style="display:block">This video is unavailable</yt-playability-error-supported-renderers>');
    await page.evaluate(plan=>fixtureApply(plan),playPlan);await page.clock.runFor(1500);
    assert.equal(await page.evaluate(()=>fixtureMessages.filter(value=>value.type==='unavailable').length),1);
    await page.close();
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
}
(async()=>{await backgroundTests();await browserTests();console.log(JSON.stringify({native_command_validation:true,private_status_only:true,stale_document_rejected:true,serialized_queue_navigation:true,duplicate_end_guard:true,playlist_order_and_progress:true,paginated_collection:true,no_history_mutation:true,sign_in_status:true,cancelled_generation:true,autoplay_and_remembered_position:true,user_pause_retained:true,ad_end_guard:true,fullscreen_player_and_video:true,unavailable_skip:true}));})().catch(error=>{console.error(error);process.exit(1);});
