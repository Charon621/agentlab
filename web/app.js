/* AgentLab · 研究工作台前端 */
const $ = id => document.getElementById(id);
const $$ = sel => [...document.querySelectorAll(sel)];
const names = {planner:'Planner',human_plan:'计划审核',searcher:'Searcher',analyst:'Analyst',reviewer:'Reviewer',single:'单智能体',finalize:'报告',clarify:'澄清',recover:'错误恢复',human:'人工处理',workflow:'工作流'};
const duties = {
  planner:'规划研究方向与任务分解',
  searcher:'搜索相关信息并收集资料',
  analyst:'分析资料并生成初步结论',
  reviewer:'审核内容并生成最终报告',
  single:'单智能体完成检索、分析与作答',
  finalize:'汇总全部结论，生成研究报告'
};
const statuses = {queued:'排队中',running:'研究中',paused:'等待人工处理',completed:'已完成',limited:'未完整完成',interrupted:'已中断，可恢复',cancelled:'已停止'};
const evStatusText = {running:'执行中',done:'完成',error:'失败',limited:'受限完成',waiting:'等待',interrupted:'中断',stopping:'停止中'};
const connectionCategories={connected:'连接成功',authentication:'密钥无效 · 401',permission:'权限不足 · 403',model_not_found:'接口或模型不存在 · 404',rate_limit:'额度或频率受限 · 429',timeout:'请求超时',network:'网络连接失败',http_error:'服务响应异常',redirect_blocked:'接口跳转已阻止',unsafe_url:'地址安全检查未通过',credential_missing:'原凭证缺失',legacy_binding_required:'需显式绑定凭证',grant_missing:'联动授权失效',grant_revoked:'联动授权已撤销',grant_expired:'联动授权已过期',grant_drift:'授权模型版本已变',unsupported_capability:'模型能力不支持',invalid_response:'响应格式异常',provider_error:'模型接口异常'};
function readableConnection(result){return {...result,message:result.message.replace(/^credential_missing：/,'原凭证缺失：'),category:connectionCategories[result.category]||result.category}}
let currentId = null, currentTask = null, stream = null, polling = null, refreshTimer = null;
const openNodes = new Set();

/* ---------- 基础工具 ---------- */
function toast(text){ $('toast').textContent=text; $('toast').hidden=false; clearTimeout(toast.t); toast.t=setTimeout(()=>$('toast').hidden=true,4200); }
async function api(url, options={}){
  const res=await fetch(url,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});
  if(!res.ok){let body;try{body=await res.json()}catch{body={detail:'本地服务响应异常'}};throw new Error(typeof body.detail==='string'?body.detail:'输入内容不符合要求，请检查长度、范围与必填项。')}
  const value=await res.json();return url.endsWith('/test')&&value.category?readableConnection(value):value;
}
function el(tag,cls,text){const x=document.createElement(tag);if(cls)x.className=cls;if(text!==undefined)x.textContent=text;return x}
function modeText(mode){return mode==='live'?'真实联网研究':'离线演示'}
function setBusy(button,busy,text){button.disabled=busy;if(text)button.textContent=text}
function fmtDuration(ms){
  if(ms==null||ms<0)return '—';
  const s=ms/1000;
  if(s<1)return '<1 s';
  if(s<60)return s.toFixed(1)+' s';
  return Math.floor(s/60)+'m '+Math.round(s%60)+'s';
}
const svgCheck='<svg viewBox="0 0 16 16" fill="none"><path d="M3.5 8.5l3 3 6-7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const svgCross='<svg viewBox="0 0 16 16" fill="none"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>';
const svgDot='<svg viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="3.4" stroke="currentColor" stroke-width="1.6"/></svg>';
const svgChevron='<svg class="flow-chevron" viewBox="0 0 16 16" fill="none"><path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

/* ---------- 历史与导航 ---------- */
let lastItems=[];
function switchView(name){
  for(const v of ['create','history','library','models','tools','settings','task'])$(''+v+'-view').hidden=v!==name;
  $$('.side-link').forEach(b=>b.classList.toggle('active',b.dataset.nav===name));
}
function renderRecordList(container,items,emptyText,metaFn){
  container.replaceChildren();
  if(!items.length){container.append(el('p','empty-side',emptyText));return}
  for(const task of items){
    const wrap=el('div','record-row');
    const row=el('button','record-item');
    row.type='button';
    const main=el('span','record-main');
    main.append(el('strong','',task.question.split('\n')[0]));
    const small=el('small','',metaFn(task));
    main.append(small);
    row.append(main,el('span','status-pill '+task.status,statuses[task.status]||task.status));
    row.addEventListener('click',()=>{openTask(task.id);closeMenu()});
    const del=el('button','record-delete','删除');
    del.type='button';
    del.title='删除这条研究记录';
    del.addEventListener('click',async e=>{
      e.stopPropagation();
      if(!await confirmArchive())return;
      del.disabled=true;
      try{
        await api('/api/tasks/'+task.id,{method:'DELETE'});
        toast('已移入回收站');
        if(currentId===task.id)showCreate();
        await loadHistory();
      }catch(err){
        del.disabled=false;
        toast(err.message);
      }
    });
    wrap.append(row,del);
    container.append(wrap);
  }
}
const taskMeta=task=>new Date(task.created*1000).toLocaleString('zh-CN',{hour12:false})+' · '+(task.request?.execution_mode==='live'?'真实研究':'离线演示')+' · '+(task.request?.mode==='single'?'单智能体':'多智能体');
async function updateUsage(items){
  const day=new Date();day.setHours(0,0,0,0);
  const today=items.filter(t=>t.created*1000>=day.getTime());
  let tokens=0,calls=0,costKnown=true,cost=0;
  for(const t of today){
    try{
      const detail=await api('/api/tasks/'+t.id);
      const r=detail.result||{};
      if(r.model_calls)calls+=r.model_calls;
      if(r.usage_complete&&r.model_calls){tokens+=(r.input_tokens||0)+(r.output_tokens||0)}
      if(r.cost_usd!=null&&r.cost_usd!==undefined)cost+=r.cost_usd;
      else if(r.model_calls)costKnown=false;
    }catch{}
  }
  const usageEls=[$('usage-tokens'),$('usage-calls'),$('usage-cost')];
  if(usageEls[0])usageEls[0].textContent=tokens.toLocaleString('zh-CN');
  if(usageEls[1])usageEls[1].textContent=String(calls);
  if(usageEls[2])usageEls[2].textContent=calls===0?'$0':(costKnown?'$'+cost.toFixed(4):'未知');
}
async function loadHistory(){
  try{
    const items=await api('/api/tasks');
    lastItems=await Promise.all(items.map(async t=>{
      try{return {...t,...(await api('/api/tasks/'+t.id))}}catch{return t}
    }));
    $('history-count').textContent=items.length;
    renderRecordList($('history-list'),lastItems,'还没有研究记录。从「研究工作台」提出第一个问题。',taskMeta);
    updateUsage(items);
  }catch(e){setConn(false)}
}
function setConn(ok){
  for(const id of ['conn-dot','conn-dot-m']){const d=$(id);if(d)d.className='dot '+(ok?'on':'off')}
  $('connection').textContent=ok?'本地服务已连接':'服务暂时不可用';
}
function stopListening(){if(stream){stream.close();stream=null}if(polling){clearInterval(polling);polling=null}if(refreshTimer){clearTimeout(refreshTimer);refreshTimer=null}}
function clearGrant(){ $('grant-token').value='';$('grant-once').hidden=true; }
function showCreate(){
  stopListening();currentId=null;currentTask=null;clearGrant();
  switchView('create');
  history.replaceState(null,'',location.pathname);
  loadHistory();loadModels().catch(e=>toast(e.message));
  closeMenu();renderPill();
}
function showModels(){
  stopListening();clearGrant();
  switchView('models');
  history.replaceState(null,'','#models');closeMenu();
  (async()=>{
    try{await loadModels();await loadGrants()}catch(e){toast(e.message)}
  })();
}
function showHistory(){
  stopListening();clearGrant();
  switchView('history');
  history.replaceState(null,'','#history');closeMenu();
  loadHistory();
}
function showLibrary(){
  stopListening();clearGrant();
  switchView('library');
  history.replaceState(null,'','#library');closeMenu();
  renderLibrary().catch(e=>toast(e.message));
}
function showTools(){
  stopListening();clearGrant();
  switchView('tools');
  history.replaceState(null,'','#tools');closeMenu();
  renderTools().catch(e=>toast(e.message));
}
function showSettings(){
  stopListening();clearGrant();
  switchView('settings');
  history.replaceState(null,'','#settings');closeMenu();
  renderSettings().catch(e=>toast(e.message));
}

