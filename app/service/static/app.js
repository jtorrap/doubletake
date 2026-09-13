import RFB from '../novnc/core/rfb.js';

const $ = id => document.getElementById(id);
let state = null, csrf = '', busy = false, rfb = null, connecting = false;
let editing = null, removing = null;
const labels = {closed:'Browser closed', starting:'Starting browser', ready:'Browser ready', error:'Needs attention'};

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
  $('openBrowser').disabled = busy || !$('pageChoice').value;
  $('cast').disabled = busy || !$('pageChoice').value || !$('tvChoice').value;
  $('stop').disabled = busy || !state?.runtime.tv_id;
  $('closeBrowser').disabled = busy || !ready;
  $('fullscreen').disabled = !ready;
  $('pasteText').disabled = busy || !ready || !rfb;
  $('checkVideo').disabled = busy || !ready;
  document.querySelectorAll('[data-browser]').forEach(button => button.disabled = busy || !ready);
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
  options($('pageChoice'),state.pages,'Add a page below'); options($('tvChoice'),state.tvs,'Add a TV below');
  renderItems('pages'); renderItems('tvs');
  $('version').textContent = `Doubletake Browser ${state.version}`;
  $('mqttState').textContent = state.mqtt_connected?'Home Assistant connected':'Home Assistant controls reconnecting';
  $('mqttState').className = `badge ${state.mqtt_connected?'good':'wait'}`;
  const current=state.runtime, tv=state.tvs.find(item=>item.id===current.tv_id), page=state.pages.find(item=>item.id===current.page_id);
  $('sessionState').textContent=current.airplay==='sending'?'Sending to '+(tv?.name||'TV'):current.airplay==='pairing'?'TV pairing required':current.airplay==='starting'?'Connecting to TV':labels[current.browser];
  $('sessionState').className=`badge ${current.airplay==='sending'?'good':current.airplay==='pairing'?'wait':''}`;
  $('sessionDetail').textContent=current.error || (page?`${page.name}${tv?' → '+tv.name:''}`:'Open a page to sign in, click around, or start a TV view.');
  $('pairForm').hidden=current.airplay!=='pairing';
  $('previewLabel').textContent=page?.name||'Browser preview';
  $('previewEmpty').hidden=current.browser==='ready';
  if (current.browser==='ready' && !rfb && !connecting) connectPreview();
  if (current.browser!=='ready' && rfb) { rfb.disconnect(); rfb=null; $('screen').replaceChildren(); }
  if (current.browser!=='ready') $('previewState').textContent=labels[current.browser]||current.browser;
  controls();
}
async function connectPreview() {
  connecting=true;
  try {
    const auth=await api('api/preview',undefined,'GET');
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
  try{const saved=await api(`api/settings/${kind}${id?'/'+id:''}`,item,id?'PUT':'POST'); $('editor').close();await refresh();$(kind==='pages'?'pageChoice':'tvChoice').value=saved.id;controls();}
  catch(error){$('editorError').textContent=error.message;}
});
$('cancelEdit').onclick=()=>$('editor').close();
$('addPage').onclick=()=>edit('pages'); $('addTV').onclick=()=>edit('tvs');
$('pageChoice').onchange=controls; $('tvChoice').onchange=controls;
$('openBrowser').onclick=()=>run(()=>api('api/action/open',{page_id:$('pageChoice').value}));
$('cast').onclick=()=>run(()=>api('api/action/cast',{page_id:$('pageChoice').value,tv_id:$('tvChoice').value}));
$('stop').onclick=()=>run(()=>api('api/action/stop',{}));
$('closeBrowser').onclick=()=>run(()=>api('api/action/close',{}));
document.querySelectorAll('[data-browser]').forEach(button=>button.onclick=()=>run(()=>api('api/action/browser',{action:button.dataset.browser})));
$('fullscreen').onclick=()=>$('viewport').requestFullscreen();
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
$('pairForm').addEventListener('submit',event=>{event.preventDefault();const value=$('pairValue').value;$('pairValue').value='';run(()=>api('api/action/pin',{value}));});
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
