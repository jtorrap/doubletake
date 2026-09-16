import RFB from '../novnc/core/rfb.js';

const $ = id => document.getElementById(id);
let state = null, csrf = '', busy = false, rfb = null, connecting = false;
let editing = null, removing = null, pairingTV = null;
let previewPaused = false;
let selectedTVs = new Set(), selectionDirty = false, pageSelectionDirty = false;
let tvChoicesSignature = '', receiversSignature = '';
const labels = {closed:'Browser closed', starting:'Starting browser', ready:'Browser ready', error:'Needs attention'};
const receiverLabels = {starting:'Connecting', pairing:'Pairing required', sending:'Sending', error:'Needs attention'};
const audioLabels = {starting:'Audio starting',active:'Audio active',unavailable:'Audio unavailable',error:'Audio failed',disabled:'Audio off'};

function receivers() { return state?.runtime.receivers || {}; }
function tvName(id) { return state?.tvs.find(item => item.id === id)?.name || 'TV'; }
function sameSelection(ids) { return ids.length === selectedTVs.size && ids.every(id => selectedTVs.has(id)); }

async function api(path, body, method='POST') {
  const response = await fetch(new URL(path, document.baseURI), {method, credentials:'same-origin', headers:{'Content-Type':'application/json','X-Doubletake-CSRF':csrf}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Refresh the app and try again.');
  return data;
}
function showError(message) { $('error').textContent = message || ''; $('error').hidden = !message; }
async function run(operation) {
  if (busy) return;
  busy = true; showError(''); controls();
  try { await operation(); await refresh(); }
  catch (error) { showError(error.message); }
  finally { busy = false; controls(); }
}
function options(select, values, empty) {
  const selected = select.value;
  select.replaceChildren();
  if (!values.length) select.add(new Option(empty, ''));
  for (const value of values) select.add(new Option(value.name, value.id));
  if (values.some(item => item.id === selected)) select.value = selected;
}
function controls() {
  const ready = state?.runtime.browser === 'ready';
  $('pageChoice').disabled = busy;
  $('openBrowser').disabled = busy || !$('pageChoice').value;
  $('cast').disabled = busy || !$('pageChoice').value || !selectedTVs.size;
  $('stop').disabled = busy || !Object.keys(receivers()).length;
  $('closeBrowser').disabled = busy || !ready;
  $('fullscreen').disabled = !ready;
  $('pausePreview').disabled = !ready;
  $('pausePreview').textContent = previewPaused ? 'Resume preview' : 'Pause preview';
  $('pasteText').disabled = busy || !ready || !rfb;
  $('checkVideo').disabled = busy || !ready;
  document.querySelectorAll('[data-browser]').forEach(button => button.disabled = busy || !ready);
  document.querySelectorAll('[data-receiver-action], #tvChoices input').forEach(control => control.disabled = busy);
  const changed = !sameSelection(Object.keys(receivers()));
  const shared = state?.runtime.audio_enabled === false ? 'All selected TVs share one browser page.' : 'All selected TVs share this page and its audio.';
  $('selectionHint').textContent = changed && Object.keys(receivers()).length ?
    `Selection changed. Show on TVs applies this set and disconnects unchecked TVs. ${shared}` :
    `${shared} Apply your selection with Show on TVs.`;
}
function renderTVChoices() {
  const signature = JSON.stringify(state.tvs.map(({id,name}) => [id,name]));
  if (signature !== tvChoicesSignature) {
    tvChoicesSignature = signature;
    $('tvChoices').replaceChildren();
    if (!state.tvs.length) {
      const empty = document.createElement('span'); empty.className = 'empty-choice'; empty.textContent = 'Add a TV below';
      $('tvChoices').append(empty);
    }
    for (const tv of state.tvs) {
      const label = document.createElement('label'); label.className = 'tv-choice';
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.value = tv.id;
      const name = document.createElement('span'); name.textContent = tv.name;
      checkbox.addEventListener('change', () => {
        selectionDirty = true;
        if (checkbox.checked) selectedTVs.add(tv.id); else selectedTVs.delete(tv.id);
        controls();
      });
      label.append(checkbox,name); $('tvChoices').append(label);
    }
  }
  $('tvChoices').querySelectorAll('input').forEach(checkbox => { checkbox.checked = selectedTVs.has(checkbox.value); });
}
function startPairing(id) {
  pairingTV = id; $('pairTarget').textContent = `Pair ${tvName(id)}`;
  $('pairValue').value = ''; $('pairError').textContent = '';
  $('pairDialog').showModal(); $('pairValue').focus();
}
function clearPairing() { $('pairValue').value = ''; $('pairError').textContent = ''; pairingTV = null; }
function renderReceivers() {
  const entries = Object.entries(receivers());
  $('receiverSection').hidden = !entries.length;
  const signature = JSON.stringify(entries.map(([id,receiver]) => [id,tvName(id),receiver.state,receiver.error,receiver.audio,receiver.performance,receiver.performance_age_seconds > 12]));
  if (signature !== receiversSignature) {
    receiversSignature = signature;
    $('receivers').replaceChildren();
    for (const [id,receiver] of entries) {
      const row = document.createElement('div'); row.className = 'receiver card';
      const info = document.createElement('div'); info.className = 'receiver-info';
      const name = document.createElement('strong'); name.textContent = tvName(id);
      const status = document.createElement('span'); status.className = `badge ${receiver.state === 'sending' ? 'good' : receiver.state === 'error' ? 'bad' : 'wait'}`;
      status.textContent = receiverLabels[receiver.state] || receiver.state;
      const title = document.createElement('div'); title.className = 'receiver-title'; title.append(name,status);
      if (receiver.audio && audioLabels[receiver.audio]) {
        const audio = document.createElement('span');
        audio.className = `badge ${receiver.audio === 'active' ? 'good' : ['unavailable','error'].includes(receiver.audio) ? 'bad' : receiver.audio === 'starting' ? 'wait' : ''}`;
        audio.textContent = audioLabels[receiver.audio];
        if (receiver.audio === 'active') audio.title = 'Audio capture is active. Confirm sound on the TV.';
        title.append(audio);
      }
      info.append(title);
      if (receiver.performance) {
        const performance = document.createElement('p'); performance.className = 'receiver-error';
        const metrics = receiver.performance, stale = receiver.performance_age_seconds > 12;
        performance.textContent = stale ? 'Waiting for fresh video measurements…' :
          `${metrics.sent_fps.toFixed(1)} fps sent · ${receiver.encoder === 'vaapi' ? 'Intel GPU' : 'Software'} · ${Math.round(metrics.source_age_mean_ms)} ms capture to send`;
        performance.title = 'Measures encoded frames sent, not frames displayed by the TV.';
        info.append(performance);
      }
      if (receiver.error) { const detail = document.createElement('p'); detail.className = 'receiver-error'; detail.textContent = receiver.error; info.append(detail); }
      const actions = document.createElement('div'); actions.className = 'actions';
      if (receiver.state === 'pairing') {
        const pair = document.createElement('button'); pair.textContent = 'Enter code'; pair.className = 'primary';
        pair.dataset.receiverAction = 'pair'; pair.setAttribute('aria-label',`Enter code for ${tvName(id)}`);
        pair.onclick = () => startPairing(id); actions.append(pair);
      }
      const stop = document.createElement('button'); stop.textContent = 'Stop'; stop.className = 'quiet'; stop.dataset.receiverAction = 'stop';
      stop.setAttribute('aria-label',`Stop ${tvName(id)}`);
      stop.onclick = () => run(async () => { await api('api/action/stop',{tv_id:id}); selectedTVs.delete(id); });
      actions.append(stop); row.append(info,actions); $('receivers').append(row);
    }
  }
  if (pairingTV && receivers()[pairingTV]?.state !== 'pairing') $('pairDialog').close();
}
function renderItems(kind) {
  const list = $(kind); list.replaceChildren();
  if (!state[kind].length) { const empty = document.createElement('p'); empty.className='empty-list'; empty.textContent=kind==='pages'?'Save pages you want to open on your TV.':'Add an Apple TV by name and address, or find it on the network.'; list.append(empty); }
  for (const item of state[kind]) {
    const row=document.createElement('div'); row.className='item';
    const info=document.createElement('div'); info.className='iteminfo';
    const name=document.createElement('strong'); name.textContent=item.name;
    const detail=document.createElement('small'); detail.textContent=kind==='pages'?new URL(item.url).host:`${item.host}:${item.port}`;
    info.append(name,detail);
    const buttons=document.createElement('div'); buttons.className='actions';
    for (const [label, action] of [['Edit',()=>edit(kind,item)],['Remove',()=>remove(kind,item)]]) {
      const button=document.createElement('button'); button.className='quiet'; button.textContent=label; button.addEventListener('click',action); buttons.append(button);
    }
    row.append(info,buttons); list.append(row);
  }
}
async function refresh() {
  state = await api('api/state', undefined, 'GET'); csrf = state.csrf;
  options($('pageChoice'),state.pages,'Add a page below');
  if (!pageSelectionDirty && state.pages.some(item => item.id === state.runtime.page_id)) $('pageChoice').value = state.runtime.page_id;
  selectedTVs = new Set([...selectedTVs].filter(id => state.tvs.some(tv => tv.id === id)));
  if (!selectionDirty) selectedTVs = new Set(Object.keys(receivers()).filter(id => state.tvs.some(tv => tv.id === id)));
  renderTVChoices(); renderReceivers();
  renderItems('pages'); renderItems('tvs');
  $('version').textContent = `Doubletake Browser ${state.version} · ${state.runtime.control_mode==='native'?'Standard browser':'Diagnostic browser'}`;
  const display = state.runtime.display;
  $('streamFormat').textContent = display ? `${display.width} × ${display.height} · ${display.fps} fps target` : '';
  $('audioPreviewNote').textContent = state.runtime.audio_enabled === false ?
    'TV audio is off in app Configuration. Preview is silent.' : 'Browser audio plays on the TVs. Preview is silent.';
  $('mqttState').textContent = state.mqtt_connected?'Home Assistant connected':'Home Assistant controls reconnecting';
  $('mqttState').className = `badge ${state.mqtt_connected?'good':'wait'}`;
  const current=state.runtime, page=state.pages.find(item=>item.id===current.page_id), active=Object.entries(receivers());
  const sending=active.filter(([,receiver])=>receiver.state==='sending').length;
  const attention=active.some(([,receiver])=>['pairing','error'].includes(receiver.state)||['unavailable','error'].includes(receiver.audio));
  $('sessionState').textContent=sending?`Sending to ${sending} ${sending===1?'TV':'TVs'}`:attention?'TVs need attention':active.length?'Connecting to TVs':labels[current.browser];
  $('sessionState').className=`badge ${attention?'wait':sending?'good':''}`;
  $('sessionDetail').textContent=current.error || (page?`${page.name}${active.length?' → '+active.map(([id])=>tvName(id)).join(', '):''}`:'Open a page to sign in, click around, or start a TV view.');
  $('previewLabel').textContent=page?.name||'Browser preview';
  $('previewEmpty').hidden=current.browser==='ready';
  if (current.browser==='ready' && !previewPaused && !rfb && !connecting) connectPreview();
  if (current.browser!=='ready' && rfb) { rfb.disconnect(); rfb=null; $('screen').replaceChildren(); }
  if (current.browser!=='ready') $('previewState').textContent=labels[current.browser]||current.browser;
  controls();
}
async function connectPreview() {
  if (previewPaused) return;
  connecting=true;
  try {
    const auth=await api('api/preview',undefined,'GET');
    if (previewPaused) return;
    const url=new URL('ws/preview',document.baseURI); url.protocol=location.protocol==='https:'?'wss:':'ws:';
    $('screen').replaceChildren();
    const connection=new RFB($('screen'),url.href,{credentials:{password:auth.password},shared:true,wsProtocols:['binary','doubletake.'+csrf]});
    rfb=connection; connection.scaleViewport=true; connection.resizeSession=false; connection.showDotCursor=false;
    connection.addEventListener('connect',()=>{$('previewState').textContent='Connected · Click the browser to type';});
    connection.addEventListener('disconnect',()=>{if(rfb===connection){rfb=null;$('previewState').textContent='Preview disconnected · reconnecting';}});
    connection.addEventListener('securityfailure',()=>{$('previewState').textContent='Preview authentication failed';});
  } catch { $('previewState').textContent='Connecting to preview…'; }
  finally { connecting=false; }
}
function edit(kind,item={}) {
  editing={kind,id:item.id}; $('editorError').textContent='';
  $('editTitle').textContent=(item.id?'Edit ':'Add ')+(kind==='pages'?'page':'TV');
  $('itemName').value=item.name||''; $('itemURL').value=item.url||''; $('itemHost').value=item.host||''; $('itemPort').value=item.port||7000;
  $('urlLabel').hidden=kind!=='pages'; $('itemURL').required=kind==='pages';
  $('hostLabel').hidden=$('portLabel').hidden=kind!=='tvs'; $('itemHost').required=kind==='tvs';
  $('editor').showModal(); $('itemName').focus();
}
function remove(kind,item) {
  removing={kind,item}; $('removeTitle').textContent=`Remove ${item.name}?`;
  $('removeDetail').textContent='This removes the saved item and its Home Assistant controls. An active view using it will stop. Saved browser login and pairing files are retained.';
  $('removeDialog').showModal();
}
$('removeDialog').addEventListener('close',()=>{if($('removeDialog').returnValue==='remove'&&removing){const {kind,item}=removing;run(()=>api(`api/settings/${kind}/${item.id}`,{},'DELETE'));}});
$('editForm').addEventListener('submit',async event=>{
  event.preventDefault(); const {kind,id}=editing;
  const item={name:$('itemName').value,...(kind==='pages'?{url:$('itemURL').value}:{host:$('itemHost').value,port:Number($('itemPort').value)})};
  try{const saved=await api(`api/settings/${kind}${id?'/'+id:''}`,item,id?'PUT':'POST'); $('editor').close();await refresh();
    if(kind==='pages'){$('pageChoice').value=saved.id;pageSelectionDirty=true;}
    else if(!id){selectedTVs.add(saved.id);selectionDirty=true;renderTVChoices();}
    controls();}
  catch(error){$('editorError').textContent=error.message;}
});
$('cancelEdit').onclick=()=>$('editor').close();
$('addPage').onclick=()=>edit('pages'); $('addTV').onclick=()=>edit('tvs');
$('pageChoice').onchange=()=>{pageSelectionDirty=true;controls();};
$('openBrowser').onclick=()=>run(async()=>{await api('api/action/open',{page_id:$('pageChoice').value});pageSelectionDirty=false;});
$('cast').onclick=()=>run(async()=>{await api('api/action/cast',{page_id:$('pageChoice').value,tv_ids:[...selectedTVs]});selectionDirty=false;pageSelectionDirty=false;});
$('stop').onclick=()=>run(async()=>{await api('api/action/stop',{});selectedTVs.clear();selectionDirty=false;});
$('closeBrowser').onclick=()=>run(async()=>{await api('api/action/close',{});selectedTVs.clear();selectionDirty=false;});
document.querySelectorAll('[data-browser]').forEach(button=>button.onclick=()=>run(()=>api('api/action/browser',{action:button.dataset.browser})));
$('fullscreen').onclick=()=>$('viewport').requestFullscreen();
$('pausePreview').onclick=()=>{
  previewPaused=!previewPaused;
  if (previewPaused) {
    const connection=rfb; rfb=null; connection?.disconnect();
    $('screen').replaceChildren();
    $('previewState').textContent='Preview paused · Browser and TVs keep playing';
  } else if (!rfb && !connecting) connectPreview();
  controls();
};
$('pasteText').onclick=()=>{
  $('pasteValue').value=''; $('pasteError').textContent='';
  $('pasteDialog').showModal(); $('pasteValue').focus();
};
function clearPaste() {
  $('pasteValue').value=''; $('pasteError').textContent='';
}
$('cancelPaste').onclick=$('cancelPasteFooter').onclick=()=>{
  clearPaste(); $('pasteDialog').close();
};
$('pasteDialog').addEventListener('cancel',clearPaste);
$('pasteDialog').addEventListener('close',clearPaste);
$('pasteForm').addEventListener('submit',async event=>{
  event.preventDefault();
  let value=$('pasteValue').value;
  $('pasteValue').value=''; $('sendPaste').disabled=true;
  try {
    await api('api/action/insert_text',{value});
    $('pasteDialog').close(); rfb?.focus();
  } catch {
    $('pasteError').textContent='Text could not be sent. Click the field in the preview and try again.';
  } finally {
    value=''; $('sendPaste').disabled=false;
  }
});
$('cancelPair').onclick=()=>$('pairDialog').close();
$('pairDialog').addEventListener('cancel',clearPairing);
$('pairDialog').addEventListener('close',clearPairing);
$('pairForm').addEventListener('submit',async event=>{
  event.preventDefault(); const tv_id=pairingTV; let value=$('pairValue').value;
  $('pairValue').value=''; $('sendPair').disabled=true;
  try { await api('api/action/pin',{tv_id,value}); $('pairDialog').close(); await refresh(); }
  catch { $('pairError').textContent='The code could not be sent. Check this TV and try again.'; }
  finally { value=''; $('sendPair').disabled=false; }
});
$('discover').onclick=async()=>{
  $('discover').disabled=true; $('discovered').textContent='Looking for Apple TVs…'; $('discovery').showModal();
  try{const result=await api('api/discover',{});$('discovered').replaceChildren();if(!result.tvs.length)$('discovered').textContent='No Apple TVs found. You can still add a TV by address.';
    for(const tv of result.tvs){const row=document.createElement('div');row.className='item';const label=document.createElement('div');label.className='iteminfo';const name=document.createElement('strong');name.textContent=tv.name;const detail=document.createElement('small');detail.textContent=tv.host+' · '+tv.model;label.append(name,detail);const button=document.createElement('button');button.textContent='Add';button.onclick=()=>{$('discovery').close();edit('tvs',tv);};row.append(label,button);$('discovered').append(row);}
  }catch(error){$('discovered').textContent=error.message;}finally{$('discover').disabled=false;}
};
$('closeDiscovery').onclick=()=>$('discovery').close();
$('closeVideo').onclick=()=>$('videoDialog').close();
$('checkVideo').onclick=()=>run(async()=>{
  $('videoSummary').textContent='Checking video decoding…'; $('videoDetails').textContent='';
  $('videoDialog').showModal();
  try {
    const result=await api('api/diagnostics',{});
    $('videoScope').textContent=result.browser_inspection_available?'Browser video details are available.':
      'GPU activity includes browser decoding and TV encoding. Sender measurements count encoded frames; they do not measure frames displayed by the TV.';
    $('videoSummary').textContent=result.video_engine_active?'GPU video engine active':
      !result.hardware_decoding_enabled?'Hardware decoding is switched off':
      !result.render_nodes.length?'No accessible GPU found':
      !result.vaapi_ready?'GPU driver could not initialize':
      'GPU available · active hardware decoding not confirmed';
    $('videoDetails').textContent=JSON.stringify(result,null,2);
  } catch (error) { $('videoSummary').textContent='Video check unavailable. Try again.'; throw error; }
});
async function poll(){try{await refresh();}catch{showError('The app connection is unavailable. Retrying…');}setTimeout(poll,2000);}
poll();