/* ---------- 知识库 / 工具 / 设置 ---------- */
async function renderLibrary(){
  const container=$('library-list');
  container.replaceChildren(el('p','empty-side','正在汇总历次研究的来源资料…'));
  const items=lastItems.length?lastItems:await api('/api/tasks');
  const details=await Promise.all(items.map(async t=>{try{return await api('/api/tasks/'+t.id)}catch{return null}}));
  const byUrl=new Map();
  for(const task of details.filter(Boolean)){
    for(const src of (task.result?.sources)||[]){
      const key=src.url||src.title;
      if(!byUrl.has(key))byUrl.set(key,{...src,tasks:[]});
      byUrl.get(key).tasks.push(task.question.split('\n')[0]);
    }
  }
  const list=[...byUrl.values()].sort((a,b)=>b.tasks.length-a.tasks.length);
  container.replaceChildren();
  if(!list.length){container.append(el('p','empty-side','还没有可汇总的来源。完成一次研究后，检索到的资料会出现在这里。'));return}
  for(const src of list){
    const card=el('article','source-card');
    const h=el('h3');h.append(el('span','',src.id||'·'),document.createTextNode(src.title||src.url));
    card.append(h);
    const url=safeSourceUrl(src.url);
    if(url){const a=el('a','',src.url+' ↗');a.href=url;a.target='_blank';a.rel='noopener noreferrer';card.append(a)}
    card.append(el('small','',(src.retrieved?'已返回检索片段 · 非全文事实核验':'固定离线样例 · 未实时访问网页')+' · 被 '+src.tasks.length+' 项研究引用'));
    card.append(el('p','',src.excerpt||''));
    container.append(card);
  }
}
async function renderTools(){
  const health=await api('/api/health');
  const grid=$('tools-grid');
  grid.replaceChildren();
  const searchName=health.search==='tavily'?'Tavily Search API':'Wikipedia 公开检索';
  const searchDesc=health.search==='tavily'
    ?'通过 Tavily API 检索公开网页，需要 TAVILY_API_KEY 环境配置。'
    :'通过 Wikipedia 公开 API 检索条目与摘要，无需密钥，中文问题自动使用中文站点。';
  const tools=[
    {name:'search_web',badge:'检索工具',desc:searchDesc+' Searcher / 单智能体在执行中调用，来源片段作为证据数据提供给后续节点。',state:searchName},
    {name:'研究计划编辑',badge:'人工协作',desc:'Planner 生成计划后暂停，由你修改计划或调整方向，确认后从检查点继续，不消耗模型预算。',state:health.live_calls_require_explicit_selection?'启用':'—'},
    {name:'引用核验',badge:'规则工具',desc:'对最终报告执行引用编号与来源映射核验；只验证编号对应关系，不代表事实正确性。',state:'启用'}
  ];
  for(const t of tools){
    const card=el('article','panel tool-card');
    const head=el('div','tool-card-head');
    head.append(el('strong','',t.name),el('span','badge done',t.badge),el('span','tool-state',t.state));
    card.append(head,el('p','hint',t.desc));
    grid.append(card);
  }
  const note=$('provider-status-note');
  note.textContent='PRESETS · 密钥在「模型管理」配置';
  const box=$('provider-status');
  box.replaceChildren();
  for(const [provider,info] of Object.entries(health.providers||{})){
    const row=el('div','provider-row');
    row.append(el('span','provider-name',providerNames[provider]||provider),
      el('code','',info.model||'—'),
      el('span','badge '+(info.configured?'done':''),info.configured?'已配置密钥':'未配置'));
    box.append(row);
  }
}
async function renderSettings(){
  const health=await api('/api/health').catch(()=>null);
  const svc=$('settings-service');
  svc.replaceChildren();
  const rows=[['应用','AgentLab '+ (health?.version||'1.0.0')],['数据模式','schema v'+(health?.schema_version||'1.0')],['搜索服务',health?.search==='tavily'?'Tavily':'Wikipedia'],['实时调用','须显式选择模型配置'],['跨项目联动','Benchmark Studio 授权调用']];
  for(const [k,v] of rows){const d=el('div');d.append(el('dt','',k),el('dd','',v));svc.append(d)}
  try{
    const legacy=await api('/api/history/unclaimed');
    $('legacy-history').hidden=!legacy.count;
    $('legacy-history-count').textContent='发现 '+legacy.count+' 项升级前的单用户研究记录。';
    $('legacy-history-done').hidden=!!legacy.count;
  }catch{}
}

/* ---------- Composer：模型 pill / 联网搜索 / 设置 ---------- */
function renderPill(){
  const live=$('execution-mode').value==='live';
  let name,model,letter;
  if(live){
    const m=modelConfigs.find(x=>x.id===$('model-config').value);
    if(m){name=m.name;model=m.model+(m.configured?'':' · 需补密钥');letter=providerLetter[m.provider]||'M'}
    else{name='未配置模型';model='前往「模型管理」添加';letter='?'}
  }else{
    const p=$('provider').value;
    name=providerNames[p]||p;model=modelPresets[p]?.[1]||'';letter=providerLetter[p]||'C';
  }
  $('pill-avatar').textContent=letter;
  $('pill-name').textContent=name;
  $('pill-model').textContent=live?model:(model+' · 离线演示');
}
function selectModelConfig(id){
  $('model-config').value=id;
  const m=modelConfigs.find(x=>x.id===id);
  // 选中模型后同步厂商，避免上方模型与下方提供商不一致。
  if(m&&$('provider').value!==m.provider){
    $('provider').value=m.provider;
    $('provider').dispatchEvent(new Event('change'));
  }
  closePillMenu();
  renderPill();
}
function renderPillMenu(){
  const menu=$('pill-menu');
  const live=$('execution-mode').value==='live';
  menu.replaceChildren();
  const head=el('p','pill-menu-head',live?'使用模型配置（来自模型管理）':'演示基线提供商');
  menu.append(head);
  if(live){
    if(!modelConfigs.length){
      menu.append(el('p','hint','还没有模型配置。'));
      const add=el('button','pill-menu-item','前往「模型管理」添加模型');
      add.type='button';add.addEventListener('click',()=>{closePillMenu();showModels()});
      menu.append(add);
    }
    for(const m of modelConfigs){
      const b=el('button','pill-menu-item'+(m.id===$('model-config').value?' active':''));
      b.type='button';
      b.append(el('span','pill-dot',providerLetter[m.provider]||'M'),el('span','',''+m.name+' · '+m.model+(m.configured?'':' · 需补密钥')));
      b.addEventListener('click',()=>selectModelConfig(m.id));
      menu.append(b);
    }
  }else{
    for(const p of Object.keys(modelPresets)){
      if(p==='custom')continue;
      const b=el('button','pill-menu-item'+($('provider').value===p?' active':''));
      b.type='button';
      b.append(el('span','pill-dot',providerLetter[p]),el('span','',providerNames[p]+' / '+modelPresets[p][1]));
      b.addEventListener('click',()=>{$('provider').value=p;closePillMenu();renderPill()});
      menu.append(b);
    }
  }
}
function closePillMenu(){$('pill-menu').hidden=true;$('model-pill').setAttribute('aria-expanded','false')}

