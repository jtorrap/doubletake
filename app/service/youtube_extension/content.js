(() => {
  'use strict';
  const VIDEO = /^[A-Za-z0-9_-]{11}$/;
  let plan = null, timer = null, task = 0, lastState = '', started = false, ended = false;
  let lastMediaTime = 0, progressAt = 0, appliedAt = 0, leavingAd = false, latestLaunch = 0, cancelledLaunch = 0;
  function send(type, extra={}) {
    if (!plan) return;
    chrome.runtime.sendMessage({type,launch_id:plan.launch_id,index:plan.index,...extra}).catch(()=>{});
  }
  function status(state,error=false) {
    if (lastState === state) return;
    lastState = state; send('status',{state,error});
  }
  function cancel(block=false,revoked=latestLaunch) {
    if (block) {latestLaunch=Math.max(latestLaunch,revoked);cancelledLaunch=latestLaunch;}
    task++; plan=null; clearInterval(timer); timer=null;
    document.documentElement.removeAttribute('data-doubletake-player');
  }
  function videoID() { return new URL(location.href).searchParams.get('v'); }
  function visible(element) { return !!(element && element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden'); }
  function progress(row) {
    const bar = row.querySelector('ytd-thumbnail-overlay-resume-playback-renderer #progress, yt-thumbnail-overlay-progress-bar-view-model .ytThumbnailOverlayProgressBarHostWatchedProgressBarSegment');
    if (!bar) return 0;
    const percent = parseFloat(bar.style.width);
    if (bar.style.width.endsWith('%') && Number.isFinite(percent)) return Math.max(0,Math.min(100,percent));
    const parent = bar.parentElement?.getBoundingClientRect().width || 0;
    return parent ? Math.max(0,Math.min(100,bar.getBoundingClientRect().width/parent*100)) : 0;
  }
  function startTime(raw) {
    if (/^\d+s?$/.test(raw)) return parseInt(raw,10);
    const parts = /^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$/.exec(raw);
    return parts && parts.slice(1).some(Boolean) ? Number(parts[1] || 0)*3600 + Number(parts[2] || 0)*60 + Number(parts[3] || 0) : null;
  }
  function playlistItem(row) {
    const anchor = row.querySelector('a#video-title[href], a.yt-lockup-metadata-view-model__title[href]');
    if (!anchor || row.getAttribute('is-playable') === 'false' ||
        row.querySelector('[overlay-style="UNPLAYABLE"]') ||
        /^\[?(?:private video|deleted video|unavailable video)\]?$/i.test(anchor.textContent.trim())) return null;
    let url;
    try { url = new URL(anchor.href,location.href); } catch { return null; }
    const id = url.searchParams.get('v');
    if (url.origin !== 'https://www.youtube.com' || !VIDEO.test(id || '')) return null;
    const watched = progress(row);
    // The visible meter only selects items. YouTube resumes its own saved
    // position; never estimate seek times or modify history or Watch Later.
    if (watched >= 100 || (!plan.resume && watched > 0)) return null;
    const start = startTime(url.searchParams.get('t') || '');
    return {id,start:plan.resume && start !== null && start <= 604800 ? start : null};
  }
  async function collect(generation) {
    const observed = new Map();
    let stable = 0, previous = '', iterations = 0, foundRows = false;
    const begin = performance.now();
    while (plan && task === generation && performance.now()-begin < 180000 && iterations++ < 600) {
      if (location.pathname !== '/playlist' || new URL(location.href).searchParams.get('list') !== 'WL') {
        status('needs_interaction',true); return;
      }
      const rows = [...document.querySelectorAll('ytd-playlist-video-renderer')];
      foundRows ||= rows.length > 0;
      for (const row of rows) {
        const anchor = row.querySelector('a#video-title[href], a.yt-lockup-metadata-view-model__title[href]');
        if (!anchor) continue;
        let url;
        try {url=new URL(anchor.href,location.href);} catch {continue;}
        const id=url.searchParams.get('v');
        if (url.origin === 'https://www.youtube.com' && VIDEO.test(id || '')) observed.set(id,playlistItem(row));
      }
      // Progress overlays can arrive after the row itself. Update eligibility
      // on every observation while Map retains each item's first-seen order.
      const items=[...observed.values()].filter(Boolean);
      if (items.length > 5000) {status('error',true);return;}
      const height = document.documentElement.scrollHeight;
      // Include row identities: virtualized pages can replace rows without
      // changing their count or the document's overall height.
      const signature = `${height}:${rows.map(row=>`${row.querySelector('a#video-title[href], a.yt-lockup-metadata-view-model__title[href]')?.getAttribute('href') || ''}:${progress(row)}`).join('|')}`;
      const atBottom = innerHeight+scrollY >= height-8;
      const loading = [...document.querySelectorAll('ytd-continuation-item-renderer tp-yt-paper-spinner, ytd-continuation-item-renderer .yt-spinner')].some(visible);
      stable = atBottom && signature===previous && !loading ? stable+1 : 0;
      previous=signature;
      if (stable >= 12 && foundRows) {send('queue',{items});return;}
      const message = [...document.querySelectorAll('ytd-message-renderer, ytd-background-promo-renderer, ytd-alert-with-button-renderer, yt-alert-with-button-renderer')].filter(visible).map(e=>e.textContent).join(' ');
      if (!foundRows && /no videos|playlist is empty/i.test(message)) {send('queue',{items:[]});return;}
      const signIn = [...document.querySelectorAll('ytd-browse a[href*="accounts.google.com"], ytd-playlist-header-renderer a[href*="accounts.google.com"]')].some(visible);
      if (!foundRows && performance.now()-begin>2000 && (signIn || /sign in|unavailable|does not exist/i.test(message))) {
        status('needs_interaction',true);return;
      }
      if (!foundRows && !loading && performance.now()-begin>15000) {status('needs_interaction',true);return;}
      window.scrollTo(0,Math.min(height,scrollY+Math.max(300,innerHeight*0.8)));
      await new Promise(resolve=>setTimeout(resolve,500));
    }
    if (plan && task === generation) status('needs_interaction',true);
  }
  function tick() {
    if (!plan || plan.phase === 'collect') return;
    const video = document.querySelector('#movie_player video');
    if (plan.phase === 'finished') {video?.pause(); return;}
    if (location.pathname !== '/watch' || videoID() !== plan.video_id) {
      if (performance.now()-appliedAt>15000) status('needs_interaction',true);
      return;
    }
    const player = document.getElementById('movie_player');
    if (player) document.documentElement.setAttribute('data-doubletake-player','');
    const error = document.querySelector('yt-playability-error-supported-renderers');
    if (visible(error)) {
      const text = error.textContent || '';
      if (/private video|video unavailable|video has been removed|video is unavailable|deleted video/i.test(text)) {
        if (!ended) {ended=true; send('unavailable',{video_id:plan.video_id});}
      } else status('needs_interaction',true);
      return;
    }
    if (!video) {
      if (performance.now()-appliedAt>30000) status('needs_interaction',true);
      return;
    }
    const advert = player.classList.contains('ad-showing') || player.classList.contains('ad-interrupting');
    if (advert) {leavingAd=true;status('loading');return;}
    // An ad may finish before YouTube removes its CSS marker. Wait for a
    // non-ended media timeline before accepting content ended signals.
    if ((leavingAd || !started) && video.ended) {
      status(performance.now()-appliedAt>30000?'needs_interaction':'loading',performance.now()-appliedAt>30000);
      return;
    }
    if (leavingAd) {leavingAd=false;lastMediaTime=video.currentTime;progressAt=performance.now();}
    if (!started && video.readyState>=1) {
      started=true;
      const generation=task;
      video.play().catch(()=>{if(plan && generation===task)status('needs_interaction',true);});
    }
    if (video.ended && !ended && started) {
      ended=true; video.pause(); send('ended',{video_id:plan.video_id}); return;
    }
    if (ended) {video.pause();return;}
    if (video.currentTime !== lastMediaTime) {lastMediaTime=video.currentTime;progressAt=performance.now();}
    if (!video.paused && video.readyState>=2 && performance.now()-progressAt<10000) status('playing');
    else if (started && video.paused) status(lastState==='needs_interaction' ? 'needs_interaction' : 'paused');
    else if (performance.now()-appliedAt>30000) status('needs_interaction',true);
  }
  chrome.runtime.onMessage.addListener((value,sender,respond)=>{
    if (sender.id !== chrome.runtime.id) return;
    if (value?.type === 'cancel') {
      if (value.launch_id !== undefined && (!Number.isSafeInteger(value.launch_id) || value.launch_id < latestLaunch)) return;
      cancel(true,value.launch_id ?? latestLaunch);respond({ok:true});return;
    }
    const incoming=value?.plan;
    if (value?.type !== 'apply' || !Number.isSafeInteger(incoming?.launch_id) || incoming.launch_id < 1 ||
        !Number.isInteger(incoming.index) || incoming.index < 0 || typeof incoming.resume !== 'boolean' ||
        !['video','watch_later'].includes(incoming.mode) || !['collect','play','finished'].includes(incoming.phase)) return;
    if (incoming.launch_id < latestLaunch || incoming.launch_id === cancelledLaunch) return;
    const sameLaunch = plan?.launch_id === incoming.launch_id;
    const matchingLocation = incoming.phase === 'collect' ? location.pathname === '/playlist' && new URL(location.href).searchParams.get('list') === 'WL' :
      VIDEO.test(incoming.video_id || '') && location.pathname === '/watch' && videoID() === incoming.video_id;
    if (!matchingLocation && !(incoming.phase === 'finished' && sameLaunch)) return;
    if (sameLaunch && (incoming.index < plan.index || (plan.phase === 'finished' && incoming.phase !== 'finished'))) return;
    if (sameLaunch && plan.index === incoming.index && plan.phase === incoming.phase) {respond({ok:true});return;}
    cancel();plan=incoming;latestLaunch=incoming.launch_id;lastState='';started=ended=leavingAd=false;lastMediaTime=0;
    appliedAt=progressAt=performance.now();
    if (plan.phase==='collect') void collect(task);
    else {tick();timer=setInterval(tick,250);}
    respond({ok:true});
  });
  document.addEventListener('yt-navigate-finish',()=>chrome.runtime.sendMessage({type:'ready'}).catch(()=>{}));
  chrome.runtime.sendMessage({type:'ready'}).catch(()=>{});
})();
