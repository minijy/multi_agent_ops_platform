const state={page:'dashboard',summary:null,runs:[],approvals:[],catalog:null,configuration:null,models:null,editingModel:null,audit:[],session:{apiKey:localStorage.getItem('arkflow.apiKey')||'',tenant:localStorage.getItem('arkflow.tenant')||'tenant-a',role:localStorage.getItem('arkflow.role')||'admin'},agentChat:{sessionId:null,sessions:[],events:[],subagents:[],pendingAttachments:[],selectedModelId:localStorage.getItem('arkflow.modelId')||'',sending:false,cooldownUntil:0,stream:null,motionKeys:new Set(),stickToBottom:true}};
const $=(selector,root=document)=>root.querySelector(selector);const $$=(selector,root=document)=>[...root.querySelectorAll(selector)];
const escapeHTML=value=>String(value??'').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
function visibleAssistantText(content){if(!content)return'';return String(content).replace(/\{\s*"(?:index|finish_reason|tool_calls)"[\s\S]*\}\s*/g,'').trim()}
function splitMarkdownRow(line){
  return String(line||'').trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(cell=>cell.trim());
}
function isMarkdownTableRow(line){
  const value=String(line||'').trim();
  if(!value.includes('|'))return false;
  return /^\|.*\|$/.test(value)||(value.match(/\|/g)||[]).length>=2;
}
function isMarkdownTableSep(line){
  const cells=splitMarkdownRow(line);
  return cells.length>1&&cells.every(cell=>/^:?-{3,}:?$/.test(cell.replace(/\s/g,'')));
}
function inlineMarkdown(text){
  let value=escapeHTML(text);
  value=value.replace(/`([^`]+)`/g,'<code class="md-inline">$1</code>');
  value=value.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  value=value.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
  value=value.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g,(_,label,href)=>{
    const raw=String(href||'').replace(/&amp;/g,'&');
    if(/^https?:\/\//i.test(raw)||/^mailto:/i.test(raw)){
      return `<a href="${href}" target="_blank" rel="noreferrer">${label}</a>`;
    }
    const path=raw.replace(/^(sandbox:|file:\/\/|file:)/i,'');
    if(!path)return label;
    return `<a href="#" class="workspace-file-link" data-workspace-path="${escapeHTML(path)}">${label}</a>`;
  });
  return value.replace(/\n/g,'<br>');
}
function renderMarkdownBlocks(text){
  const lines=String(text||'').split('\n');
  const html=[];
  let index=0;
  while(index<lines.length){
    const line=lines[index];
    if(!line.trim()){index+=1;continue}
    if(isMarkdownTableRow(line)&&index+1<lines.length&&isMarkdownTableSep(lines[index+1])){
      const header=splitMarkdownRow(line);
      index+=2;
      const rows=[];
      while(index<lines.length&&isMarkdownTableRow(lines[index])&&!isMarkdownTableSep(lines[index])){
        rows.push(splitMarkdownRow(lines[index]));
        index+=1;
      }
      html.push(`<div class="md-table-wrap"><table class="md-table"><thead><tr>${header.map(cell=>`<th>${inlineMarkdown(cell)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${header.map((_,col)=>`<td>${inlineMarkdown(row[col]||'')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
      continue;
    }
    const heading=line.match(/^(#{1,6})\s+(.+)$/);
    if(heading){
      const level=heading[1].length;
      html.push(`<h${level} class="md-h">${inlineMarkdown(heading[2])}</h${level}>`);
      index+=1;
      continue;
    }
    if(/^[-*]\s+/.test(line)){
      const items=[];
      while(index<lines.length&&/^[-*]\s+/.test(lines[index])){
        items.push(`<li>${inlineMarkdown(lines[index].replace(/^[-*]\s+/,''))}</li>`);
        index+=1;
      }
      html.push(`<ul class="md-list">${items.join('')}</ul>`);
      continue;
    }
    if(/^\d+\.\s+/.test(line)){
      const items=[];
      while(index<lines.length&&/^\d+\.\s+/.test(lines[index])){
        items.push(`<li>${inlineMarkdown(lines[index].replace(/^\d+\.\s+/,''))}</li>`);
        index+=1;
      }
      html.push(`<ol class="md-list">${items.join('')}</ol>`);
      continue;
    }
    const para=[];
    while(index<lines.length&&lines[index].trim()&&!(isMarkdownTableRow(lines[index])&&index+1<lines.length&&isMarkdownTableSep(lines[index+1]))&&!/^(#{1,6})\s+/.test(lines[index])&&!/^[-*]\s+/.test(lines[index])&&!/^\d+\.\s+/.test(lines[index])){
      para.push(lines[index]);
      index+=1;
    }
    html.push(`<p>${inlineMarkdown(para.join('\n'))}</p>`);
  }
  return html.join('');
}
function renderMarkdown(source){
  const text=String(source||'').replace(/\r\n/g,'\n');
  const chunks=[];
  const fence=/```([a-zA-Z0-9_-]*)\n?([\s\S]*?)```/g;
  let last=0;
  let match;
  while((match=fence.exec(text))){
    if(match.index>last)chunks.push({type:'md',text:text.slice(last,match.index)});
    chunks.push({type:'code',text:match[2].replace(/\n$/,'')});
    last=match.index+match[0].length;
  }
  if(last<text.length)chunks.push({type:'md',text:text.slice(last)});
  if(!chunks.length)return renderMarkdownBlocks(text);
  return chunks.map(chunk=>chunk.type==='code'?`<pre class="md-code"><code>${escapeHTML(chunk.text)}</code></pre>`:renderMarkdownBlocks(chunk.text)).join('');
}
let agentTranscriptObserver=null;
let agentScrollFrame=0;
function agentMessageList(){return $('#agent-message-list')}
function isAgentNearBottom(root){return root.scrollHeight-root.scrollTop-root.clientHeight<120}
function scrollAgentTranscript(force=false){
  const root=agentMessageList();
  if(!root)return;
  if(!force&&!state.agentChat.stickToBottom)return;
  const pin=()=>{
    const list=agentMessageList();
    if(!list)return;
    list.scrollTop=list.scrollHeight;
    const anchor=$('#agent-scroll-anchor',list)||list.lastElementChild;
    if(anchor)anchor.scrollIntoView({block:'end',inline:'nearest',behavior:'auto'});
  };
  pin();
  if(agentScrollFrame)return;
  agentScrollFrame=requestAnimationFrame(()=>{
    agentScrollFrame=0;
    pin();
  });
}
function bindAgentTranscriptScroll(){
  const root=agentMessageList();
  if(!root||root.dataset.scrollBound)return;
  root.dataset.scrollBound='1';
  root.addEventListener('scroll',()=>{state.agentChat.stickToBottom=isAgentNearBottom(root)},{passive:true});
}
function watchAgentTranscriptSize(){
  const root=agentMessageList();
  if(!root||typeof ResizeObserver==='undefined')return;
  if(!agentTranscriptObserver){
    agentTranscriptObserver=new ResizeObserver(()=>{
      if(state.agentChat.stickToBottom)scrollAgentTranscript();
    });
  }
  agentTranscriptObserver.disconnect();
  agentTranscriptObserver.observe(root);
  const live=root.querySelector('.chat-turn.live, #agent-stream-text, #agent-thinking');
  if(live)agentTranscriptObserver.observe(live);
}
const statusLabels={started:'已启动',collecting_evidence:'收集证据',planning_action:'生成计划',waiting_approval:'等待审批',executing:'执行中',reviewing:'审核中',completed:'已完成',rejected:'已拒绝',review_failed:'审核失败'};
const agentLabels={supervisor:'Supervisor',knowledge_agent:'Knowledge Agent',telemetry_agent:'Telemetry Agent',diagnosis_agent:'Diagnosis Agent',action_agent:'Action Agent',approval_gate:'Approval Gate',reviewer_agent:'Reviewer',finalizer:'Finalizer'};
function headers(){const value={'Content-Type':'application/json','X-Tenant-ID':state.session.tenant,'X-User-ID':'local-admin','X-User-Role':state.session.role};if(state.session.apiKey)value['X-API-Key']=state.session.apiKey;return value}
async function api(path,options={}){const response=await fetch(path,{...options,headers:{...headers(),...(options.headers||{})}});if(!response.ok){let payload={};try{payload=await response.json()}catch{}const detail=payload.detail;const message=typeof detail==='object'&&detail?detail.message:(detail||`请求失败 (${response.status})`);const error=new Error(message);if(typeof detail==='object'&&detail){error.code=detail.code;error.retryAfterSeconds=detail.retry_after_seconds;error.provider=detail.provider}throw error}return response.json()}
async function downloadWorkspaceFile(path, sourceLink){
  if(!path){toast('没有可下载的文件','error');return}
  try{
    const auth={...headers()};
    delete auth['Content-Type'];
    const response=await fetch(`/v1/agent/workspace/file?path=${encodeURIComponent(path)}`,{headers:auth});
    if(response.ok){
      await saveBlobResponse(response,path);
      return;
    }
    const recovered=recoverSandboxFile(path);
    if(recovered){
      const file=asCsvDownload(recovered);
      saveTextFile(file.name,file.payload);
      toast(`已下载 ${file.name}`,'success');
      return;
    }
    let payload={};
    try{payload=await response.json()}catch{}
    const detail=payload.detail;
    const message=typeof detail==='string'?detail:`下载失败 (${response.status})`;
    if(sourceLink){
      sourceLink.classList.add('workspace-file-missing');
      sourceLink.title=message;
    }
    throw new Error(message==='file not found'?'工作区中找不到该文件，命令可能没有真正写入':message);
  }catch(error){
    toast(error.message,'error');
  }
}
function recoverSandboxFile(path){
  const name=String(path||'').split(/[\\/]/).pop();
  if(!name)return null;
  const aliases=new Set([name,name.replace(/\.csv$/i,'.txt'),name.replace(/\.txt$/i,'.csv')]);
  for(const event of [...state.agentChat.events].reverse()){
    if(event.event_type!=='tool.completed')continue;
    const output=event.payload?.output;
    const parsed=echoFileFromCommand(output?.command);
    if(parsed&&aliases.has(parsed.name))return parsed;
    const files=output?.written_files||[];
    if(files.some(item=>aliases.has(String(item).split(/[\\/]/).pop()))&&output?.stdout){
      return {name,payload:String(output.stdout)};
    }
  }
  return null;
}
function unwrapEchoPayload(text){
  let value=String(text||'').replace(/^\ufeff/, '');
  const pairs={'"':'"',"'":"'",'\u201c':'\u201d','\u2018':'\u2019'};
  for(let i=0;i<2;i+=1){
    const stripped=value.replace(/^\s+|\s+$/g,'');
    if(stripped.length<2)break;
    const closer=pairs[stripped[0]];
    if(!closer||stripped[stripped.length-1]!==closer)break;
    const first=stripped.split('\n',1)[0];
    if(firstLineIsCsvQuotedField(first,closer))break;
    value=stripped.slice(1,-1).replace(/\\"/g,'"');
  }
  return value;
}
function firstLineIsCsvQuotedField(firstLine,closer){
  for(let i=1;i<firstLine.length;i+=1){
    if(firstLine[i]!==closer)continue;
    if(firstLine[i+1]===closer){i+=1;continue;}
    const rest=firstLine.slice(i+1);
    return rest===''||rest.startsWith(',');
  }
  return false;
}
function csvCell(value){
  let cell=String(value||'').trim();
  if(cell.length>=2&&cell.startsWith('"')&&cell.endsWith('"')&&(cell.match(/"/g)||[]).length===2)cell=cell.slice(1,-1);
  else if(cell.startsWith('"')&&(cell.match(/"/g)||[]).length===1)cell=cell.slice(1);
  else if(cell.endsWith('"')&&(cell.match(/"/g)||[]).length===1)cell=cell.slice(0,-1);
  if(/[",\n]/.test(cell))return`"${cell.replace(/"/g,'""')}"`;
  return cell;
}
function tsvToCsv(text){
  const payload=unwrapEchoPayload(text);
  return payload.split('\n').map(line=>{
    if(!line.includes('\t'))return line;
    return line.split('\t').map(csvCell).join(',');
  }).join('\n');
}
function asCsvDownload(file){
  let payload=unwrapEchoPayload(file.payload||'');
  if(payload.includes('\t'))payload=tsvToCsv(payload);
  const name=String(file.name||'download.txt').replace(/\.txt$/i,'.csv');
  return {name,payload};
}
function echoFileFromCommand(command){
  if(!Array.isArray(command)||command.length<3)return null;
  const bin=String(command[0]||'').split(/[\\/]/).pop();
  if(bin!=='echo'&&bin!=='printf')return null;
  const args=command.slice(1);
  let interpret=false;
  const rest=[];
  for(const item of args){
    if(!rest.length&&['-e','-n','-E'].includes(item)){
      if(item==='-e')interpret=true;
      continue;
    }
    rest.push(item);
  }
  if(rest.length<2)return null;
  const name=String(rest.at(-1)||'').split(/[\\/]/).pop();
  if(!/\.[A-Za-z0-9]{1,12}$/.test(name))return null;
  let payload=rest.slice(0,-1).join(' ');
  if(interpret)payload=payload.replace(/\\n/g,'\n').replace(/\\t/g,'\t').replace(/\\r/g,'\r');
  return {name,payload:unwrapEchoPayload(payload)};
}
async function saveBlobResponse(response,path){
  const blob=await response.blob();
  const disposition=response.headers.get('content-disposition')||'';
  const named=/filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(disposition);
  const filename=decodeURIComponent((named&&(named[1]||named[2]))||path.split(/[\\/]/).pop()||'download');
  const url=URL.createObjectURL(blob);
  const link=document.createElement('a');
  link.href=url;
  link.download=filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
function saveTextFile(name,text){
  const csv=/\.csv$/i.test(name||'');
  const body=csv&&!text.startsWith('\ufeff')?`\ufeff${text}`:text;
  const blob=new Blob([body],{type:csv?'text/csv;charset=utf-8':'text/plain;charset=utf-8'});
  const url=URL.createObjectURL(blob);
  const link=document.createElement('a');
  link.href=url;
  link.download=name||'download.txt';
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
function formatTime(value){if(!value)return'—';return new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(value))}
function modelLabel(provider,model){return [provider,model].map(value=>String(value||'').trim()).filter(Boolean).join(' / ')||'Agent'}
function setAgentSessionLabel(id){
  const label=id||'新会话';
  const short=id&&id.length>16?`${id.slice(0,8)}…${id.slice(-4)}`:label;
  const el=$('#agent-session-id');
  if(el)el.textContent=short;
  const pill=$('#agent-session-pill');
  if(pill)pill.title=label;
  const title=$('#agent-chat-title');
  if(title){
    const current=state.agentChat.sessions.find(item=>item.id===id);
    title.textContent=current?.title||(id?'当前会话':'新会话');
  }
}
function statusChip(value){return`<span class="status-chip ${escapeHTML(value)}">${escapeHTML(statusLabels[value]||value)}</span>`}
function risk(value){return`<span class="risk ${escapeHTML(value||'unknown')}">${escapeHTML(({high:'高风险',medium:'中风险',low:'低风险'})[value]||'待判断')}</span>`}
let toastTimer=0;
function toast(message,kind='info'){
  const el=$('#toast');
  if(!el)return;
  el.innerHTML=`<b>${kind==='error'?'出错了':kind==='success'?'已完成':'提示'}</b><span>${escapeHTML(message)}</span>`;
  el.className=`toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>el.classList.remove('show'),kind==='error'?7000:3200);
}
async function loadHealth(){try{const data=await api('/health');$('#health-label').textContent=`${data.environment} · ${data.session_events}`;$('#health-dot').style.background='#49c699';$('.environment span').textContent=data.environment}catch(error){$('#health-label').textContent='服务不可用';$('#health-dot').style.background='#d75a55'}}
async function loadDashboard(){const data=await api('/v1/dashboard/summary');state.summary=data;const metrics=[['待审批工具',data.waiting_approval,'逐次工具审批','warning'],['审计事件',data.audit_events||0,'控制面记录',''],['Agent 回合',data.runtime?.turn_count||0,'Runtime 累计',''],['失败率',`${Math.round((data.runtime?.failure_rate||0)*1000)/10}%`,`失败 ${data.runtime?.failed_turns||0} 次`,data.runtime?.failure_rate?'warning':'']];$('#metric-grid').innerHTML=metrics.map(([label,value,foot,kind])=>`<article class="metric-card ${kind}"><div class="metric-label">${label}</div><div class="metric-value">${value}</div><div class="metric-foot">${foot}</div></article>`).join('');const runtime=data.runtime||{};const runtimeMetrics=[['Token',runtime.total_tokens||0,'prompt + completion',''],['平均延迟',`${Math.round(runtime.avg_latency_ms||0)}ms`,'单回合 wall clock',''],['工具调用',runtime.tool_calls||0,'累计工具执行',''],['估算成本',`$${Number(runtime.estimated_cost_usd||0).toFixed(4)}`,'按模型用量估算','']];$('#runtime-metric-grid').innerHTML=runtimeMetrics.map(([label,value,foot,kind])=>`<article class="metric-card ${kind}"><div class="metric-label">${label}</div><div class="metric-value">${value}</div><div class="metric-foot">${foot}</div></article>`).join('');$('#approval-badge').textContent=data.waiting_approval;$('#attention-list').innerHTML=data.waiting_approval?`<div class="attention-item"><div class="attention-icon">!</div><div><strong>${data.waiting_approval} 个工具调用等待审批</strong><span>高风险沙箱或完全访问操作需要人工确认</span></div></div>`:`<div class="empty"><b>队列已清空</b><span>当前没有等待处理的人工审批。</span></div>`}
async function loadApprovals(){const runtime=await api('/v1/agent/approvals');state.approvals=runtime.items;$('#approval-badge').textContent=runtime.count;const runtimeCards=runtime.items.map(item=>`<article class="approval-card runtime-approval"><div class="approval-head"><div><span class="status-chip waiting_approval">逐次工具审批</span><h2>${escapeHTML(toolDisplayName(item.call.name))}</h2><div class="approval-meta">${escapeHTML(item.session_id)} · ${formatTime(item.created_at)}</div></div>${risk(item.call.name==='sandbox_full_access'?'high':'medium')}</div><div class="approval-body"><div class="evidence-box"><span>操作说明</span><p>${escapeHTML(summarizeTool(item.call.name,item.call.arguments))}</p><details class="tool-trace"><summary>原始数据</summary><div class="tool-trace-block"><span>调用参数</span><pre class="json-box">${escapeHTML(prettyJSON(item.call.arguments))}</pre></div></details></div><div class="evidence-box"><span>调用身份</span><p>${escapeHTML(item.user_id)} · ${escapeHTML(item.role)}</p></div></div><div class="approval-actions"><button class="danger-button" data-runtime-decision="false" data-id="${item.approval_id}">拒绝</button><button class="primary" data-runtime-decision="true" data-id="${item.approval_id}">仅批准本次</button></div></article>`);$('#approval-list').innerHTML=runtime.count?runtimeCards.join(''):`<article class="panel empty"><b>没有待审批任务</b><span>高风险工具调用会进入这里。</span></article>`;$$('[data-runtime-decision]').forEach(button=>button.addEventListener('click',()=>decideRuntimeApproval(button.dataset.id,button.dataset.runtimeDecision==='true')))}
async function decideRuntimeApproval(id,approved){const comment=window.prompt(approved?'请说明本次批准依据（可选）':'请输入拒绝原因','')??'';try{const result=await api(`/v1/agent/approvals/${id}`,{method:'POST',body:JSON.stringify({approved,comment})});toast(approved?'本次工具调用已批准并恢复':'工具调用已拒绝');await Promise.all([loadApprovals(),loadDashboard()]);if(state.agentChat.sessionId===result.session_id){await refreshAgentEvents();renderAgentMessages()}}catch(error){toast(error.message)}}
async function loadCatalog(){state.catalog=state.catalog||await api('/v1/catalog');const {tools}=state.catalog;const toolIcon=tool=>tool.builtin?'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 12h8M12 8v8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><rect x="4" y="4" width="16" height="16" rx="4" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>':'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9.5 14.5 5 5M8.5 6.5l-3 3a2.1 2.1 0 0 0 0 3l7 7a2.1 2.1 0 0 0 3 0l3-3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M14 5l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';$('#tool-grid').innerHTML=tools.map(tool=>`<article class="tool-card"><div class="agent-icon tool-icon ${tool.builtin?'tool-icon-builtin':''}">${toolIcon(tool)}</div><div><h3>${escapeHTML(tool.id)}</h3><p>${tool.approval?'写操作 · 强制人工审批':'只读操作 · 策略自动批准'}</p><span class="tag">${escapeHTML(tool.risk)} risk</span><span class="tag">${escapeHTML(tool.mode)}</span>${tool.builtin?agentTagMarkup('builtin','内置'):''}</div></article>`).join('')}
function renderBuiltinToolChips(names,toolCatalog){const lookup=new Map((toolCatalog||[]).map(item=>[item.name,item]));$('#agent-builtin-tools').innerHTML=names.map(name=>{const tool=lookup.get(name)||{name};return `<span class="tool-chip locked" title="系统内置，不可移除">${escapeHTML(tool.name)}<span class="tag">builtin</span></span>`}).join('')||'<span class="form-hint">暂无</span>'}
function renderOptionalToolChips(names,toolCatalog){const lookup=new Map((toolCatalog||[]).map(item=>[item.name,item]));$('#agent-optional-tools').innerHTML=names.length?names.map(name=>{const tool=lookup.get(name)||{name,mode:'custom'};return `<span class="tool-chip" data-optional-tool="${escapeHTML(name)}">${escapeHTML(tool.name)}<span class="tag">${escapeHTML(tool.mode||'custom')}</span><button type="button" data-remove-tool="${escapeHTML(name)}" title="移除">×</button></span>`}).join(''):'<span class="form-hint">未追加额外工具，Runtime 可使用全部已注册工具。</span>';$$('[data-remove-tool]').forEach(button=>button.addEventListener('click',()=>removeOptionalTool(button.dataset.removeTool)))}
function renderToolMenuOptions(){const agent=state.editingAgent;if(!agent)return;const selected=new Set(state.editingOptionalTools||[]);const options=(agent.tool_catalog||[]).filter(tool=>!tool.builtin&&!selected.has(tool.name));const menu=$('#agent-tool-menu-list');if(!options.length){menu.innerHTML='<div class="form-hint" style="padding:8px">没有可添加的工具</div>';return}menu.innerHTML=options.map(tool=>`<button type="button" class="tool-picker-option" data-add-tool="${escapeHTML(tool.name)}"><strong>${escapeHTML(tool.name)}</strong><span>${escapeHTML(tool.mode||'custom')} · ${escapeHTML(tool.risk||'low')} risk</span></button>`).join('');$$('[data-add-tool]').forEach(button=>button.addEventListener('click',()=>addOptionalTool(button.dataset.addTool)))}
function closeToolMenu(){$('#agent-tool-menu').hidden=true}
function openToolMenu(){renderToolMenuOptions();$('#agent-tool-menu').hidden=false}
function addOptionalTool(name){if(!state.editingOptionalTools)state.editingOptionalTools=[];if(!state.editingOptionalTools.includes(name))state.editingOptionalTools.push(name);renderOptionalToolChips(state.editingOptionalTools,state.editingAgent?.tool_catalog);renderToolMenuOptions();closeToolMenu()}
function removeOptionalTool(name){state.editingOptionalTools=(state.editingOptionalTools||[]).filter(item=>item!==name);renderOptionalToolChips(state.editingOptionalTools,state.editingAgent?.tool_catalog);renderToolMenuOptions()}
function renderAgentToolPicker(agent){state.editingOptionalTools=[...(agent.optional_tools||[])];renderBuiltinToolChips(agent.builtin_tools||[],agent.tool_catalog);renderOptionalToolChips(state.editingOptionalTools,agent.tool_catalog);closeToolMenu()}
function agentStatusChip(status){return `<span class="status-chip ${status==='active'?'completed':'disabled'}">${status==='active'?'运行中':'已停用'}</span>`}
function agentIconMarkup(agent){
  const iconsById={
    'lingxing-profit-report':{cls:'api',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 17a4.5 4.5 0 1 1 9 0" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M11 17V9.5M11 9.5 8.5 7M11 9.5 13.5 7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 19h16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'},
    'profit-report-query':{cls:'database',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="6.5" rx="7" ry="3" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M5 6.5v4c0 1.66 3.13 3 7 3s7-1.34 7-3v-4M5 10.5v4c0 1.66 3.13 3 7 3s7-1.34 7-3v-4M5 14.5v4c0 1.66 3.13 3 7 3s7-1.34 7-3v-4" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>'},
    'kingdee-cloud':{cls:'erp',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5h14v14H5z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M8 9h8M8 13h5M8 17h3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'}
  };
  const custom=iconsById[agent.id];
  if(custom)return `<div class="agent-icon agent-icon-${custom.cls}" title="${escapeHTML(agent.name)}">${custom.svg}</div>`;
  const kind=agent.kind||'runtime';
  const icons={runtime:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 9h8M8 13h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M7 4h10a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M16.5 17.5 19 20l3.5-3.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>','hybrid':'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 18V6a2 2 0 0 1 2-2h8l4 4v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M14 4v4h4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M8 13h8M8 17h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'};
  return `<div class="agent-icon agent-icon-${escapeHTML(kind)}" title="${escapeHTML(agent.name)}">${icons[kind]||icons.runtime}</div>`;
}
function agentTagMarkup(type,label){const icons={runtime:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5h10M3 8h6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',hybrid:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 12V4.5A1.5 1.5 0 0 1 4 3h5l2 2V12a1.5 1.5 0 0 1-1.5 1.5H4A1.5 1.5 0 0 1 2.5 12Z" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>',builtin:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" fill="none" stroke="currentColor" stroke-width="1.3"/><rect x="4" y="7" width="8" height="6" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>',tools:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5.8 10.2 3.3 12.7a1.2 1.2 0 1 1-1.7-1.7l2.5-2.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><path d="M9.5 3.5l3 3-5.8 5.8-3-3z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>','source-api':'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v5M8 7.5 6 5.5M8 7.5l2-2" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/><path d="M4 12.5h8" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>','source-db':'<svg viewBox="0 0 16 16" aria-hidden="true"><ellipse cx="8" cy="4.5" rx="5" ry="2" fill="none" stroke="currentColor" stroke-width="1.1"/><path d="M3 4.5v3c0 1.1 2.24 2 5 2s5-.9 5-2v-3M3 7.5v3c0 1.1 2.24 2 5 2s5-.9 5-2v-3" fill="none" stroke="currentColor" stroke-width="1.1"/></svg>'};return `<span class="tag tag-icon tag-${escapeHTML(type)}">${icons[type]||''}<span>${escapeHTML(label)}</span></span>`}
function agentTags(agent){const tags=[agentTagMarkup(agent.kind,agent.kind==='runtime'?'对话 Runtime':agent.kind==='hybrid'?'混合 Agent':agent.kind)];if(agent.id==='lingxing-profit-report')tags.push(agentTagMarkup('source-api','开放平台'));if(agent.id==='kingdee-cloud')tags.push(agentTagMarkup('source-api','金蝶 WebAPI'));if(agent.id==='profit-report-query')tags.push(agentTagMarkup('source-db','PostgreSQL'));if(agent.builtin)tags.push(agentTagMarkup('builtin','内置'));if(agent.allowed_tools?.length)tags.push(agentTagMarkup('tools',`${agent.allowed_tools.length} 工具`));return tags.join('')}
function lingxingSetupCardMarkup(agent){
  if(agent.id!=='lingxing-profit-report'||agent.status==='active')return'';
  const integration=agent.integration||{};
  const configured=!!(integration.app_id&&integration.app_secret_configured);
  const statusText=configured?agent.enabled?'凭证已保存；若调用失败请确认出口 IP 已加白名单。':'凭证已保存；请勾选启用并保存。':'请完成下列三步后再启用。';
  return`<div class="integration-setup-inline"><strong class="integration-setup-title">开放平台接入准备</strong><ol class="integration-setup-steps"><li>在领星 ERP <strong>设置 → 业务配置 → 开放接口</strong> 获取 App ID / App Secret</li><li>将<strong>本服务器出口 IP</strong> 加入领星白名单</li><li>点击下方「编辑配置」填写凭证，勾选<strong>启用此 Agent</strong> 并保存</li></ol><span class="integration-setup-status">${escapeHTML(statusText)}</span></div>`;
}
function profitReportSetupCardMarkup(agent){
  if(agent.id!=='profit-report-query'||agent.status==='active')return'';
  const statusText=agent.enabled?'请确认 ANALYTICS_DSN 已配置且已导入 XLSX 数据。':'请完成下列三步后再启用。';
  return`<div class="integration-setup-inline integration-setup-db"><strong class="integration-setup-title">本地数据准备</strong><ol class="integration-setup-steps"><li>在 <strong>.env</strong> 配置 <strong>ANALYTICS_DSN</strong>（只读账号，如 amazon_finance_reader）</li><li>运行 <strong>scripts/import_lingxing_profit_xlsx.py</strong> 将领星导出 XLSX 写入 PostgreSQL</li><li>点击下方「编辑配置」，勾选<strong>启用此 Agent</strong> 并保存</li></ol><span class="integration-setup-status">${escapeHTML(statusText)}</span></div>`;
}
function kingdeeSetupCardMarkup(agent){
  if(agent.id!=='kingdee-cloud'||agent.status==='active')return'';
  const integration=agent.integration||{};
  const configured=!!(integration.server_url&&integration.acct_id&&integration.app_id&&integration.app_secret_configured&&integration.username);
  const statusText=configured?agent.enabled?'WebAPI 凭证已保存；若调用失败请检查第三方授权与服务地址。':'凭证已保存；请勾选启用并保存。':'请完成金蝶 WebAPI 授权与凭证配置后再启用。';
  return`<div class="integration-setup-inline integration-setup-erp"><strong class="integration-setup-title">金蝶云星空接入准备</strong><ol class="integration-setup-steps"><li>在金蝶 <strong>基础管理 → 公共设置 → Web API</strong> 完成第三方系统登录授权</li><li>获取账套 ID、应用 ID、应用密钥与集成用户名</li><li>填写以 <strong>/K3Cloud</strong> 结尾的服务地址，勾选<strong>启用此 Agent</strong> 并保存</li></ol><span class="integration-setup-status">${escapeHTML(statusText)}</span></div>`;
}
function agentSetupMarkup(agent){return lingxingSetupCardMarkup(agent)+profitReportSetupCardMarkup(agent)+kingdeeSetupCardMarkup(agent)}
function renderAgentCard(agent){
  const hasSetup=(agent.id==='lingxing-profit-report'||agent.id==='profit-report-query'||agent.id==='kingdee-cloud')&&agent.status!=='active';
  return`<article class="agent-card ${agent.status==='active'?'':'disabled'} ${hasSetup?'agent-card-integration':''}"><div class="agent-card-head">${agentIconMarkup(agent)}${agentStatusChip(agent.status)}</div><h3>${escapeHTML(agent.name)}</h3><p class="agent-role">${escapeHTML(agent.role)}</p><p class="form-hint agent-desc">${escapeHTML(agent.description||'')}</p>${agentSetupMarkup(agent)}<div class="agent-tags">${agentTags(agent)}</div><div class="agent-card-actions"><button class="secondary" data-edit-agent="${escapeHTML(agent.id)}">编辑配置</button></div></article>`;
}
function renderAgentCards(agents){
  const byId=Object.fromEntries(agents.map(item=>[item.id,item]));
  const core=['function-calling-runtime','amazon-finance-query'].map(id=>byId[id]).filter(Boolean);
  const profit=['lingxing-profit-report','profit-report-query'].map(id=>byId[id]).filter(Boolean);
  const erp=['kingdee-cloud'].map(id=>byId[id]).filter(Boolean);
  const root=$('#agent-sections');
  root.innerHTML=`<div class="agent-section"><div class="agent-grid agent-grid-core">${core.map(renderAgentCard).join('')}</div></div><div class="agent-section agent-section-profit"><h2 class="section-title">利润报表<span class="section-sub">两个独立 Agent：开放平台实时拉取 · PostgreSQL 本地仓查询</span></h2><div class="agent-grid agent-grid-profit">${profit.map(renderAgentCard).join('')}</div></div><div class="agent-section agent-section-erp"><h2 class="section-title">ERP<span class="section-sub">金蝶云星空私有云 WebAPI · 销售/应收单据查询</span></h2><div class="agent-grid agent-grid-erp">${erp.map(renderAgentCard).join('')}</div></div>`;
  $$('[data-edit-agent]',root).forEach(button=>button.addEventListener('click',()=>openAgentEditor(button.dataset.editAgent)));
}
async function loadAgentsPage(){const [agents,catalog]=await Promise.all([api('/v1/agents'),api('/v1/catalog')]);state.agents=agents.items;state.catalog=catalog;renderAgentCards(agents.items);await loadCatalog()}
async function openAgentEditor(agentId){try{const agent=await api(`/v1/agents/${agentId}`);state.editingAgent=agent;$('#agent-editor-title').textContent=agent.name;$('#agent-editor-subtitle').textContent=`${agent.id} · ${agent.kind}`;$('#agent-edit-name').value=agent.name;$('#agent-edit-role').value=agent.role;$('#agent-edit-description').value=agent.description||'';$('#agent-edit-enabled').checked=!!agent.enabled;$('#agent-edit-system-prompt').value=agent.system_prompt||'';const toolsWrap=$('#agent-edit-tools-wrap');const integrationWrap=$('#agent-edit-integration-wrap');const lingxingWrap=$('#agent-edit-lingxing-wrap');const kingdeeWrap=$('#agent-edit-kingdee-wrap');if(agent.id==='function-calling-runtime'){toolsWrap.hidden=false;renderAgentToolPicker(agent)}else{toolsWrap.hidden=true;state.editingOptionalTools=[]}if(agent.id==='lingxing-profit-report'){integrationWrap.hidden=false;lingxingWrap.hidden=false;kingdeeWrap.hidden=true;const integration=agent.integration||{};$('#agent-edit-lingxing-app-id').value=integration.app_id||'';$('#agent-edit-lingxing-app-secret').value='';$('#agent-edit-lingxing-app-secret').placeholder=integration.app_secret_configured?'已配置，留空保持不变':'填写 App Secret';$('#agent-edit-lingxing-base-url').value=integration.base_url||'https://openapi.lingxing.com'}else if(agent.id==='kingdee-cloud'){integrationWrap.hidden=false;lingxingWrap.hidden=true;kingdeeWrap.hidden=false;const integration=agent.integration||{};$('#agent-edit-kingdee-server-url').value=integration.server_url||'';$('#agent-edit-kingdee-acct-id').value=integration.acct_id||'';$('#agent-edit-kingdee-app-id').value=integration.app_id||'';$('#agent-edit-kingdee-app-secret').value='';$('#agent-edit-kingdee-app-secret').placeholder=integration.app_secret_configured?'已配置，留空保持不变':'填写应用密钥';$('#agent-edit-kingdee-username').value=integration.username||'';$('#agent-edit-kingdee-lcid').value=integration.lcid||2052}else{integrationWrap.hidden=true;lingxingWrap.hidden=true;kingdeeWrap.hidden=true}$('#agent-editor-drawer').classList.add('open')}catch(error){toast(error.message)}}
async function saveAgentEditor(event){event.preventDefault();const agent=state.editingAgent;if(!agent)return;const payload={name:$('#agent-edit-name').value.trim(),role:$('#agent-edit-role').value.trim(),description:$('#agent-edit-description').value.trim(),enabled:$('#agent-edit-enabled').checked,system_prompt:$('#agent-edit-system-prompt').value};if(agent.id==='function-calling-runtime')payload.allowed_tools=[...(state.editingOptionalTools||[])];if(agent.id==='lingxing-profit-report'){const secret=$('#agent-edit-lingxing-app-secret').value;payload.integration={app_id:$('#agent-edit-lingxing-app-id').value.trim(),base_url:$('#agent-edit-lingxing-base-url').value.trim()||'https://openapi.lingxing.com'};if(secret.trim())payload.integration.app_secret=secret.trim()}if(agent.id==='kingdee-cloud'){const secret=$('#agent-edit-kingdee-app-secret').value;payload.integration={server_url:$('#agent-edit-kingdee-server-url').value.trim(),acct_id:$('#agent-edit-kingdee-acct-id').value.trim(),app_id:$('#agent-edit-kingdee-app-id').value.trim(),username:$('#agent-edit-kingdee-username').value.trim(),lcid:Number($('#agent-edit-kingdee-lcid').value)||2052};if(secret.trim())payload.integration.app_secret=secret.trim()}const submit=$('#agent-edit-save');submit.disabled=true;try{await api(`/v1/agents/${agent.id}`,{method:'PATCH',body:JSON.stringify(payload)});toast('Agent 配置已保存');$('#agent-editor-drawer').classList.remove('open');closeToolMenu();state.catalog=null;await loadAgentsPage()}catch(error){toast(error.message)}finally{submit.disabled=false}}
async function loadConfiguration(){
  state.configuration=await api('/v1/configuration');
  const c=state.configuration;
  const knowledge=$('#knowledge-settings');
  if(knowledge){
    knowledge.innerHTML=[['检索后端',c.knowledge.backend,c.knowledge.configured?'连接配置完整':'等待配置'],['Qdrant Collection',c.knowledge.collection,'按 tenant 与 knowledge base 过滤'],['Embedding 模型',c.knowledge.embedding_model,`Top K · ${c.knowledge.top_k}`]].map(settingCard).join('');
  }
  const system=$('#system-settings');
  if(system){
    const defaultModel=c.models?.items?.find(item=>item.is_default)||c.models?.items?.[0];
    const modelSummary=defaultModel?`${defaultModel.name} (${defaultModel.provider}/${defaultModel.model_name})`:`${c.model.provider} / ${c.model.name}`;
    system.innerHTML=[['运行环境',c.environment,'当前部署环境'],['Session 事件',c.persistence.session_events,'Agent 对话事件库'],['默认模型',modelSummary,`已配置 ${c.models?.count||0} 个模型`],['控制面数据库',c.persistence.control_plane,'审计与配置索引'],['本回合 Token 预算',String(c.limits.run_token_budget),'超限会停止本回合，不是滑动窗口'],['安全策略','Secrets hidden','API 不返回任何密钥']].map(settingCard).join('');
  }
  await loadModelSettings();
  fillContextWindowForm(c.context_window);
}
function modelProviderLabel(provider){return ({zhipu:'智谱',openai:'OpenAI',mock:'Mock'})[provider]||provider}
function renderModelCard(model){
  return`<article class="agent-card ${model.enabled?'':'disabled'}"><div class="agent-card-head"><div class="agent-icon agent-icon-runtime" title="${escapeHTML(model.name)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l7 4v10l-7 4-7-4V7z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 12 19 8M12 12v9M12 12 5 8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></div>${model.is_default?'<span class="status-chip completed">默认</span>':agentStatusChip(model.enabled?'active':'disabled')}</div><h3>${escapeHTML(model.name)}</h3><p class="agent-role mono">${escapeHTML(model.id)}</p><p class="form-hint agent-desc">${escapeHTML(modelProviderLabel(model.provider))} · ${escapeHTML(model.model_name)}${model.supports_vision?' · Vision':''}</p><div class="agent-tags">${model.api_key_configured?agentTagMarkup('tools','Key 已配置'):agentTagMarkup('runtime','未配置 Key')}</div><div class="agent-card-actions"><button class="secondary" data-edit-model="${escapeHTML(model.id)}">编辑配置</button></div></article>`;
}
function renderModelCards(models){
  const root=$('#model-grid');
  if(!root)return;
  root.innerHTML=models.length?models.map(renderModelCard).join(''):`<article class="panel empty"><b>尚未配置模型</b><span>点击「添加模型」创建第一个可用模型。</span></article>`;
  $$('[data-edit-model]',root).forEach(button=>button.addEventListener('click',()=>openModelEditor(button.dataset.editModel)));
}
async function loadModelSettings(){
  const data=await api('/v1/configuration/models');
  state.models=data.items;
  renderModelCards(data.items);
}
function fillModelEditor(model,isNew){
  $('#model-editor-title').textContent=isNew?'添加模型':model.name;
  $('#model-editor-subtitle').textContent=isNew?'新建模型配置':'编辑模型配置';
  $('#model-edit-id-wrap').hidden=!isNew;
  $('#model-edit-id').value=model.id||'';
  $('#model-edit-id').disabled=!isNew;
  $('#model-edit-name').value=model.name||'';
  $('#model-edit-provider').value=model.provider||'zhipu';
  $('#model-edit-model-name').value=model.model_name||'';
  $('#model-edit-api-key').value='';
  $('#model-edit-api-key').placeholder=model.api_key_configured?'已配置，留空保持不变':'填写 API Key';
  $('#model-edit-base-url').value=model.base_url||'';
  $('#model-edit-vision-model').value=model.vision_model_name||'';
  $('#model-edit-temperature').value=model.temperature??'';
  $('#model-edit-enabled').checked=model.enabled!==false;
  $('#model-edit-default').checked=!!model.is_default;
  $('#model-edit-delete').hidden=isNew||!!model.builtin;
}
function openModelEditor(modelId){
  const isNew=!modelId;
  const model=isNew?{id:'',name:'',provider:'zhipu',model_name:'',enabled:true,is_default:false}:state.models.find(item=>item.id===modelId);
  if(!isNew&&!model){toast('模型不存在');return}
  state.editingModel={...(model||{}),isNew:!!isNew};
  fillModelEditor(state.editingModel,isNew);
  $('#model-editor-drawer').classList.add('open');
}
async function saveModelEditor(event){
  event.preventDefault();
  const editing=state.editingModel;
  if(!editing)return;
  const payload={
    name:$('#model-edit-name').value.trim(),
    provider:$('#model-edit-provider').value,
    model_name:$('#model-edit-model-name').value.trim(),
    base_url:$('#model-edit-base-url').value.trim(),
    vision_model_name:$('#model-edit-vision-model').value.trim(),
    enabled:$('#model-edit-enabled').checked,
    is_default:$('#model-edit-default').checked,
  };
  const temperature=$('#model-edit-temperature').value.trim();
  if(temperature!=='')payload.temperature=Number(temperature);
  const apiKey=$('#model-edit-api-key').value.trim();
  if(apiKey)payload.api_key=apiKey;
  const submit=$('#model-edit-save');
  submit.disabled=true;
  try{
    if(editing.isNew){
      payload.id=$('#model-edit-id').value.trim().toLowerCase();
      await api('/v1/configuration/models',{method:'POST',body:JSON.stringify(payload)});
      toast('模型已创建');
    }else{
      await api(`/v1/configuration/models/${encodeURIComponent(editing.id)}`,{method:'PATCH',body:JSON.stringify(payload)});
      toast('模型配置已保存');
    }
    $('#model-editor-drawer').classList.remove('open');
    state.configuration=null;
    await loadConfiguration();
    await refreshAgentModelSelect();
  }catch(error){toast(error.message)}
  finally{submit.disabled=false}
}
async function deleteModelEditor(){
  const editing=state.editingModel;
  if(!editing||editing.isNew||editing.builtin)return;
  if(!window.confirm(`确定删除模型「${editing.name}」？`))return;
  try{
    await api(`/v1/configuration/models/${encodeURIComponent(editing.id)}`,{method:'DELETE'});
    toast('模型已删除');
    $('#model-editor-drawer').classList.remove('open');
    if(state.agentChat.selectedModelId===editing.id){
      state.agentChat.selectedModelId='';
      localStorage.removeItem('arkflow.modelId');
    }
    state.configuration=null;
    await loadConfiguration();
    await refreshAgentModelSelect();
  }catch(error){toast(error.message)}
}
function selectedAgentModelId(models,defaultModelId){
  const enabled=new Set(models.map(item=>item.id));
  const stored=state.agentChat.selectedModelId;
  if(stored&&enabled.has(stored))return stored;
  if(defaultModelId&&enabled.has(defaultModelId))return defaultModelId;
  return models[0]?.id||'';
}
async function refreshAgentModelSelect(){
  const select=$('#agent-model-select');
  if(!select)return;
  const data=await api('/v1/models');
  const models=data.items||[];
  const selected=selectedAgentModelId(models,data.default_model_id);
  state.agentChat.selectedModelId=selected;
  if(selected)localStorage.setItem('arkflow.modelId',selected);
  select.innerHTML=models.map(item=>`<option value="${escapeHTML(item.id)}"${item.id===selected?' selected':''}>${escapeHTML(item.name)} (${escapeHTML(item.provider)}/${escapeHTML(item.model_name)})</option>`).join('');
  select.disabled=!models.length;
}
function onAgentModelChange(){
  const select=$('#agent-model-select');
  if(!select)return;
  state.agentChat.selectedModelId=select.value;
  if(select.value)localStorage.setItem('arkflow.modelId',select.value);
  else localStorage.removeItem('arkflow.modelId');
}
function fillContextWindowForm(config){
  const form=$('#context-window-form');
  if(!form||!config)return;
  form.enabled.checked=!!config.enabled;
  form.keep_recent_user_turns.value=config.keep_recent_user_turns;
  form.max_messages.value=config.max_messages;
  form.max_chars.value=config.max_chars;
  form.tool_max_rows.value=config.tool_max_rows;
  form.tool_max_chars.value=config.tool_max_chars;
}
async function saveContextWindow(event){
  event.preventDefault();
  const form=event.target;
  const payload={
    enabled:form.enabled.checked,
    keep_recent_user_turns:Number(form.keep_recent_user_turns.value),
    max_messages:Number(form.max_messages.value),
    max_chars:Number(form.max_chars.value),
    tool_max_rows:Number(form.tool_max_rows.value),
    tool_max_chars:Number(form.tool_max_chars.value),
  };
  try{
    const result=await api('/v1/configuration/context-window',{method:'PATCH',body:JSON.stringify(payload)});
    if(state.configuration)state.configuration.context_window=result.context_window;
    fillContextWindowForm(result.context_window);
    toast('滑动窗口已保存，后续对话立即生效');
  }catch(error){
    toast(error.message);
  }
}
function settingCard([title,value,description]){return`<article class="setting-card"><h3>${escapeHTML(title)}</h3><div class="setting-value">${escapeHTML(value)}</div><p>${escapeHTML(description)}</p></article>`}
async function loadAudit(){const data=await api('/v1/audit-events');state.audit=data.items;$('#audit-table').innerHTML=data.items.length?data.items.map(item=>`<tr><td>${formatTime(item.created_at)}</td><td>${escapeHTML(item.actor_id)}<br><span class="mono">${escapeHTML(item.actor_role)}</span></td><td><b>${escapeHTML(item.action)}</b></td><td>${escapeHTML(item.resource_type)}<br><span class="mono">${escapeHTML(item.resource_id.slice(0,12))}</span></td><td class="mono">${escapeHTML(JSON.stringify(item.detail))}</td></tr>`).join(''):`<tr><td colspan="5"><div class="empty">暂无审计事件</div></td></tr>`}
function agentStorageKey(){return`arkflow.agentSessions.${state.session.tenant}`}
function loadStoredAgentSessions(){try{return JSON.parse(localStorage.getItem(agentStorageKey())||'[]')}catch{return[]}}
function rememberAgentSession(id,title){const sessions=loadStoredAgentSessions().filter(item=>item.id!==id);sessions.unshift({id,title:title.slice(0,48),updatedAt:new Date().toISOString()});state.agentChat.sessions=sessions.slice(0,50);localStorage.setItem(agentStorageKey(),JSON.stringify(state.agentChat.sessions));localStorage.setItem(`${agentStorageKey()}.current`,id)}
function forgetAgentSession(id){state.agentChat.sessions=loadStoredAgentSessions().filter(item=>item.id!==id);localStorage.setItem(agentStorageKey(),JSON.stringify(state.agentChat.sessions));if(localStorage.getItem(`${agentStorageKey()}.current`)===id)localStorage.removeItem(`${agentStorageKey()}.current`)}
function saveAgentSessions(sessions){state.agentChat.sessions=sessions;localStorage.setItem(agentStorageKey(),JSON.stringify(sessions))}
function moveAgentSession(id,direction){const sessions=[...state.agentChat.sessions];const index=sessions.findIndex(item=>item.id===id);if(index<0)return;const target=index+direction;if(target<0||target>=sessions.length)return;[sessions[index],sessions[target]]=[sessions[target],sessions[index]];saveAgentSessions(sessions);renderAgentSessionList();toast(direction<0?'会话已上移':'会话已下移')}
function closeAgentSessionMenu(){$$('.agent-session-menu-panel').forEach(panel=>{panel.hidden=true});$$('.agent-session-menu').forEach(menu=>menu.classList.remove('open'))}
function toggleAgentSessionMenu(id,trigger){const panel=trigger.parentElement.querySelector('.agent-session-menu-panel');const opening=panel.hidden;closeAgentSessionMenu();if(opening){panel.hidden=false;trigger.closest('.agent-session-menu').classList.add('open')}}
function updateAgentSessionActions(){const button=$('#agent-delete-session');if(!button)return;button.disabled=!state.agentChat.sessionId||state.agentChat.sending}
function readFileDataUrl(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=()=>reject(reader.error);reader.readAsDataURL(file)})}
async function uploadAgentImage(file){const dataUrl=await readFileDataUrl(file);const [prefix,dataBase64]=dataUrl.split(',',2);const mediaType=(prefix.match(/^data:([^;]+);base64$/)||[])[1];if(!mediaType)throw new Error('无法识别图片格式');const reference=await api('/v1/agent/attachments',{method:'POST',body:JSON.stringify({name:file.name,media_type:mediaType,data_base64:dataBase64})});return{reference,dataUrl}}
async function handleAgentImageSelect(event){const files=[...(event.target.files||[])];if(!files.length)return;$('#agent-attach-image').disabled=true;try{for(const file of files){if(state.agentChat.pendingAttachments.length>=20)throw new Error('每条消息最多 20 张图片');const uploaded=await uploadAgentImage(file);state.agentChat.pendingAttachments.push(uploaded)}renderAgentAttachmentPreview()}catch(error){toast(error.message)}finally{$('#agent-attach-image').disabled=false;event.target.value=''}}
function renderAgentAttachmentPreview(){const root=$('#agent-image-preview');root.innerHTML=state.agentChat.pendingAttachments.map((item,index)=>`<span class="chat-image-item"><img src="${item.dataUrl}" alt="${escapeHTML(item.reference.name||'图片')}"><button type="button" data-remove-agent-image="${index}" aria-label="移除">×</button></span>`).join('');$$('[data-remove-agent-image]',root).forEach(button=>button.addEventListener('click',()=>{state.agentChat.pendingAttachments.splice(Number(button.dataset.removeAgentImage),1);renderAgentAttachmentPreview()}))}
function projectAgentTurns(events){
  const items=[];
  let turn=null;
  const ensureTurn=(meta={})=>{
    if(!turn){
      turn={kind:'turn',provider:meta.provider||'',model:meta.model||'',texts:[],steps:[],final:null};
      items.push(turn);
    }
    if(meta.provider)turn.provider=meta.provider;
    if(meta.model)turn.model=meta.model;
    return turn;
  };
  for(const event of events){
    const payload=event.payload||{};
    if(event.event_type==='user.message'){
      turn=null;
      items.push({kind:'user',content:payload.content,attachments:payload.attachments||[],at:event.created_at});
      continue;
    }
    if(event.event_type==='model.response'){
      const current=ensureTurn(payload);
      for(const call of payload.tool_calls||[]){
        current.steps.push({kind:'tool',name:call.name,arguments:call.arguments,status:'queued',ok:null,output:null,duration_ms:null});
      }
      const text=visibleAssistantText(payload.content);
      if(text)current.texts.push(text);
      continue;
    }
    if(event.event_type==='tool.requested'){
      const current=ensureTurn({});
      const existing=current.steps.find(step=>step.kind==='tool'&&step.name===payload.tool_name&&step.status==='queued');
      if(existing){
        existing.status='running';
        existing.arguments=payload.arguments||existing.arguments;
      }else{
        current.steps.push({kind:'tool',name:payload.tool_name,arguments:payload.arguments,status:'running',ok:null,output:null,duration_ms:null});
      }
      continue;
    }
    if(event.event_type==='tool.completed'){
      const current=ensureTurn({});
      const existing=[...current.steps].reverse().find(step=>step.kind==='tool'&&step.name===payload.tool_name&&step.status!=='done');
      const body=payload.ok?payload.output:{error:payload.error};
      if(existing){
        existing.status='done';
        existing.ok=payload.ok;
        existing.output=body;
        existing.duration_ms=payload.duration_ms;
      }else{
        current.steps.push({kind:'tool',name:payload.tool_name,arguments:payload.arguments,status:'done',ok:payload.ok,output:body,duration_ms:payload.duration_ms});
      }
      continue;
    }
    if(event.event_type==='approval.requested'){
      ensureTurn({}).steps.push({kind:'governance',eventType:'approval.requested',...payload,decided:false});
      continue;
    }
    if(event.event_type==='approval.decided'){
      const current=ensureTurn({});
      const existing=[...current.steps].reverse().find(step=>
        step.kind==='governance'
        && !step.decided
        && (step.approval_id===payload.approval_id || step.call_id===payload.call_id)
      );
      if(existing){
        existing.eventType='approval.decided';
        existing.decided=true;
        existing.approved=payload.approved;
        existing.comment=payload.comment;
      }else{
        current.steps.push({kind:'governance',eventType:'approval.decided',...payload,decided:true});
      }
      continue;
    }
    if(event.event_type.startsWith('subagent.')){
      ensureTurn({}).steps.push({kind:'subagent',eventType:event.event_type,...payload});
      continue;
    }
    if(event.event_type==='turn.completed'){
      const current=ensureTurn({});
      if(payload.status==='waiting_approval')continue;
      const answer=visibleAssistantText(payload.answer||'');
      if(answer==='高风险工具正在等待逐次人工审批。')continue;
      if(answer&&answer!==current.texts.at(-1))current.final={content:answer,status:payload.status};
    }
  }
  return items;
}
function liveAgentStatus(){
  if(!state.agentChat.sending)return'';
  if(state.agentChat.stream?.text)return'正在生成回答';
  const last=state.agentChat.events.at(-1);
  const type=last?.event_type;
  const payload=last?.payload||{};
  if(type==='tool.requested')return`正在执行 · ${summarizeTool(payload.tool_name,payload.arguments||{})}`;
  if(type==='tool.completed')return'正在整理结果';
  if(type==='model.response'&&(payload.tool_calls||[]).length)return'准备调用工具';
  if(type==='model.request')return'正在连接模型';
  return'正在思考';
}
function lockAgentMotionKeys(){
  projectAgentTurns(state.agentChat.events).forEach((item,index)=>{
    state.agentChat.motionKeys.add(`user-${index}`);
    state.agentChat.motionKeys.add(`turn-${index}`);
    (item.steps||[]).forEach((_,stepIndex)=>state.agentChat.motionKeys.add(`step-${index}-${stepIndex}`));
  });
}
function enterClass(key){
  if(!state.agentChat.sending)return'';
  if(state.agentChat.motionKeys.has(key))return'';
  state.agentChat.motionKeys.add(key);
  return' chat-enter';
}
function toolDisplayName(name){
  return ({
    sandbox_workspace_write:'写入工作区',
    sandbox_read_only:'读取本地文件',
    sandbox_full_access:'无隔离命令',
    amazon_finance_query:'Amazon 结算查询',
    lingxing_profit_query:'领星开放平台 API',
    profit_report_query:'利润报表 PostgreSQL',
    kingdee_cloud_query:'金蝶云星空 WebAPI',
  }[name])||name;
}
function summarizeTool(name,args={},output){
  const command=Array.isArray(args?.command)?args.command:[];
  if(String(name||'').startsWith('sandbox_')){
    const file=command.find((item,index)=>index===command.length-1&&/\.\w+$/.test(String(item)));
    const bin=command[0];
    if(file)return `${toolDisplayName(name)} · ${file}`;
    if(bin)return `${toolDisplayName(name)} · ${bin}`;
    return toolDisplayName(name);
  }
  if(name==='amazon_finance_query'){
    if(output?.summary)return output.summary;
    const metric={overview:'总览',daily:'每日趋势',transaction_type:'交易类型',fee:'费用',sku:'SKU',settlement:'结算批次'}[args.metric]||'查询';
    const range=[args.start_date,args.end_date].filter(Boolean).join(' ~ ');
    return `Amazon ${metric}${range?` · ${range}`:''}`;
  }
  if(name==='lingxing_profit_query'){
    if(output?.summary)return output.summary;
    const range=[args.start_date,args.end_date].filter(Boolean).join(' ~ ');
    const currency=args.currency_code?` · ${args.currency_code}`:'';
    return `领星利润报表${range?` · ${range}`:''}${currency}`;
  }
  if(name==='profit_report_query'){
    if(output?.summary)return output.summary;
    const metric={overview:'总览',daily:'每日',store:'店铺',msku:'MSKU',order:'订单',event_source:'费用类型'}[args.metric]||'查询';
    const range=[args.start_date,args.end_date].filter(Boolean).join(' ~ ');
    const currency=args.currency_code?` · ${args.currency_code}`:'';
    return `利润报表 ${metric}${range?` · ${range}`:''}${currency}`;
  }
  if(name==='kingdee_cloud_query'){
    if(output?.summary)return output.summary;
    const doc={sale_order:'销售订单',sale_outstock:'销售出库单',ar_receivable:'应收单',ar_expense_receivable:'费用应收单'}[args.document_type]||'金蝶单据';
    const range=[args.start_date,args.end_date].filter(Boolean).join(' ~ ');
    return `${doc}${range?` · ${range}`:''}${args.bill_no?` · ${args.bill_no}`:''}`;
  }
  if(output?.summary)return output.summary;
  return toolDisplayName(name);
}
function prettyJSON(value,limit=20000){
  let text;
  try{text=JSON.stringify(value,null,2)}catch{text=String(value??'')}
  if(text.length<=limit)return text;
  return `${text.slice(0,limit)}\n…（显示已截断，完整数据仍在会话事件中）`;
}
function compactTraceOutput(output){
  if(!output||typeof output!=='object'||Array.isArray(output))return output;
  const rows=output.rows;
  if(!Array.isArray(rows)||rows.length<=20)return output;
  return {...output,row_count:output.row_count??rows.length,rows:rows.slice(0,20),rows_truncated:true};
}
function toolTraceHtml(step){
  const args=step.arguments;
  const output=step.status==='done'?step.output:null;
  const blocks=[];
  if(args&&!(typeof args==='object'&&!Array.isArray(args)&&!Object.keys(args).length)){
    blocks.push(`<div class="tool-trace-block"><span>调用参数</span><pre class="json-box">${escapeHTML(prettyJSON(args))}</pre></div>`);
  }
  if(output!=null){
    blocks.push(`<div class="tool-trace-block"><span>工具结果</span><pre class="json-box">${escapeHTML(prettyJSON(compactTraceOutput(output)))}</pre></div>`);
  }
  if(!blocks.length)return'';
  return `<details class="tool-trace"><summary>原始数据</summary>${blocks.join('')}</details>`;
}
function renderToolStep(step,key){
  const enter=enterClass(key);
  if(step.kind==='governance'){
    const summary=summarizeTool(step.tool_name,step.arguments||{});
    if(step.eventType==='approval.requested'&&!step.decided){
      return`<div class="chat-step governance${enter}"><div class="tool-event governance-event"><div><b>等待审批</b><span>${escapeHTML(summary)}</span></div><div class="chat-approval-actions"><button type="button" class="danger-button" data-chat-reject="${escapeHTML(step.approval_id)}">拒绝</button><button type="button" class="primary small" data-chat-approve="${escapeHTML(step.approval_id)}">批准本次</button></div></div>${toolTraceHtml(step)}</div>`;
    }
    const verdict=step.approved===false?'已拒绝':'已批准';
    return`<div class="chat-step${enter}"><div class="tool-event governance-event"><b>${verdict}</b><span>${escapeHTML(summary||step.tool_name||step.call_id||'')}</span></div>${toolTraceHtml(step)}</div>`;
  }
  if(step.kind==='subagent'){
    return`<div class="chat-step${enter}"><div class="tool-event subagent-event"><b>后台任务</b><span>${escapeHTML(step.status||step.objective||step.task_id||'')}</span></div></div>`;
  }
  const status=step.status==='done'?(step.ok?'done':'error'):step.status;
  const summary=summarizeTool(step.name,step.arguments||{},step.output);
  const label=step.status==='done'?`${step.ok?'完成':'失败'} · ${escapeHTML(summary)}${step.duration_ms?` · ${escapeHTML(step.duration_ms)}ms`:''}`:step.status==='running'?`正在执行 · ${escapeHTML(summary)}`:`准备执行 · ${escapeHTML(summary)}`;
  const files=[...(step.output?.written_files||[])];
  if(!files.length){
    const parsed=echoFileFromCommand(step.arguments?.command||step.output?.command);
    if(parsed)files.push(parsed.name);
  }
  const unique=[...new Set(files)].sort((a,b)=>Number(String(b).toLowerCase().endsWith('.csv'))-Number(String(a).toLowerCase().endsWith('.csv')));
  const downloads=step.status==='done'&&step.ok&&unique.length?`<div class="tool-file-links">${unique.map(file=>`<a href="#" class="workspace-file-link" data-workspace-path="${escapeHTML(String(file).split(/[\\/]/).pop())}">下载 ${escapeHTML(String(file).split(/[\\/]/).pop())}</a>`).join('')}</div>`:'';
  return`<div class="chat-step tool-event ${status}${enter}"><span class="tool-step-label">${label}</span>${downloads}${toolTraceHtml(step)}</div>`;
}
function renderAgentMessages(){
  const root=$('#agent-message-list');
  const turns=projectAgentTurns(state.agentChat.events);
  const stream=state.agentChat.stream;
  if(state.agentChat.sending){
    let last=turns.at(-1);
    if(!last||last.kind==='user'){
      last={kind:'turn',provider:stream?.provider||'',model:stream?.model||'',texts:[],steps:[],final:null,live:true};
      turns.push(last);
    }else{
      last.live=true;
      if(stream?.provider)last.provider=stream.provider;
      if(stream?.model)last.model=stream.model;
    }
    last.streamText=visibleAssistantText(stream?.text||'');
    last.status=liveAgentStatus();
  }
  const html=turns.length?turns.map((item,index)=>{
    if(item.kind==='user'){
      const enter=enterClass(`user-${index}`);
      return`<div class="chat-row user${enter}"><div class="chat-bubble user"><div class="chat-meta">你 · ${formatTime(item.at)}</div><p>${escapeHTML(item.content)}</p>${item.attachments.length?`<div class="chat-attachment-badges">${item.attachments.map(file=>`<span class="tag">${escapeHTML(file.name||file.media_type)} · ${file.width}×${file.height}</span>`).join('')}</div>`:''}</div><div class="chat-avatar user" aria-hidden="true">你</div></div>`;
    }
    const enter=enterClass(`turn-${index}`);
    const steps=item.steps.map((step,stepIndex)=>renderToolStep(step,`step-${index}-${stepIndex}`)).join('');
    const texts=item.texts.map(text=>`<div class="chat-answer markdown-body">${renderMarkdown(text)}</div>`).join('');
    const final=item.final&&item.final.content!==item.texts.at(-1)?`<div class="chat-final-text markdown-body">${renderMarkdown(item.final.content)}</div>`:'';
    const streamText=item.streamText?`<div id="agent-stream-text" class="chat-answer markdown-body">${renderMarkdown(item.streamText)}</div>`:'';
    const thinking=!item.streamText&&item.live?`<div class="chat-thinking" id="agent-thinking"><span></span><span></span><span></span><b>${escapeHTML(item.status||'正在思考')}</b></div>`:'';
    return`<div class="chat-row assistant chat-turn${enter}${item.live?' live':''}"><div class="chat-avatar assistant${item.live?' pulsing':''}" aria-hidden="true">A</div><div class="chat-bubble assistant chat-turn-card${item.live?' live-card':''}${item.live&&item.streamText?' streaming':''}"><div class="chat-meta">${escapeHTML(modelLabel(item.provider,item.model)||'Agent')}${item.live?` · ${escapeHTML(item.status||'生成中')}`:''}</div>${steps?`<div class="chat-steps">${steps}</div>`:''}${texts}${final}${streamText}${thinking}</div></div>`;
  }).join(''):'';
  root.innerHTML=html?`${html}<div id="agent-scroll-anchor" aria-hidden="true"></div>`:`<div class="chat-empty"><div class="chat-empty-mark">A</div><b>开始一段新对话</b><span>可以直接提问，也可以让 Agent 调用工具、Skills 或分析 Amazon 费用。</span></div>`;
  const status=$('#agent-live-status');
  if(status){
    status.textContent=state.agentChat.sending?liveAgentStatus():'流式输出 · 工具轨迹可见';
    status.classList.toggle('is-live',state.agentChat.sending);
  }
  $$('[data-chat-approve]',root).forEach(button=>button.addEventListener('click',()=>decideRuntimeApproval(button.dataset.chatApprove,true)));
  $$('[data-chat-reject]',root).forEach(button=>button.addEventListener('click',()=>decideRuntimeApproval(button.dataset.chatReject,false)));
  $$('a.workspace-file-link',root).forEach(link=>link.addEventListener('click',event=>{
    event.preventDefault();
    downloadWorkspaceFile(link.dataset.workspacePath||'',link);
  }));
  bindAgentTranscriptScroll();
  watchAgentTranscriptSize();
  scrollAgentTranscript();
}
function renderAgentSessionList(){const root=$('#agent-session-list');const sessions=state.agentChat.sessions;root.innerHTML=sessions.length?sessions.map((item,index)=>{const isFirst=index===0;const isLast=index===sessions.length-1;return`<div class="agent-session-row ${item.id===state.agentChat.sessionId?'active':''}"><button type="button" class="agent-session-button" data-agent-session="${escapeHTML(item.id)}"><b>${escapeHTML(item.title||'未命名会话')}</b><span>${formatTime(item.updatedAt)}</span></button><div class="agent-session-menu"><button type="button" class="agent-session-menu-trigger" data-session-menu="${escapeHTML(item.id)}" aria-label="更多操作" title="更多操作">⋯</button><div class="agent-session-menu-panel" hidden><button type="button" class="agent-session-menu-item" data-session-move-up="${escapeHTML(item.id)}" ${isFirst?'disabled':''}>上移</button><button type="button" class="agent-session-menu-item" data-session-move-down="${escapeHTML(item.id)}" ${isLast?'disabled':''}>下移</button><button type="button" class="agent-session-menu-item danger" data-delete-session="${escapeHTML(item.id)}">删除</button></div></div></div>`}).join(''):`<div class="empty compact">还没有会话</div>`;$$('[data-agent-session]',root).forEach(button=>button.addEventListener('click',()=>{closeAgentSessionMenu();selectAgentSession(button.dataset.agentSession)}));$$('[data-session-menu]',root).forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();toggleAgentSessionMenu(button.dataset.sessionMenu,button)}));$$('[data-session-move-up]',root).forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();closeAgentSessionMenu();moveAgentSession(button.dataset.sessionMoveUp,-1)}));$$('[data-session-move-down]',root).forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();closeAgentSessionMenu();moveAgentSession(button.dataset.sessionMoveDown,1)}));$$('[data-delete-session]',root).forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();closeAgentSessionMenu();deleteAgentSession(button.dataset.deleteSession)}));updateAgentSessionActions()}
function renderAgentSubagents(){const root=$('#agent-subagent-list');root.innerHTML=state.agentChat.subagents.length?state.agentChat.subagents.slice(0,8).map(item=>`<div class="subagent-task"><b>${escapeHTML(item.objective)}</b><span class="status-chip ${item.status==='completed'?'completed':['failed','timed_out','cancelled'].includes(item.status)?'rejected':'waiting_approval'}">${escapeHTML(item.status)}</span><small>${escapeHTML(item.child_session_id.slice(0,8))} · depth ${item.depth}</small>${['queued','running','cancel_requested'].includes(item.status)?`<button class="text-button" data-cancel-subagent="${escapeHTML(item.task_id)}">取消</button>`:''}</div>`).join(''):`<div class="empty compact">暂无后台任务</div>`;$$('[data-cancel-subagent]',root).forEach(button=>button.addEventListener('click',()=>cancelSubagent(button.dataset.cancelSubagent)))}
async function loadAgentSubagents(){const suffix=state.agentChat.sessionId?`?parent_session_id=${encodeURIComponent(state.agentChat.sessionId)}`:'';const data=await api(`/v1/agent/subagents${suffix}`);state.agentChat.subagents=data.items;renderAgentSubagents()}
async function cancelSubagent(taskId){try{await api(`/v1/agent/subagents/${encodeURIComponent(taskId)}/cancel`,{method:'POST'});toast('已请求取消 Subagent');await loadAgentSubagents()}catch(error){toast(error.message)}}
async function refreshAgentEvents(){if(!state.agentChat.sessionId){state.agentChat.events=[];return}const data=await api(`/v1/agent/sessions/${encodeURIComponent(state.agentChat.sessionId)}/events`);state.agentChat.events=data.items}
async function selectAgentSession(id){state.agentChat.sessionId=id;localStorage.setItem(`${agentStorageKey()}.current`,id);state.agentChat.motionKeys=new Set();state.agentChat.stickToBottom=true;try{await Promise.all([refreshAgentEvents(),loadAgentSubagents()]);setAgentSessionLabel(id);renderAgentSessionList();renderAgentMessages();await maybeResumeAgentTurn()}catch(error){toast(error.message)}}
function startNewAgentSession(){state.agentChat.sessionId=null;localStorage.removeItem(`${agentStorageKey()}.current`);state.agentChat.events=[];state.agentChat.subagents=[];state.agentChat.pendingAttachments=[];state.agentChat.stream=null;state.agentChat.motionKeys=new Set();state.agentChat.stickToBottom=true;setAgentSessionLabel(null);renderAgentSessionList();renderAgentMessages();renderAgentSubagents();renderAgentAttachmentPreview()}
async function deleteAgentSession(id){if(!id){toast('当前没有可删除的会话');return}if(state.agentChat.sending&&id===state.agentChat.sessionId){toast('请等待当前回复结束');return}if(!window.confirm('确定删除这段对话？删除后无法恢复。'))return;try{await api(`/v1/agent/sessions/${encodeURIComponent(id)}`,{method:'DELETE'})}catch(error){if(!/not found/i.test(error.message||'')){toast(error.message);return}}forgetAgentSession(id);toast('对话已删除');if(state.agentChat.sessionId===id)startNewAgentSession();else renderAgentSessionList()}
function lastUserQuestion(events){
  for(let index=events.length-1;index>=0;index-=1){
    if(events[index].event_type==='user.message')return String(events[index].payload?.content||'续跑');
  }
  return'续跑';
}
function isOpenAgentTurn(events){
  let open=false;
  for(const event of events){
    if(event.event_type==='user.message')open=true;
    if(event.event_type==='turn.completed')open=false;
  }
  return open;
}
async function loadAgentChat(){
  state.agentChat.sessions=loadStoredAgentSessions();
  if(!state.agentChat.sessionId)state.agentChat.sessionId=localStorage.getItem(`${agentStorageKey()}.current`);
  await refreshAgentModelSelect();
  if(state.agentChat.sessionId){
    try{await refreshAgentEvents()}
    catch{startNewAgentSession()}
  }
  renderAgentSessionList();
  renderAgentMessages();
  setAgentSessionLabel(state.agentChat.sessionId);
  await loadAgentSubagents();
  await maybeResumeAgentTurn();
}
let agentCooldownTimer=null;
function updateAgentSendButton(){const button=$('#agent-send-button');const remaining=Math.max(0,Math.ceil((state.agentChat.cooldownUntil-Date.now())/1000));button.disabled=state.agentChat.sending||remaining>0;button.textContent=state.agentChat.sending?'处理中…':(remaining>0?`请等待 ${remaining}s`:'发送');updateAgentSessionActions();if(!remaining&&agentCooldownTimer){clearInterval(agentCooldownTimer);agentCooldownTimer=null}}
function startAgentCooldown(seconds){state.agentChat.cooldownUntil=Date.now()+Math.max(1,Number(seconds)||5)*1000;updateAgentSendButton();if(agentCooldownTimer)clearInterval(agentCooldownTimer);agentCooldownTimer=setInterval(updateAgentSendButton,1000)}
async function consumeAgentSse(response,question){
  if(!response.ok){
    let body={};
    try{body=await response.json()}catch{}
    const detail=body.detail;
    const message=typeof detail==='object'&&detail?detail.message:(detail||`请求失败 (${response.status})`);
    const error=new Error(message);
    if(typeof detail==='object'&&detail){error.code=detail.code;error.retryAfterSeconds=detail.retry_after_seconds}
    throw error;
  }
  const reader=response.body.getReader();
  const decoder=new TextDecoder();
  let buffer='';
  while(true){
    const {value,done}=await reader.read();
    if(done)break;
    buffer+=decoder.decode(value,{stream:true});
    const parsed=parseSseBlocks(buffer);
    buffer=parsed.rest;
    for(const item of parsed.events)handleAgentStreamEvent(item.name,item.data,question);
  }
  if(buffer.trim()){
    const parsed=parseSseBlocks(buffer+'\n\n');
    for(const item of parsed.events)handleAgentStreamEvent(item.name,item.data,question);
  }
}
async function finishAgentStream(error){
  if(error){
    if(error.retryAfterSeconds)startAgentCooldown(error.retryAfterSeconds);
    toast(error.code?`${error.message}（${error.code}）`:error.message);
  }
  state.agentChat.stream=null;
  state.agentChat.sending=false;
  updateAgentSendButton();
  if(state.agentChat.sessionId){
    try{
      await Promise.all([refreshAgentEvents(),loadAgentSubagents()]);
      renderAgentSessionList();
      renderAgentMessages();
    }catch{renderAgentMessages()}
  }
}
async function maybeResumeAgentTurn(){
  if(!state.agentChat.sessionId||state.agentChat.sending)return;
  if(!isOpenAgentTurn(state.agentChat.events))return;
  const question=lastUserQuestion(state.agentChat.events);
  state.agentChat.sending=true;
  updateAgentSendButton();
  lockAgentMotionKeys();
  state.agentChat.stickToBottom=true;
  state.agentChat.stream={provider:'',model:'',text:''};
  renderAgentMessages();
  try{
    const response=await fetch('/v1/agent/query/resume',{method:'POST',headers:headers(),body:JSON.stringify({session_id:state.agentChat.sessionId,seller_id:$('#agent-seller-id').value.trim()||undefined})});
    await consumeAgentSse(response,question);
  }catch(error){
    await finishAgentStream(error);
    return;
  }
  await finishAgentStream();
}
async function sendAgentMessage(event){event.preventDefault();if(state.agentChat.sending)return;if(Date.now()<state.agentChat.cooldownUntil){updateAgentSendButton();return}const question=$('#agent-question').value.trim();if(question.length<2){toast('请输入至少 2 个字符');return}state.agentChat.sending=true;updateAgentSendButton();const modelId=$('#agent-model-select')?.value||state.agentChat.selectedModelId||undefined;const payload={question,session_id:state.agentChat.sessionId||undefined,seller_id:$('#agent-seller-id').value.trim()||undefined,model_id:modelId,attachment_ids:state.agentChat.pendingAttachments.map(item=>item.reference.attachment_id)};lockAgentMotionKeys();state.agentChat.stickToBottom=true;state.agentChat.events=[...state.agentChat.events,{event_type:'user.message',created_at:new Date().toISOString(),payload:{content:question,attachments:state.agentChat.pendingAttachments.map(item=>item.reference)}}];state.agentChat.stream={provider:'',model:'',text:''};$('#agent-question').value='';state.agentChat.pendingAttachments=[];renderAgentAttachmentPreview();renderAgentMessages();try{const response=await fetch('/v1/agent/query/stream',{method:'POST',headers:headers(),body:JSON.stringify(payload)});await consumeAgentSse(response,question);await finishAgentStream()}catch(error){await finishAgentStream(error)}}
function parseSseBlocks(buffer){const parts=buffer.split('\n\n');const rest=parts.pop()||'';const events=[];for(const block of parts){let name='message';const dataLines=[];for(const line of block.split('\n')){if(line.startsWith('event:'))name=line.slice(6).trim();else if(line.startsWith('data:'))dataLines.push(line.slice(5).trimStart())}if(!dataLines.length)continue;try{events.push({name,data:JSON.parse(dataLines.join('\n'))})}catch{}}return{events,rest}}
function handleAgentStreamEvent(name,data,question){
  if(name==='session'&&data.session_id){
    state.agentChat.sessionId=data.session_id;
    rememberAgentSession(data.session_id,question);
    setAgentSessionLabel(data.session_id);
    renderAgentSessionList();
    return;
  }
  if(name==='token'){
    if(!state.agentChat.stream)state.agentChat.stream={provider:'',model:'',text:''};
    state.agentChat.stream.provider=data.provider||state.agentChat.stream.provider;
    state.agentChat.stream.model=data.model||state.agentChat.stream.model;
    state.agentChat.stream.text+=data.text||'';
    const el=$('#agent-stream-text');
    if(el){
      el.innerHTML=renderMarkdown(visibleAssistantText(state.agentChat.stream.text)||'正在思考…');
      const meta=el.parentElement?.querySelector('.chat-meta');
      if(meta)meta.textContent=`${modelLabel(state.agentChat.stream.provider,state.agentChat.stream.model)} · ${liveAgentStatus()}`;
      const thinking=$('#agent-thinking');
      if(thinking)thinking.remove();
      el.parentElement?.classList.add('streaming');
      const status=$('#agent-live-status');
      if(status){status.textContent=liveAgentStatus();status.classList.add('is-live')}
      watchAgentTranscriptSize();
      scrollAgentTranscript();
    }else renderAgentMessages();
    return;
  }
  if(name==='error'){
    const error=new Error(data.message||'流式请求失败');
    error.code=data.code;
    error.retryAfterSeconds=data.retry_after_seconds;
    throw error;
  }
  if(name==='done'){
    toast(data.status==='waiting_approval'?'工具调用正在等待逐次审批':`完成 · ${data.provider}/${data.model}`);
    return;
  }
  if(data.type==='user.message')return;
  if(data.payload&&data.type&&data.type!=='token'&&data.type!=='session'){
    if(data.type==='model.response'){
      const text=visibleAssistantText(data.payload?.content||'');
      if(text)state.agentChat.stream=null;
    }
    const last=state.agentChat.events.at(-1);
    if(!(last&&last.event_type===data.type&&JSON.stringify(last.payload||{})===JSON.stringify(data.payload||{}))){
      state.agentChat.events=[...state.agentChat.events,{event_type:data.type,payload:data.payload,created_at:data.created_at}];
      renderAgentMessages();
    }
  }
}
const loaders={dashboard:loadDashboard,approvals:loadApprovals,'agent-chat':loadAgentChat,agents:loadAgentsPage,knowledge:loadConfiguration,audit:loadAudit,settings:loadConfiguration};const pageNames={dashboard:'运行概览',approvals:'审批中心','agent-chat':'Agent 对话',agents:'Agents & 工具',knowledge:'知识库',audit:'审计日志',settings:'系统设置'};
async function navigate(page){state.page=page;$$('.page').forEach(el=>el.classList.toggle('active',el.id===`page-${page}`));$$('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.page===page));$('#page-breadcrumb').textContent=pageNames[page];$('.sidebar').classList.remove('open');try{await loaders[page]()}catch(error){toast(error.message)}}
function handleAgentQuestionKeydown(event){
  if(event.key!=='Enter'||event.shiftKey||event.altKey||event.ctrlKey||event.metaKey)return;
  if(event.isComposing||event.keyCode===229)return;
  event.preventDefault();
  const form=$('#agent-chat-form');
  if(typeof form.requestSubmit==='function')form.requestSubmit();
  else form.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));
}
$$('.nav-item').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.page)));$$('[data-go]').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.go)));$('#context-window-form').addEventListener('submit',saveContextWindow);$('#agent-editor-form').addEventListener('submit',saveAgentEditor);$('#model-editor-form').addEventListener('submit',saveModelEditor);$('#model-add-button')?.addEventListener('click',()=>openModelEditor());$('#model-edit-delete')?.addEventListener('click',deleteModelEditor);$('#agent-model-select')?.addEventListener('change',onAgentModelChange);$$('[data-close-model-editor]').forEach(button=>button.addEventListener('click',()=>$('#model-editor-drawer').classList.remove('open')));$('#model-editor-drawer')?.addEventListener('click',event=>{if(event.target.id==='model-editor-drawer')event.currentTarget.classList.remove('open')});$$('[data-close-agent-editor]').forEach(button=>button.addEventListener('click',()=>{closeToolMenu();$('#agent-editor-drawer').classList.remove('open')}));$('#agent-editor-drawer').addEventListener('click',event=>{if(event.target.id==='agent-editor-drawer'){closeToolMenu();event.currentTarget.classList.remove('open')}});$('#agent-add-tool-button').addEventListener('click',event=>{event.stopPropagation();const menu=$('#agent-tool-menu');menu.hidden=!menu.hidden;if(!menu.hidden)renderToolMenuOptions()});document.addEventListener('click',event=>{if(!event.target.closest('.tool-picker-add'))closeToolMenu();if(!event.target.closest('.agent-session-menu'))closeAgentSessionMenu()});$('#context-window-form').addEventListener('submit',saveContextWindow);$('#agent-chat-form').addEventListener('submit',sendAgentMessage);$('#agent-question').addEventListener('keydown',handleAgentQuestionKeydown);$('#agent-new-session').addEventListener('click',startNewAgentSession);$('#agent-delete-session').addEventListener('click',()=>deleteAgentSession(state.agentChat.sessionId));$('#agent-attach-image').addEventListener('click',()=>$('#agent-image-input').click());$('#agent-image-input').addEventListener('change',handleAgentImageSelect);$('#refresh-button').addEventListener('click',()=>navigate(state.page));$('#mobile-menu').addEventListener('click',()=>$('.sidebar').classList.toggle('open'));$('#api-key-input').value=state.session.apiKey;$('#tenant-input').value=state.session.tenant;$('#role-input').value=state.session.role;$('#save-session').addEventListener('click',()=>{state.session={apiKey:$('#api-key-input').value,tenant:$('#tenant-input').value||'tenant-a',role:$('#role-input').value};localStorage.setItem('arkflow.apiKey',state.session.apiKey);localStorage.setItem('arkflow.tenant',state.session.tenant);localStorage.setItem('arkflow.role',state.session.role);state.agentChat.sessionId=null;toast('开发会话已保存');navigate(state.page)});
loadHealth();navigate('dashboard');