/* ---------- 创建任务 ---------- */
function payload(question){
  const p={question,execution_mode:$('execution-mode').value,provider:$('provider').value,mode:$('mode').value,
    review_plan:$('review-plan').checked,mock_scenario:$('mock-scenario').value,
    max_calls:Number($('max-calls').value),max_iterations:Number($('max-iterations').value),timeout_seconds:Number($('timeout').value)};
  if(p.execution_mode==='live'){
    if(!$('model-config').value)throw new Error('请先在「模型管理」添加模型和密钥。');
    p.model_config_id=$('model-config').value;
  }
  return p;
}
async function submitTask(e){
  e.preventDefault();$('form-error').hidden=true;
  const q=$('question').value.trim();
  const live=$('execution-mode').value==='live';
  if(!live&&q.length>=8){
    toast('已按「离线演示」启动：不会调用真实模型，也不会联网检索。');
  }
  const b=$('start-research');setBusy(b,true,'正在创建…');
  try{
    const task=await api('/api/tasks',{method:'POST',body:JSON.stringify(payload(q))});
    await openTask(task.id);
  }catch(err){$('form-error').textContent=err.message;$('form-error').hidden=false}
  finally{b.disabled=false;b.innerHTML='开始研究 <svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 8h9M8.5 4.5L12 8l-3.5 3.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>'}
}
function syncExecutionUI(){
  const live=$('execution-mode').value==='live';
  $('scenario-field').hidden=live;
  $('provider-field').hidden=live;
  $('live-model-field').hidden=!live;
  $('mock-hint').hidden=live;
  $('live-hint').hidden=!live;
  for(const btn of document.querySelectorAll('.mode-btn')){
    const on=btn.dataset.mode===(live?'live':'mock');
    btn.classList.toggle('active',on);
    btn.setAttribute('aria-checked',String(on));
  }
  renderPill();
}
function bindSegment(containerId,selectId,onChange){
  const box=$(containerId),select=$(selectId);
  box.addEventListener('click',e=>{
    const btn=e.target.closest('.segment');if(!btn)return;
    for(const s of box.querySelectorAll('.segment')){s.classList.toggle('active',s===btn);s.setAttribute('aria-checked',String(s===btn))}
    select.value=btn.dataset.value;
    select.dispatchEvent(new Event('change'));
    if(onChange)onChange();
  });
}

