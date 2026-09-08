function toast(message, type='success'){
  const zone=document.getElementById('toast-zone'); if(!zone) return;
  const el=document.createElement('div'); el.className=`toast ${type}`; el.textContent=message; zone.appendChild(el);
  setTimeout(()=>{el.style.opacity='0';el.style.transform='translateY(8px)';},2800);
  setTimeout(()=>el.remove(),3400);
}

function prependActivity(payload){
  const feed=document.getElementById('live-feed'); if(!feed) return;
  const el=document.createElement('div'); el.className='activity-item';
  const time=(payload.created_at||'').slice(11,19);
  el.innerHTML=`<span>${time}</span>${payload.message}`;
  feed.prepend(el);
  while(feed.children.length>20){feed.lastElementChild.remove();}
}

async function refreshStats(){
  if(!window.enableDashboardRefresh) return;
  try{
    const res=await fetch('/api/dashboard/stats'); if(!res.ok) return;
    const data=await res.json();
    document.querySelectorAll('[data-stat]').forEach(el=>{const k=el.dataset.stat;if(k in data) el.textContent=data[k];});
  }catch(e){}
}

if(typeof io !== 'undefined'){
  const socket=io();
  socket.on('connect',()=>toast('Realtime connected','success'));
  socket.on('activity',(payload)=>{prependActivity(payload); refreshStats();});
}

const searchForm=document.getElementById('search-form');
if(searchForm){
  searchForm.addEventListener('submit', async (e)=>{
    e.preventDefault();
    const scanner=document.getElementById('scanner');
    const results=document.getElementById('results');
    scanner?.classList.remove('hidden');
    results.innerHTML='';
    const fd=new FormData(searchForm);
    const payload={category:fd.get('category'), email:fd.get('email')};
    if((window.protectedCategories || []).includes(payload.category)){
      const authorized=await requestPrivateCode(payload.category);
      if(!authorized){scanner?.classList.add('hidden'); return;}
    }
    try{
      const controller=new AbortController();
      const timeout=setTimeout(()=>controller.abort(),35000);
      const res=await fetch('/api/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:controller.signal});
      clearTimeout(timeout);
      const data=await res.json();
      if(!res.ok || !data.ok){toast(data.error || 'Search failed','error'); return;}
      if(data.denied && data.denied.length){toast(`${data.denied.length} email(s) denied/not assigned`,'error');}
      for(const item of data.results){
        const card=document.createElement('article');
        card.className=`result-card ${item.status}`;
        const firstItem=(item.items && item.items[0]) ? item.items[0] : '';
        // Encode the copy text so it survives as an attribute
        const copyText = firstItem || item.result || '';
        const encodedCopy = encodeURIComponent(copyText);
        card.innerHTML=`
          <div class="panel-head"><h2>${item.status==='found'?'✅':item.status==='not_found'?'⚠️':'❌'} ${item.email}</h2><span class="badge ${item.status}">${item.status}</span></div>
          <pre>${escapeHtml(item.result)}</pre>
          <div class="actions">
            <button class="btn btn-small" data-copy="${encodedCopy}">Copy</button>
            ${firstItem && firstItem.startsWith('http') ? `<a class="btn btn-small btn-primary" href="${escapeAttr(firstItem)}" target="_blank" rel="noopener">Open Link</a>`:''}
            <span class="muted">${item.fetch_time}s</span>
          </div>`;
        results.appendChild(card);
      }
      toast('Search completed','success');
    }catch(err){toast(err.name==='AbortError'?'Search timed out. Please try again.':'Search error: '+err.message,'error')}
    finally{scanner?.classList.add('hidden'); refreshStats();}
  });
}

async function requestPrivateCode(category){
  const modal=document.getElementById('private-code-modal');
  const form=document.getElementById('private-code-form');
  if(!modal || !form) return true;
  const title=document.getElementById('private-code-title');
  if(title){const selected=document.querySelector(`input[name="category"][value="${category}"]`)?.closest('label');title.textContent=selected?.querySelector('strong')?.textContent || 'Private Access';}
  modal.classList.add('open'); modal.setAttribute('aria-hidden','false');
  const input=form.querySelector('input'); input?.focus();
  return new Promise(resolve=>{
    const finish=(value)=>{modal.classList.remove('open');modal.setAttribute('aria-hidden','true');form.reset();resolve(value);};
    form.onsubmit=async(e)=>{e.preventDefault();const res=await fetch('/api/private-code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({category,private_code:input.value})});const data=await res.json();if(!res.ok){toast(data.error||'Invalid access code.','error');return;}finish(true);};
    modal.querySelector('[data-close-private]')?.addEventListener('click',()=>finish(false),{once:true});
  });
}

// ----- Robust copy with fallback -----
function copyToClipboard(encoded) {
  const text = decodeURIComponent(encoded);
  return new Promise((resolve, reject) => {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(resolve).catch(() => {
        fallbackCopy(text).then(resolve).catch(reject);
      });
    } else {
      fallbackCopy(text).then(resolve).catch(reject);
    }
  });
}

function fallbackCopy(text) {
  return new Promise((resolve, reject) => {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    textarea.style.pointerEvents = 'none';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    try {
      const success = document.execCommand('copy');
      document.body.removeChild(textarea);
      if (success) resolve();
      else reject(new Error('execCommand copy failed'));
    } catch (err) {
      document.body.removeChild(textarea);
      reject(err);
    }
  });
}

// Global click handler for copy buttons
document.addEventListener('click', (e)=>{
  const btn=e.target.closest('[data-copy]');
  if(btn){
    const encoded = btn.dataset.copy || '';
    copyToClipboard(encoded)
      .then(() => toast('Copied!', 'success'))
      .catch(() => toast('Copy failed. Please select and copy manually.', 'error'));
    e.preventDefault();
  }
});

function escapeHtml(str){return String(str||'').replace(/[&<>"]/g, s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[s]));}
function escapeAttr(str){return escapeHtml(str).replace(/'/g,'&#39;');}

setInterval(refreshStats, 10000);

// Responsive mobile menu
const menuBtn=document.querySelector('.mobile-menu-btn');
const sidebar=document.querySelector('.sidebar');
const sidebarBackdrop=document.querySelector('.sidebar-backdrop');
function setMenu(open){
  if(!sidebar) return;
  sidebar.classList.toggle('open',open);
  sidebarBackdrop?.classList.toggle('open',open);
  document.body.classList.toggle('menu-open',open);
  menuBtn?.setAttribute('aria-expanded',open?'true':'false');
  if(menuBtn) menuBtn.textContent=open?'×':'☰';
}
menuBtn?.addEventListener('click',()=>setMenu(!sidebar?.classList.contains('open')));
sidebarBackdrop?.addEventListener('click',()=>setMenu(false));
document.querySelectorAll('.sidebar .nav-link,.sidebar .logout').forEach(a=>a.addEventListener('click',()=>setMenu(false)));

document.addEventListener('keydown',(e)=>{if(e.key==='Escape') setMenu(false);});

// Tap/click safety
document.addEventListener('DOMContentLoaded', () => {
  const bd = document.querySelector('.sidebar-backdrop');
  if (bd) {
    bd.style.pointerEvents = 'none';
  }
  if (document.body.classList.contains('menu-open') && !document.querySelector('.sidebar.open')) {
    document.body.classList.remove('menu-open');
  }
});
window.addEventListener('pageshow', () => {
  const bd = document.querySelector('.sidebar-backdrop');
  if (bd) bd.style.pointerEvents = 'none';
});
