// This private companion never exports titles, account information, cookies,
// playlist contents, or video URLs to the native bridge.
const VIDEO = /^[A-Za-z0-9_-]{11}$/;
const states = new Set(['loading','playing','paused','finished','needs_interaction','error']);
let port = null, active = null, serial = Promise.resolve(), connectionGeneration = 0;
function enqueue(operation) {
  const result = serial.then(operation);
  serial = result.catch(()=>{});
  return result;
}
function post(value) { try { port?.postMessage(value); } catch {} }
function report(state, error, plan=active) {
  if (!plan || active !== plan || !states.has(state)) return;
  plan.state = state;
  post({type:'status',launch_id:plan.launch_id,mode:plan.mode,state,...(error ? {error:true} : {})});
}
function isYouTube(url) {
  try { return new URL(url).origin === 'https://www.youtube.com'; } catch { return false; }
}
function validVideo(url) {
  if (!isYouTube(url)) return false;
  const value = new URL(url);
  return value.pathname === '/watch' && VIDEO.test(value.searchParams.get('v') || '');
}
function matchingPage(url, plan) {
  if (!isYouTube(url)) return false;
  const value = new URL(url);
  return plan.phase === 'collect' ? value.pathname === '/playlist' && value.searchParams.get('list') === 'WL' :
    value.pathname === '/watch' && value.searchParams.get('v') === plan.video_id;
}
async function dispatch(plan=active) {
  if (!plan || active !== plan) return;
  try {
    await chrome.tabs.sendMessage(plan.tabId, {type:'apply',plan:{
      launch_id:plan.launch_id,mode:plan.mode,phase:plan.phase,resume:plan.resume,
      index:plan.index,video_id:plan.video_id,state:plan.state,
    }}, ...(plan.documentId ? [{documentId:plan.documentId}] : []));
  } catch { /* A document navigation temporarily has no content script. */ }
}
async function next(plan) {
  if (active !== plan) return;
  plan.index++;
  if (plan.index >= plan.queue.length) {
    plan.phase = 'finished'; report('finished',false,plan); await dispatch(plan); return;
  }
  plan.phase = 'play'; plan.documentId = null;
  const item = plan.queue[plan.index]; plan.video_id = item.id;
  report('loading',false,plan);
  // Omit list=WL so YouTube's playlist/recommendations do not own progression.
  const url = new URL('https://www.youtube.com/watch');
  url.searchParams.set('v',item.id);
  if (Number.isInteger(item.start) && item.start > 0) url.searchParams.set('t',String(item.start));
  await chrome.tabs.update(plan.tabId,{url:url.href,active:true});
}
async function cancel() {
  const previous = active; active = null;
  if (previous) await chrome.tabs.sendMessage(previous.tabId,{type:'cancel',launch_id:previous.launch_id}).catch(()=>{});
}
async function command(value,generation=connectionGeneration) {
  if (!value || !Number.isSafeInteger(value.id)) return;
  let launched = null;
  try {
    if (value.action === 'cancel') await cancel();
    else if (value.action === 'launch') {
      if (!Number.isSafeInteger(value.launch_id) || value.launch_id < 1 ||
          !['video','watch_later'].includes(value.mode) || typeof value.resume !== 'boolean' ||
          (value.mode === 'video' && !validVideo(value.url))) throw Error();
      const tabs = await chrome.tabs.query({active:true,lastFocusedWindow:true});
      if (generation !== connectionGeneration || !port) return;
      if (tabs.length !== 1 || !Number.isInteger(tabs[0].id)) throw Error();
      await cancel();
      if (generation !== connectionGeneration || !port) return;
      active = launched = {launch_id:value.launch_id,mode:value.mode,resume:value.resume,tabId:tabs[0].id,
        phase:value.mode === 'watch_later' ? 'collect' : 'play',index:0,queue:[],documentId:null,
        video_id:value.mode === 'video' ? new URL(value.url).searchParams.get('v') : null};
      report('loading');
      await chrome.tabs.update(active.tabId,{url:value.mode === 'video' ? value.url :
        'https://www.youtube.com/playlist?list=WL',active:true});
    } else throw Error();
    if (generation === connectionGeneration) post({type:'reply',id:value.id,ok:true});
  } catch {
    if (generation !== connectionGeneration) return;
    post({type:'reply',id:value.id,ok:false});
    // A malformed new command must not corrupt a previously running launch.
    if (launched) report('error',true,launched);
  }
}
function connect() {
  try {
    const connection = chrome.runtime.connectNative('com.doubletake.youtube');
    port = connection;
    connection.onMessage.addListener(value => {
      const generation=connectionGeneration;
      void enqueue(()=>port === connection ? command(value,generation) : undefined);
    });
    connection.onDisconnect.addListener(()=>{
      void chrome.runtime.lastError;
      if (port !== connection) return;
      port = null; connectionGeneration++;
      // Invalidate synchronously, even if a Chrome request is holding the
      // command queue. A lost worker must not own future playback transitions.
      const previous=active; active=null;
      if (previous) void chrome.tabs.sendMessage(previous.tabId,{type:'cancel',launch_id:previous.launch_id}).catch(()=>{});
      setTimeout(connect,2000);
    });
    post({type:'hello'});
  } catch { setTimeout(connect,2000); }
}
async function contentMessage(value,sender) {
  const plan = active;
  if (sender.id !== chrome.runtime.id || !plan || sender.tab?.id !== plan.tabId ||
      sender.frameId !== 0 || (sender.documentLifecycle && sender.documentLifecycle !== 'active') ||
      !matchingPage(sender.url,plan)) return;
  if (value?.type === 'ready') {
    plan.documentId = sender.documentId || null; await dispatch(plan); return;
  }
  if (value?.launch_id !== plan.launch_id || value.index !== plan.index ||
      (plan.documentId && sender.documentId !== plan.documentId)) return;
  if (value.type === 'queue' && plan.phase === 'collect') {
    if (!Array.isArray(value.items) || value.items.length > 5000 ||
        value.items.some(item=>!item || !VIDEO.test(item.id) ||
          (item.start !== null && (!Number.isInteger(item.start) || item.start < 0 || item.start > 604800)))) {
      report('error',true,plan); return;
    }
    const seen = new Set();
    plan.queue = value.items.filter(item=>!seen.has(item.id) && seen.add(item.id));
    plan.index = -1; plan.phase = 'transition'; await next(plan);
  } else if (value.type === 'ended' && plan.phase === 'play' && value.video_id === plan.video_id) {
    plan.phase = 'transition';
    if (plan.mode === 'watch_later') await next(plan);
    else { plan.phase = 'finished'; report('finished',false,plan); await dispatch(plan); }
  } else if (value.type === 'unavailable' && plan.phase === 'play' && value.video_id === plan.video_id) {
    plan.phase = 'transition';
    if (plan.mode === 'watch_later') await next(plan);
    else report('needs_interaction',true,plan);
  } else if (value.type === 'status' && ['collect','play'].includes(plan.phase) && states.has(value.state) && value.state !== 'finished') {
    report(value.state,value.error,plan);
  }
}
chrome.runtime.onMessage.addListener((value,sender,respond)=>{
  const plan=active;
  void enqueue(()=>contentMessage(value,sender)).then(()=>respond({ok:true}),()=>{
    report('error',true,plan); respond({ok:false});
  });
  return true;
});
chrome.tabs.onUpdated.addListener((id,change)=>{
  const plan = active;
  if (plan?.tabId !== id) return;
  void enqueue(async()=>{
    if (active !== plan) return;
    if (change.url && !matchingPage(change.url,plan)) {report('needs_interaction',true,plan);return;}
    if (change.status === 'complete') await dispatch(plan);
  });
});
chrome.tabs.onRemoved.addListener(id=>{
  void enqueue(()=>{if(active?.tabId===id){report('finished');active=null;}});
});
setInterval(()=>{const plan=active;if(plan)void enqueue(()=>dispatch(plan));},2000);
connect();