/* ---------- 任务详情 ---------- */
async function openTask(id){
  stopListening();clearGrant();currentId=id;openNodes.clear();
  hideHumanDecision();
  switchView('task');
  $$('.side-link').forEach(b=>b.classList.remove('active'));
  $('task-title').textContent='正在加载…';
  history.replaceState(null,'','#task='+id);
  try{await refreshTask();loadHistory();listen(id)}catch(e){toast(e.message)}
}
function listen(id){
  if(currentId!==id)return;
  stream=new EventSource('/api/tasks/'+id+'/events');
  stream.addEventListener('progress',()=>{if(!refreshTimer)refreshTimer=setTimeout(()=>{refreshTimer=null;refreshTask().catch(()=>{})},150)});
  stream.addEventListener('state',()=>{refreshTask().then(loadHistory);if(stream)stream.close()});
  stream.onerror=()=>{};
  polling=setInterval(()=>{if(currentId===id)refreshTask().catch(()=>{})},1800);
}
async function refreshTask(){
  if(!currentId)return;
  const id=currentId,task=await api('/api/tasks/'+id);
  if(id!==currentId)return;
  currentTask=task;renderTask(task);
}
function nodeState(step,events,task){
  const evs=events.filter(e=>e.node===step);
  const last=evs.at(-1);
  let cls='';
  const reportDone=step==='finalize'&&(task.status==='completed'||task.status==='limited')&&!!(task.result?.report||task.result?.draft);
  if(step==='finalize'&&(task.status==='completed'||reportDone))cls='done';
  else if(last?.status==='running'&&['running','queued'].includes(task.status))cls='running';
  else if(last?.status==='done')cls='done';
  else if(last&&['error','limited'].includes(last.status))cls='error';
  else if(last?.status==='running'&&task.status==='paused')cls='human-wait';
  let duration=null;
  const start=evs.find(e=>e.status==='running');
  const end=[...evs].reverse().find(e=>['done','error','limited'].includes(e.status));
  if(start&&end&&end.time>=start.time)duration=(end.time-start.time)*1000;
  return {cls,evs,last,duration};
}
function renderWorkflow(task,steps,events){
  $('workflow').replaceChildren();
  steps.forEach((step,i)=>{
    const {cls,evs,duration}=nodeState(step,events,task);
    const node=el('div','flow-node '+cls+(openNodes.has(step)?' open':''));
    node.dataset.node=step;
    const marker=el('span','flow-marker');
    if(cls==='done')marker.innerHTML=svgCheck;
    else if(cls==='running')marker.innerHTML='<span class="spin"></span>';
    else if(cls==='error')marker.innerHTML=svgCross;
    else if(cls==='human-wait')marker.innerHTML=svgDot;
    else marker.textContent=String(i+1).padStart(2,'0');
    const head=el('button','flow-head');
    head.type='button';
    head.setAttribute('aria-expanded',String(openNodes.has(step)));
    const title=el('span','flow-title');
    title.append(el('strong','',names[step]||step),el('small','',duties[step]||''));
    const meta=el('span','flow-meta');
    const badgeText=cls==='done'?'已完成':cls==='running'?'执行中':cls==='error'?'失败':cls==='human-wait'?'等待人工':'未开始';
    meta.append(el('span','badge '+cls,badgeText));
    if(duration!=null)meta.append(el('time','',fmtDuration(duration)));
    meta.insertAdjacentHTML('beforeend',svgChevron);
    head.append(title,meta);
    head.addEventListener('click',()=>{
      if(openNodes.has(step))openNodes.delete(step);else openNodes.add(step);
      node.classList.toggle('open');
      head.setAttribute('aria-expanded',String(openNodes.has(step)));
      detail.hidden=!openNodes.has(step);
    });
    const detail=el('div','flow-detail');
    detail.hidden=!openNodes.has(step);
    const dl=el('dl');
    const rows=[['职责',duties[step]||'—'],['状态',badgeText],['耗时',duration!=null?fmtDuration(duration):'—'],['事件数',String(evs.length)]];
    for(const [k,v] of rows){dl.append(el('dt','',k),el('dd','',v))}
    detail.append(dl);
    if(evs.length){
      const list=el('ul','flow-events');
      for(const ev of evs){
        const li=el('li',ev.status);
        const body=el('div');
        body.append(el('span','ev-status',evStatusText[ev.status]||ev.status),document.createTextNode('　'));
        body.append(el('p','',ev.detail||''));
        li.append(el('time','',new Date(ev.time*1000).toLocaleTimeString('zh-CN',{hour12:false})),body);
        list.append(li);
      }
      detail.append(list);
    }else detail.append(el('p','hint','尚未产生事件。节点开始执行后，这里会显示输入输出摘要、工具调用记录与错误提示。'));
    node.append(marker,head,detail);
    $('workflow').append(node);
  });
}
function renderTask(task){
  const s=task.result||{},req=task.request,events=task.events||[],mock=req.execution_mode==='mock';
  $('task-title').textContent=task.question.split('\n\n上一轮')[0];
  $('task-kicker').textContent=modeText(req.execution_mode)+' / '+(req.mode==='multi'?'四角色协作':'单智能体基线')+' / '+task.id.slice(0,8);
  $('task-status').textContent=statuses[task.status]||task.status;
  $('task-status').className='status-pill '+task.status;
  $('task-warning').textContent=mock?'演示模式：使用固定离线样例，仅验证工程流程。没有真实模型或实时联网研究，不能作为架构优劣实验结果。':'真实模式：会联网检索并产生模型调用费用。网页内容作为证据数据处理；引用核验不能代替事实验证。';
  $('iteration-label').textContent='第 '+(s.iteration||1)+' / '+req.max_iterations+' 轮';
  const steps=req.mode==='single'?['searcher','single','finalize']:['planner','searcher','analyst','reviewer','finalize'];
  renderWorkflow(task,steps,events);
  $('latest-event').textContent=events.length?(names[events.at(-1).node]||events.at(-1).node)+' · '+events.at(-1).detail:'准备执行…';

  const pending=(s.interrupts||[])[0];
  const human=task.status==='paused'&&pending;
  $('human-panel').hidden=!human;
  if(human){
    hideHumanDecision();
    const type=pending.kind;
    $('human-title').textContent=type==='plan'?'计划已准备好，等你确认':type==='error'?'执行遇到问题，可以恢复':'需要你补充一点信息';
    $('human-message').textContent=pending.message;
    $('human-ref-question').textContent=task.question.split('\n\n上一轮')[0];
    // Credential restore entry: reachable from task context.
    const credErr=pending.category==='credential_missing'||pending.category==='legacy_binding_required';
    const restoreLink=$('restore-from-task');
    if(restoreLink){
      restoreLink.hidden=!credErr;
      if(credErr){
        restoreLink.textContent=pending.category==='legacy_binding_required'?'前往模型管理绑定凭证':'前往模型管理补充原凭证';
        restoreLink.onclick=()=>{showModels();toast('请找到对应模型，补充原凭证 ID 的密钥后返回任务继续')};
      }
    }
    const currentDir=pending.direction||s.direction||'';
    $('human-ref-direction').textContent=currentDir||'（尚未设置，确认后将沿用研究问题）';
    if($('human-panel').dataset.task!==task.id+JSON.stringify(pending)){
      const planText=(s.plan||pending.plan||[]).join('\n');
      $('plan-edit').value=planText;
      $('plan-edit').dataset.original=planText;
      // 计划审核/恢复：预填当前方向，清空才表示不改。澄清：必须写新答案，不预填。
      const prefill=type==='clarification'?'':currentDir;
      $('direction').value=prefill;
      $('direction').dataset.original=prefill;
      $('human-panel').dataset.task=task.id+JSON.stringify(pending);
      $('resume-error').hidden=true;
      $('resume-hint').textContent=type==='clarification'
        ?'请填写补充说明后继续；上方显示的是当前方向，不会被自动覆盖'
        :'方向和计划会预填当前值；清空表示保持原样';
      $('restore-plan').hidden=!planText;
    }
    $('plan-edit').hidden=type==='error';
    $('plan-edit-label').hidden=type==='error';
    $('restore-plan').hidden=type==='error'||!$('plan-edit').dataset.original;
    $('direction').placeholder=type==='error'?'先打开「模型管理」补充原凭证，再返回当前任务继续。也可调整关键词。':'可修改研究方向；留空表示保持当前方向不变。';
  }else if(task.status==='paused'&&!pending){
    $('resume-hint').textContent='修改后从检查点继续，确认内容会固定显示';
  }else if(s.human_decisions?.length){
    const decision=s.human_decisions.at(-1);
    showHumanDecision(decision.plan,decision.direction,'已从持久化检查点恢复人工确认内容。');
  }else if(req.review_plan&&s.direction&&task.status==='completed'){
    showHumanDecision(s.plan,s.direction,'从旧版任务的检查点读取当前计划与方向。');
  }

  $('metric-time').textContent=((s.elapsed_ms||0)/1000).toFixed(2)+' s';
  $('metric-calls').textContent=s.model_calls||0;
  $('metric-search').textContent=s.search_calls||0;
  $('metric-tokens').textContent=!mock&&s.usage_complete&&s.model_calls?(s.input_tokens||0)+' / '+(s.output_tokens||0):'未知';
  $('metric-cost').textContent=s.cost_usd!=null?Number(s.cost_usd).toFixed(6):'未知';
  $('budget-label').textContent='总调用 '+(s.calls||0)+' / '+req.max_calls;
  $('budget-meter').style.width=Math.min(100,(s.calls||0)/req.max_calls*100)+'%';
  $('metrics-note').textContent=mock?'其中 '+(s.simulated_calls||0)+' 次为模拟模型调用；无真实 token / 费用。':'人工等待不计时；费用须按配置单价估算。未返回 usage 的调用保持未知。';

  const report=s.report||s.draft;
  $('report-empty').hidden=!!report;$('report').hidden=!report;
  if(report)renderReport(report);
  if(!report&&['limited','interrupted'].includes(task.status)){
    $('report-empty').querySelector('h3').textContent='研究尚未完成';
    $('report-empty').querySelector('p').textContent=task.error||s.stop_reason||'请查看执行日志与调用预算。';
  }
  if(report&&task.status==='limited'){
    $('report-empty').hidden=true;$('report').hidden=false;
  }
  const check=s.citation_check;
  $('citation-banner').hidden=!check;
  if(check){
    $('citation-banner').className='citation-banner'+(check.all_mapped?'':' warning');
    $('citation-banner').textContent=(check.all_mapped?'引用编号与来源映射通过。':'引用映射未通过。')+(check.retrieval_verified?'检索片段已返回。':'离线样例或未完成真实检索。')+'事实正确性未自动验证。';
  }
  renderSources(s.sources||[]);
  renderTrace(events);
  $('configuration').textContent=JSON.stringify({request:req,provider_config:s.provider_config||{},actual_response_models:s.response_models||[],note:'密钥不保存在任务状态或导出文件中'},null,2);
  $('export-md').href='/api/tasks/'+task.id+'/export?format=markdown';
  $('export-json').href='/api/tasks/'+task.id+'/export?format=json';
  $('stop-task').hidden=!['running','queued'].includes(task.status);
  $('retry-task').hidden=task.status!=='interrupted';
  $('delete-task').hidden=['running','queued'].includes(task.status);
  $('followup-panel').hidden=!['completed','limited'].includes(task.status);
  if(!['queued','running'].includes(task.status)&&polling){clearInterval(polling);polling=null}
}

/* ---------- 报告渲染（markdown-lite） ---------- */
function escHtml(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function inlineMd(t){
  return t
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')
    .replace(/\[(\d{1,3})\]/g,'<span class="cite">[$1]</span>');
}
function renderReport(text){
  const lines=text.replace(/\r/g,'').split('\n');
  const frag=document.createDocumentFragment();
  let list=null,code=null,para=[];
  const flushPara=()=>{if(para.length){const p=document.createElement('p');p.innerHTML=inlineMd(para.join(' '));frag.append(p);para=[]}};
  const flushList=()=>{if(list){frag.append(list);list=null}};
  const flush=()=>{flushPara();flushList()};
  for(const raw of lines){
    const line=raw;
    if(line.trim().startsWith('```')){
      if(code){code.textContent=code.textContent.replace(/\n$/,'');const pre=document.createElement('pre');pre.append(code);frag.append(pre);code=null}
      else{flush();code=document.createElement('code')}
      continue;
    }
    if(code){code.textContent+=line+'\n';continue}
    const esc=escHtml(line);
    if(/^###\s+/.test(line)){flush();const h=document.createElement('h4');h.innerHTML=inlineMd(esc.replace(/^###\s+/,''));frag.append(h);continue}
    if(/^##\s+/.test(line)){flush();const h=document.createElement('h3');h.innerHTML=inlineMd(esc.replace(/^##\s+/,''));frag.append(h);continue}
    if(/^#\s+/.test(line)){flush();const h=document.createElement('h2');h.innerHTML=inlineMd(esc.replace(/^#\s+/,''));frag.append(h);continue}
    if(/^>\s?/.test(line)){flush();const q=document.createElement('blockquote');q.innerHTML=inlineMd(esc.replace(/^>\s?/,''));frag.append(q);continue}
    if(/^\s*[-*]\s+/.test(line)){flushPara();if(!list||list.tagName!=='UL'){flushList();list=document.createElement('ul')}const li=document.createElement('li');li.innerHTML=inlineMd(esc.replace(/^\s*[-*]\s+/,''));list.append(li);continue}
    if(/^\s*\d+[.、]\s*/.test(line)){flushPara();if(!list||list.tagName!=='OL'){flushList();list=document.createElement('ol')}const li=document.createElement('li');li.innerHTML=inlineMd(esc.replace(/^\s*\d+[.、]\s*/,''));list.append(li);continue}
    if(!line.trim()){flush();continue}
    para.push(esc);
  }
  flush();
  $('report').replaceChildren(frag);
}
function safeSourceUrl(url){try{const u=new URL(url);return ['https:','http:'].includes(u.protocol)&&!u.username&&!u.password?u.href:null}catch{return null}}
function renderSources(sources){
  $('source-count').textContent=sources.length;
  $('sources').replaceChildren();
  if(!sources.length){$('sources').append(el('p','hint','当前没有来源。完成检索后将显示标题、链接和片段。'));return}
  for(const src of sources){
    const card=el('section','source-card'),title=el('h3');
    title.append(el('span','',src.id),document.createTextNode(src.title));
    card.append(title);
    const url=safeSourceUrl(src.url);
    if(url){const a=el('a','',src.url+' ↗');a.href=url;a.target='_blank';a.rel='noopener noreferrer';card.append(a)}
    card.append(el('small','',src.retrieved?'已返回检索片段 · 非全文事实核验':'固定离线样例 · 未实时访问网页'),el('p','',src.excerpt));
    $('sources').append(card);
  }
}
function renderTrace(events){
  $('trace').replaceChildren();
  for(const ev of events){
    const li=el('li',ev.status);
    li.append(el('time','',new Date(ev.time*1000).toLocaleTimeString('zh-CN',{hour12:false})),el('strong','',(names[ev.node]||ev.node)+' / '+(evStatusText[ev.status]||ev.status)),el('p','',ev.detail));
    $('trace').append(li);
  }
}

/* ---------- 恢复 / 追问 / 停止 ---------- */
function showHumanDecision(plan,direction,note){
  const panel=$('human-decided');
  panel.hidden=false;
  $('human-decided-note').textContent=note||'已从检查点继续执行，下列内容已固定。';
  const dirWrap=$('human-decided-direction-wrap'),dirText=$('human-decided-direction');
  if(direction){dirWrap.hidden=false;dirText.textContent=direction}
  else{dirWrap.hidden=true;dirText.textContent=''}
  const planWrap=$('human-decided-plan-wrap'),list=$('human-decided-plan');
  list.replaceChildren();
  if(plan&&plan.length){
    planWrap.hidden=false;
    for(const item of plan){
      const li=document.createElement('li');
      li.textContent=item;
      list.append(li);
    }
  }else{
    planWrap.hidden=true;
  }
}
function hideHumanDecision(){$('human-decided').hidden=true}
async function resumeTask(interrupted=false){
  if(!currentId)return;
  const button=$(interrupted?'retry-task':'resume-task');
  // 框内内容预填了当前方向：留空表示保持原方向，有内容才更新。
  const direction=interrupted?'':$('direction').value.trim();
  const planLines=interrupted?null:$('plan-edit').value.split('\n').map(x=>x.trim()).filter(Boolean);
  const originalPlan=(($('plan-edit').dataset.original)||'').split('\n').map(x=>x.trim()).filter(Boolean);
  if(!interrupted&&originalPlan.length&&planLines.length&&planLines.length<Math.max(1,Math.ceil(originalPlan.length/2))){
    if(!window.confirm('当前计划只剩 '+planLines.length+' 项，原先有 '+originalPlan.length+' 项。确认要用精简后的计划继续吗？'))return;
  }
  if(!interrupted&&originalPlan.length&&!planLines.length){
    toast('计划为空，将保持原研究计划不变');
  }
  setBusy(button,true,interrupted?'正在重试…':'正在继续…');
  $('resume-error').hidden=true;
  $('resume-hint').textContent='正在写入检查点并继续执行…';
  try{
    const body=interrupted?{direction:''}:{direction,plan:planLines};
    if(!body.plan?.length)delete body.plan;
    await api('/api/tasks/'+currentId+'/resume',{method:'POST',body:JSON.stringify(body)});
    toast('已确认，正在继续执行');
    showHumanDecision(
      interrupted?null:(body.plan||originalPlan),
      direction||($('direction').dataset.original||''),
      interrupted?'已请求从失败节点重试。':'人工决策已写入检查点并固定，工作流继续执行。'
    );
    stopListening();await refreshTask();listen(currentId);loadHistory();
  }catch(e){
    $('resume-error').textContent=e.message;$('resume-error').hidden=false;
    $('resume-hint').textContent='确认未生效，请修正后重试';
    toast(e.message);
  }
  finally{
    setBusy(button,false);
    button.innerHTML=(interrupted?'重试继续':'确认并继续')+
      '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 8h9M8.5 4.5L12 8l-3.5 3.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }
}
async function followup(){
  const q=$('followup').value.trim();
  if(q.length<3){toast('追问至少需要 3 个字');return}
  const button=$('ask-followup');setBusy(button,true);
  try{
    const req={...currentTask.request,question:q,parent_id:currentId};
    delete req.external_task_id;
    const task=await api('/api/tasks',{method:'POST',body:JSON.stringify(req)});
    $('followup').value='';await openTask(task.id);
  }catch(e){toast(e.message)}
  finally{setBusy(button,false)}
}

/* ---------- 模型管理 ---------- */
const modelPresets={qwen:['https://dashscope.aliyuncs.com/compatible-mode/v1','qwen3.8-flash','openai'],kimi:['https://api.moonshot.cn/v1','kimi-k3','openai'],deepseek:['https://api.deepseek.com/v1','deepseek-chat','openai'],glm:['https://open.bigmodel.cn/api/paas/v4','glm-5.3','openai'],xiaomi:['https://api.xiaomimimo.com/v1','mimo-v2.6-pro','openai'],openai:['https://api.openai.com/v1','gpt-4.1-mini','openai'],anthropic:['https://api.anthropic.com/v1','claude-sonnet-4-5','anthropic'],custom:['','','openai']};
// 仅为可编辑的输入建议，不代表可用性清单；任何版本都可手动填写。
const modelExamples={kimi:['kimi-k3','kimi-k2.7-code','kimi-k2.6'],qwen:['qwen3.7-plus','qwen3.8-max','qwen3.8-flash','qwen3-coder-plus'],deepseek:['deepseek-chat','deepseek-reasoner','deepseek-v4.1-flash','deepseek-v4.1','deepseek-v3.2'],glm:['glm-5.3','glm-5.3-flash','glm-5.2','glm-4.6'],xiaomi:['mimo-v2.6-pro','mimo-v2.6-flash','mimo-v2.6-pro-ultraspeed','mimo-v2.5'],openai:['gpt-4.1-mini','gpt-4.1','gpt-4o-mini','o4-mini'],anthropic:['claude-sonnet-4-5','claude-opus-4-1','claude-haiku-4-5'],custom:[]};
const providerNames={kimi:'Kimi',qwen:'Qwen',deepseek:'DeepSeek',glm:'GLM',xiaomi:'MiMo',openai:'OpenAI',anthropic:'Claude',custom:'自定义'};
const providerLetter={kimi:'K',qwen:'Q',deepseek:'D',glm:'G',xiaomi:'M',openai:'O',anthropic:'A',custom:'C'};
function guessProviderFromModelId(id){
  const s=String(id||'').toLowerCase();
  if(!s)return null;
  if(s.includes('deepseek'))return 'deepseek';
  if(s.includes('kimi')||s.includes('moonshot'))return 'kimi';
  if(s.startsWith('qwen')||s.startsWith('qwq')||s.includes('qwen'))return 'qwen';
  if(s.startsWith('glm')||s.includes('chatglm')||s.includes('glm-'))return 'glm';
  if(s.startsWith('mimo')||s.includes('xiaomi'))return 'xiaomi';
  if(s.startsWith('gpt-')||/^o[0-9]/.test(s)||s.includes('openai'))return 'openai';
  if(s.includes('claude'))return 'anthropic';
  return null;
}
function syncProviderFromModelId(){
  const input=$('model-id');
  const guess=guessProviderFromModelId(input.value.trim());
  if(guess&&$('model-provider').value!==guess){
    $('model-provider').value=guess;
    // 保留用户已填写的模型 ID，只同步厂商与接口预设。
    const keep=input.value;
    const touched=input.dataset.touched;
    modelPreset();
    input.value=keep;
    if(touched)input.dataset.touched=touched;
    $('model-version').textContent='已根据模型 ID 自动切换厂商为 '+providerNames[guess];
  }
}
let modelConfigs=[],editingModel=null;
function syncModelSuggestions(){
  const provider=$('model-provider').value;
  $('model-id-list').innerHTML=(modelExamples[provider]||[]).map(m=>'<option value="'+m+'"></option>').join('');
}
function modelPreset(){
  const provider=$('model-provider').value,values=modelPresets[provider];
  $('model-url').value=values[0];
  const input=$('model-id');
  // 仅在用户未手动改过时同步默认模型 ID，避免覆盖已填写内容。
  if(!input.dataset.touched||input.value===(input.dataset.preset||''))input.value=values[1];
  input.dataset.preset=values[1];
  $('model-protocol').value=values[2];
  $('model-protocol').disabled=provider!=='custom';
  $('model-temperature').max=values[2]==='anthropic'?'1':'2';
  if(Number($('model-temperature').value)>Number($('model-temperature').max))$('model-temperature').value=0;
  // Kimi K2/K3 系列仅接受 temperature=1，避免默认 0 导致接口 400。
  if(provider==='kimi'&&!$('model-temperature').dataset.touched)$('model-temperature').value=1;
  syncModelSuggestions();
}
function resetModel(){
  editingModel=null;$('model-form').reset();
  $('model-form-title').textContent='添加模型';
  $('model-version').textContent='密钥不会写入任务、日志或导出。';
  $('restore-credential-section').hidden=true;
  $('model-key').type='password';$('show-model-key').textContent='显示';$('show-model-key').setAttribute('aria-pressed','false');
  $('model-error').hidden=true;
  const input=$('model-id');delete input.dataset.touched;delete input.dataset.preset;
  modelPreset();
}
function editModel(model){
  resetModel();editingModel=model.id;
  $('model-form-title').textContent='编辑模型';
  for(const [field,key] of [['model-name','name'],['model-provider','provider'],['model-protocol','protocol'],['model-id','model'],['model-url','base_url'],['model-temperature','temperature'],['model-max-tokens','max_tokens'],['model-tool-mode','tool_mode'],['model-input-price','input_price'],['model-output-price','output_price']])$(field).value=model[key]??'';
  $('model-supports-tools').checked=model.supports_tools;
  $('model-supports-temperature').checked=model.supports_temperature!==false;
  const mid=$('model-id');mid.dataset.preset=model.model||'';delete mid.dataset.touched;
  syncModelSuggestions();
  $('model-protocol').disabled=model.provider!=='custom';
  $('model-temperature').max=model.protocol==='anthropic'?'1':'2';
  $('model-version').textContent='当前版本 '+model.config_version+' · '+(model.configured?'已填写密钥':'需要补充密钥');
  $('restore-credential-section').hidden=false;
  $('restore-credential-id').value=model.credential_id;
  $('model-form').scrollIntoView({block:'start',behavior:'smooth'});
}
async function loadModels(){
  const data=await api('/api/models');
  modelConfigs=data.models;
  const selected=$('model-config').value;
  $('model-config').replaceChildren();
  if(!modelConfigs.length)$('model-config').append(new Option('请先在模型管理添加模型',''));
  for(const model of modelConfigs)$('model-config').append(new Option(model.name+' · '+model.model+(model.configured?'':' · 需补密钥'),model.id));
  const preferred=modelConfigs.find(m=>m.configured)||modelConfigs.find(m=>m.id===data.default_model_config_id)||modelConfigs[0];
  $('model-config').value=modelConfigs.some(m=>m.id===selected)?selected:(preferred?.id||'');
  $('model-count').textContent=modelConfigs.length+' 项';
  if(modelConfigs.some(m=>m.configured)&&$('execution-mode').value!=='live'){$('execution-mode').value='live';}
  // 下拉选中的模型同步到厂商字段，保持上下一致。
  const current=modelConfigs.find(m=>m.id===$('model-config').value);
  if(current&&$('provider').value!==current.provider)$('provider').value=current.provider;
  syncExecutionUI();
  $('model-list').replaceChildren();
  if(!modelConfigs.length)$('model-list').append(el('p','hint','还没有模型。填写左侧表单保存后，再测试连接。'));
  for(const model of modelConfigs){
    const card=el('div','model-card'+(model.id===data.default_model_config_id?' default':''));
    const h=el('h3','',model.name);
    if(model.id===data.default_model_config_id)h.append(el('span','tag-default','默认'));
    card.append(h,
      el('p','meta',model.provider+' / '+model.protocol+' / '+model.model),
      el('p','meta',model.base_url));
    // Status badges: missing key / untested / connected / config changed / revoked
    const statusRow=el('div','model-status-row');
    if(!model.configured){
      statusRow.append(el('span','badge warn','缺 Key'));
    }else{
      statusRow.append(el('span','badge done','已填写密钥'));
    }
    if(model.last_test){
      const testOk=model.last_test.ok;
      const testCurrent=model.last_test.config_version===model.config_version;
      if(testCurrent&&testOk)statusRow.append(el('span','badge done','连接成功'));
      else if(testCurrent&&!testOk)statusRow.append(el('span','badge error','连接失败'));
      else statusRow.append(el('span','badge warn','配置已更改 · 未重测'));
    }else{
      statusRow.append(el('span','badge','未测试'));
    }
    if(model.credential_source==='env_import')statusRow.append(el('span','badge','环境导入'));
    else if(model.credential_source==='legacy_unclassified')statusRow.append(el('span','badge','历史配置'));
    card.append(statusRow);
    card.append(el('p','cred'+(model.configured?'':' missing'),model.configured?'配置版本 '+model.config_version:'重启后需补充密钥'));
    const actions=el('div','model-actions'),feedback=el('div','model-feedback');
    feedback.hidden=true;
    const action=(text,fn)=>{const b=el('button','btn btn-ghost btn-sm',text);b.type='button';b.addEventListener('click',async()=>{b.disabled=true;try{await fn(b)}catch(e){feedback.hidden=false;feedback.dataset.ok='false';feedback.textContent=e.message}finally{b.disabled=false}});actions.append(b)};
    action('编辑',()=>editModel(model));
    action('设为默认',async()=>{await api('/api/models/'+model.id+'/default',{method:'POST'});await loadModels();toast('已设为默认模型')});
    action('测试连接',async()=>{
      feedback.hidden=false;feedback.dataset.ok='';feedback.textContent='正在发送最小模型请求，会产生 API 费用…';
      const result=await api('/api/models/'+model.id+'/test',{method:'POST'});
      feedback.dataset.ok=String(result.ok);
      feedback.textContent=result.message+'\n'+result.category+' · '+result.latency_ms+' ms';
    });
    action('创建联动授权',async()=>{
      const grant=await api('/api/integration/grants',{method:'POST',body:JSON.stringify({model_config_id:model.id})});
      $('grant-token').value=grant.token;
      $('grant-config-id').textContent='模型配置 ID：'+model.id;
      $('grant-once').hidden=false;await loadGrants();
      $('grant-once').scrollIntoView({block:'nearest'});
    });
    action('删除密钥',async()=>{await api('/api/models/'+model.id+'/credential',{method:'DELETE'});await loadModels();toast('已删除该模型的内存密钥；原任务将暂停等待凭证')});
    action('删除配置',async()=>{await api('/api/models/'+model.id,{method:'DELETE'});if(editingModel===model.id)resetModel();await loadModels();await loadGrants();clearGrant()});
    card.append(actions,feedback);
    if(model.last_test?.config_version===model.config_version){
      const result=readableConnection(model.last_test);
      feedback.hidden=false;feedback.dataset.ok=String(result.ok);
      feedback.textContent=result.message+'\n'+result.category+' · '+result.latency_ms+' ms · '+new Date(result.tested_at*1000).toLocaleString('zh-CN');
    }
    $('model-list').append(card);
  }
}
async function loadGrants(){
  const data=await api('/api/integration/grants');
  $('grant-list').replaceChildren();
  for(const grant of data.grants){
    const row=el('div','grant-row');
    row.append(el('span','','模型 '+(modelConfigs.find(m=>m.id===grant.model_config_id)?.name||grant.model_config_id)+' · 到期 '+new Date(grant.expires*1000).toLocaleString('zh-CN')));
    const b=el('button','btn btn-ghost btn-sm','撤销授权');
    b.type='button';
    b.addEventListener('click',async()=>{try{await api('/api/integration/grants/'+grant.id,{method:'DELETE'});clearGrant();await loadGrants();toast('联动授权已撤销')}catch(e){toast(e.message)}});
    row.append(b);
    $('grant-list').append(row);
  }
}

/* ---------- 移动端导航 ---------- */
function closeMenu(){$('sidebar').classList.remove('open');$('scrim').hidden=true;$('menu-toggle').setAttribute('aria-expanded','false')}

/* ---------- 事件绑定 ---------- */
$('new-task').addEventListener('click',showCreate);
$('task-form').addEventListener('submit',submitTask);
$$('[data-example]').forEach(b=>b.addEventListener('click',()=>{$('question').value=b.dataset.example;$('question').focus()}));
bindSegment('mode-segment','mode',()=>{
  $('review-plan').disabled=$('mode').value==='single';
  if($('mode').value==='single')$('review-plan').checked=false;
});
$('mode').addEventListener('change',()=>{$('review-plan').disabled=$('mode').value==='single';if($('mode').value==='single')$('review-plan').checked=false});
/* composer：模型 pill / 联网搜索开关 / 设置 */
$('model-pill').addEventListener('click',()=>{
  const menu=$('pill-menu'),open=menu.hidden;
  if(open)renderPillMenu();
  menu.hidden=!open;
  $('model-pill').setAttribute('aria-expanded',String(open));
});
document.addEventListener('click',e=>{
  if(!e.target.closest('.pill-wrap'))closePillMenu();
});
function setExecutionMode(mode){
  if(mode==='live'&&!modelConfigs.length){toast('请先在「模型管理」添加模型和密钥');return}
  $('execution-mode').value=mode;
  syncExecutionUI();
}
for(const btn of document.querySelectorAll('.mode-btn')){
  btn.addEventListener('click',()=>setExecutionMode(btn.dataset.mode));
}
$('composer-settings').addEventListener('click',()=>{
  const panel=$('composer-advanced'),open=panel.hidden;
  panel.hidden=!open;
  $('composer-settings').setAttribute('aria-expanded',String(open));
});
/* 快速开始（可选节点） */
if($('quick-example'))$('quick-example').addEventListener('click',()=>{
  showCreate();
  $('question').value=$('[data-example]').dataset.example;
  $('question').focus();
  toast('已填入示例问题，可直接开始研究。');
});
const guideContent=`
<p><b>1. 添加模型。</b>在「模型管理」选择服务商，填写账号可用的模型 ID、接口地址与密钥，保存后测试连接。密钥只保留在内存中。</p>
<p><b>2. 提出研究问题。</b>在「研究工作台」输入问题；点击模型胶囊切换要用的模型配置；打开「联网搜索」即真实研究模式，会联网检索并产生 API 费用。</p>
<p><b>3. 审核计划。</b>多智能体模式下 Planner 会先给出研究计划，你可以修改后再开始检索；执行过程里每个智能体的状态、耗时和事件都可展开查看。</p>
<p><b>4. 查看与导出。</b>最终在「最终报告」查看带引用编号的报告，可核对「来源列表」，并导出 Markdown 或 JSON。</p>
<p><b>5. 继续追问。</b>任务完成后可基于报告新建追问；中断的任务可从检查点继续。</p>`;
const faqContent=`
<p><b>真实研究和离线演示有什么区别？</b><br>真实研究调用你配置的模型并联网检索；离线演示使用固定样例资料，只验证流程，不产生费用。</p>
<p><b>密钥保存在哪里？</b><br>只在本地服务的内存中。重启服务后需要重新填写；任务、日志和导出文件中都不会包含密钥。</p>
<p><b>模型 ID 可以随便填吗？</b><br>可以手动输入任意模型版本，下拉列表只是常用示例。以你的账号权限为准，保存前可「测试连接」验证。</p>
<p><b>费用为什么有时显示未知？</b><br>模型未返回用量或未配置单价时，费用保持「未知」，不会按零计算。</p>
<p><b>任务中断了怎么办？</b><br>在任务页点击「从检查点继续」；若提示原凭证缺失，到「模型管理」补充同一密钥后恢复。</p>`;
function openDialog(title,html){
  $('dialog-title').textContent=title;
  $('dialog-body').innerHTML=html;
  const dlg=$('info-dialog');
  if(typeof dlg.showModal==='function')dlg.showModal();else dlg.setAttribute('open','');
}
if($('quick-guide'))$('quick-guide').addEventListener('click',()=>openDialog('使用指南',guideContent));
if($('quick-faq'))$('quick-faq').addEventListener('click',()=>openDialog('常见问题',faqContent));
$('dialog-close').addEventListener('click',()=>$('info-dialog').close());
$('info-dialog').addEventListener('click',e=>{if(e.target===$('info-dialog'))$('info-dialog').close()});
$$('.tab').forEach(b=>b.addEventListener('click',()=>{
  $$('.tab').forEach(x=>{x.classList.toggle('active',x===b);x.setAttribute('aria-selected',String(x===b))});
  for(const name of ['report','sources','trace','config'])$('tab-'+name).hidden=name!==b.dataset.tab;
}));
$('resume-task').addEventListener('click',()=>resumeTask());
$('retry-task').addEventListener('click',()=>resumeTask(true));
$('restore-plan').addEventListener('click',()=>{
  const original=$('plan-edit').dataset.original||'';
  $('plan-edit').value=original;
  toast('已恢复原研究计划');
});
$('delete-task').addEventListener('click',async()=>{
  if(!currentId)return;
  if(!await confirmArchive())return;
  const button=$('delete-task');setBusy(button,true,'正在删除…');
  try{
    await api('/api/tasks/'+currentId,{method:'DELETE'});
    toast('已移入回收站');
    showCreate();
  }catch(e){toast(e.message)}
  finally{
    setBusy(button,false);
    button.textContent='删除此记录';
  }
});
$('stop-task').addEventListener('click',async()=>{try{await api('/api/tasks/'+currentId+'/stop',{method:'POST'});toast('已请求停止，当前网络调用结束后生效。')}catch(e){toast(e.message)}});
$('ask-followup').addEventListener('click',followup);
$('claim-history').addEventListener('click',async()=>{try{const result=await api('/api/history/claim',{method:'POST'});toast('已认领 '+result.claimed+' 项历史研究');$('legacy-history').hidden=true;await loadHistory();await loadModels()}catch(e){toast(e.message)}});
$$('.side-link').forEach(b=>b.addEventListener('click',()=>{
  const nav=b.dataset.nav;
  if(nav==='models')showModels();
  else if(nav==='history')showHistory();
  else if(nav==='library')showLibrary();
  else if(nav==='tools')showTools();
  else if(nav==='settings')showSettings();
  else showCreate();
}));
$('model-provider').addEventListener('change',modelPreset);
$('model-id').addEventListener('input',()=>{$('model-id').dataset.touched='1';syncProviderFromModelId()});
$('model-config').addEventListener('change',()=>{
  const m=modelConfigs.find(x=>x.id===$('model-config').value);
  if(m&&$('provider').value!==m.provider)$('provider').value=m.provider;
  renderPill();
});
$('model-protocol').addEventListener('change',()=>{$('model-temperature').max=$('model-protocol').value==='anthropic'?'1':'2'});
$('show-model-key').addEventListener('click',()=>{
  const shown=$('model-key').type==='password';
  $('model-key').type=shown?'text':'password';
  $('show-model-key').textContent=shown?'隐藏':'显示';
  $('show-model-key').setAttribute('aria-pressed',String(shown));
});
document.addEventListener('visibilitychange',()=>{if(document.hidden){$('model-key').type='password';$('show-model-key').textContent='显示'}});
$('model-reset').addEventListener('click',resetModel);
$('model-form').addEventListener('submit',async e=>{
  e.preventDefault();const b=$('save-model');b.disabled=true;$('model-error').hidden=true;
  try{
    const body={name:$('model-name').value.trim(),provider:$('model-provider').value,protocol:$('model-protocol').value,model:$('model-id').value.trim(),base_url:$('model-url').value.trim(),temperature:Number($('model-temperature').value),max_tokens:Number($('model-max-tokens').value),supports_tools:$('model-supports-tools').checked,supports_temperature:$('model-supports-temperature').checked,tool_mode:$('model-tool-mode').value,input_price:$('model-input-price').value===''?null:Number($('model-input-price').value),output_price:$('model-output-price').value===''?null:Number($('model-output-price').value)};
    if($('model-key').value)body.api_key=$('model-key').value;
    await api('/api/models'+(editingModel?'/'+editingModel:''),{method:editingModel?'PUT':'POST',body:JSON.stringify(body)});
    resetModel();await loadModels();toast('模型已保存；密钥仅保留在服务内存中');
  }catch(e){$('model-error').textContent=e.message;$('model-error').hidden=false}
  finally{b.disabled=false}
});
$('restore-credential').addEventListener('click',async()=>{
  if(!editingModel||!$('model-key').value){toast('请在上方填写原任务使用的密钥');return}
  try{
    await api('/api/models/'+editingModel+'/credentials',{method:'POST',body:JSON.stringify({credential_id:$('restore-credential-id').value.trim(),api_key:$('model-key').value})});
    $('model-key').value='';await loadModels();toast('原凭证已恢复；回到研究记录继续任务');
  }catch(e){$('model-error').textContent=e.message;$('model-error').hidden=false}
});
$('copy-grant').addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('grant-token').value);toast('令牌已复制')}catch{$('grant-token').select();toast('请复制选中的令牌')}});
$('import-env-model').addEventListener('click',async()=>{try{await api('/api/models/import-env/'+$('model-provider').value,{method:'POST'});await loadModels();toast('环境配置已认领到当前浏览器，密钥仅在服务内存中')}catch(e){$('model-error').textContent=e.message;$('model-error').hidden=false}});
$('menu-toggle').addEventListener('click',()=>{
  const open=!$('sidebar').classList.contains('open');
  $('sidebar').classList.toggle('open',open);
  $('scrim').hidden=!open;
  $('menu-toggle').setAttribute('aria-expanded',String(open));
});
$('scrim').addEventListener('click',closeMenu);
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMenu()});

/* ---------- 启动 ---------- */
api('/api/health').then(()=>setConn(true)).catch(()=>setConn(false));
loadHistory();
modelPreset();syncExecutionUI();
loadModels().then(()=>{
  // 已有可用模型时默认真实研究，否则保持离线演示。
  if(modelConfigs.length)$('execution-mode').value='live';
  syncExecutionUI();
}).catch(()=>{});
function applyHash(){
  const m=location.hash.match(/^#task=([a-f0-9]+)$/);
  if(m)openTask(m[1]);
  else if(location.hash==='#models')showModels();
  else if(location.hash==='#history')showHistory();
  else if(location.hash==='#library')showLibrary();
  else if(location.hash==='#tools')showTools();
  else if(location.hash==='#settings')showSettings();
}
addEventListener('hashchange',applyHash);
applyHash();

const trashButton=el('button','btn btn-ghost','回收站 / 恢复任务');
api('/api/environment/conflict').then(info=>{if(!info.count)return;const panel=el('div','notice','检测到历史环境凭证归属冲突。普通导入已暂停，请由冲突记录中的原会话显式确认。');if(info.can_resolve){const resolve=el('button','btn btn-ghost','确认本会话为环境凭证归属者');resolve.onclick=async()=>{try{await api('/api/environment/conflict/resolve',{method:'POST',body:JSON.stringify({confirm:true})});panel.remove();toast('已记录修复审计；可显式导入环境模型')}catch(e){toast(e.message)}};panel.append(resolve)}$('models-view').prepend(panel)}).catch(e=>toast(e.message));
function confirmArchive(){return new Promise(resolve=>{const origin=document.activeElement,dialog=document.createElement('dialog');dialog.style='max-width:560px;width:90%;padding:24px;border:1px solid #ddd;border-radius:16px';dialog.append(el('h2','','移入回收站'),el('p','','检查点、日志与调用记录均保留，可从研究历史的回收站恢复。'));const cancel=el('button','btn btn-ghost','取消'),submit=el('button','btn btn-primary','确认移入回收站');const finish=value=>{dialog.close();dialog.remove();origin?.focus();resolve(value)};cancel.onclick=()=>finish(false);submit.onclick=()=>finish(true);dialog.addEventListener('cancel',event=>{event.preventDefault();finish(false)});dialog.append(cancel,submit);document.body.append(dialog);dialog.showModal();cancel.focus()});}
$('history-view').querySelector('header').append(trashButton);
trashButton.onclick=async()=>{try{const tasks=await api('/api/tasks?archived=true');const dialog=document.createElement('dialog');dialog.style='max-width:780px;width:90%;padding:24px';const close=el('button','btn btn-ghost','关闭');close.onclick=()=>{dialog.close();dialog.remove()};dialog.append(el('h2','','回收站'),close);if(!tasks.length)dialog.append(el('p','','回收站为空'));for(const task of tasks){const row=el('p','',task.question);const restore=el('button','btn btn-primary','恢复');restore.onclick=async()=>{await api('/api/tasks/'+task.id+'/restore',{method:'POST'});row.remove();await loadHistory();toast('研究任务已恢复')};row.append(restore);dialog.append(row)}document.body.append(dialog);dialog.showModal()}catch(e){toast(e.message)}};
