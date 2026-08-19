const state={page:'dashboard',summary:null,runs:[],approvals:[],agents:[],catalog:null,connections:[],editingConnection:null,configuration:null,models:null,editingModel:null,knowledgeSpaces:[],editingKnowledgeSpace:null,knowledgeContent:{spaceId:null,items:[],categories:[],category:'',cursor:null,total:0},audit:[],access:null,memories:[],resultViewer:{resultRef:null,offset:0,limit:50,data:null},session:{apiKey:localStorage.getItem('arkflow.apiKey')||'',tenant:localStorage.getItem('arkflow.tenant')||'tenant-a',userId:localStorage.getItem('arkflow.userId')||'local-admin',role:localStorage.getItem('arkflow.role')||'admin',accessToken:localStorage.getItem('arkflow.accessToken')||'',refreshToken:localStorage.getItem('arkflow.refreshToken')||'',account:null},agentChat:{sessionId:null,sessions:[],events:[],subagents:[],models:[],pendingAttachments:[],selectedModelId:localStorage.getItem('arkflow.modelId')||'',sending:false,interrupting:false,cooldownUntil:0,stream:null,motionKeys:new Set(),stickToBottom:true}};
const SPECIALIST_ANALYST_IDS=['amazon-finance-analyst','profit-analyst','erp-analyst'];
const $=(selector,root=document)=>root.querySelector(selector);const $$=(selector,root=document)=>[...root.querySelectorAll(selector)];
const escapeHTML=value=>String(value??'').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
function visibleAssistantText(content){if(!content)return'';const text=String(content).trim();if(/^\{\s*"tasks"\s*:/.test(text))return'';return text.replace(/\{\s*"(?:index|finish_reason|tool_calls)"[\s\S]*\}\s*/g,'').trim()}
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
      while(index<lines.length){
        if(!/^[-*]\s+/.test(lines[index]))break;
        items.push(`<li>${inlineMarkdown(lines[index].replace(/^[-*]\s+/,''))}</li>`);
        index+=1;
        let next=index;
        while(next<lines.length&&!lines[next].trim())next+=1;
        if(next<lines.length&&/^[-*]\s+/.test(lines[next]))index=next;
      }
      html.push(`<ul class="md-list">${items.join('')}</ul>`);
      continue;
    }
    if(/^\d+\.\s+/.test(line)){
      const items=[];
      const start=Number(line.match(/^(\d+)\./)?.[1]||1);
      while(index<lines.length){
        if(!/^\d+\.\s+/.test(lines[index]))break;
        items.push(`<li>${inlineMarkdown(lines[index].replace(/^\d+\.\s+/,''))}</li>`);
        index+=1;
        let next=index;
        while(next<lines.length&&!lines[next].trim())next+=1;
        if(next<lines.length&&/^\d+\.\s+/.test(lines[next]))index=next;
      }
      html.push(`<ol class="md-list"${start===1?'':` start="${start}"`}>${items.join('')}</ol>`);
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
let agentProgrammaticScroll=false;
let reasoningStickToBottom=true;
let reasoningProgrammaticScroll=false;
function agentMessageList(){return $('#agent-message-list')}
function isNearScrollBottom(node,pad=120){return !!node&&node.scrollHeight-node.scrollTop-node.clientHeight<pad}
function scrollAgentReasoning(){
  const el=$('#agent-stream-reasoning');
  if(!el||!reasoningStickToBottom)return;
  reasoningProgrammaticScroll=true;
  el.scrollTop=el.scrollHeight;
  requestAnimationFrame(()=>{reasoningProgrammaticScroll=false});
}
function scrollAgentTranscript(force=false){
  const root=agentMessageList();
  if(!root)return;
  const follow=force||state.agentChat.stickToBottom;
  const pin=()=>{
    scrollAgentReasoning();
    if(!follow)return;
    const list=agentMessageList();
    if(!list)return;
    agentProgrammaticScroll=true;
    list.scrollTop=list.scrollHeight;
    requestAnimationFrame(()=>{agentProgrammaticScroll=false});
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
  root.addEventListener('scroll',()=>{
    if(agentProgrammaticScroll)return;
    state.agentChat.stickToBottom=isNearScrollBottom(root,160);
  },{passive:true});
}
function bindAgentReasoningScroll(){
  const el=$('#agent-stream-reasoning');
  if(!el||el.dataset.scrollBound)return;
  el.dataset.scrollBound='1';
  el.addEventListener('scroll',()=>{
    if(reasoningProgrammaticScroll)return;
    reasoningStickToBottom=isNearScrollBottom(el,48);
  },{passive:true});
}
function watchAgentTranscriptSize(){
  const root=agentMessageList();
  if(!root||typeof ResizeObserver==='undefined')return;
  if(!agentTranscriptObserver){
    agentTranscriptObserver=new ResizeObserver(()=>{
      if(state.agentChat.stickToBottom)scrollAgentTranscript();
      else scrollAgentReasoning();
    });
  }
  agentTranscriptObserver.disconnect();
  agentTranscriptObserver.observe(root);
  ['.codex-run.live','.chat-turn.live','#agent-stream-text','#agent-stream-reasoning','#agent-thinking','.codex-answer.streaming'].forEach(sel=>{
    const node=root.querySelector(sel);
    if(node)agentTranscriptObserver.observe(node);
  });
  bindAgentReasoningScroll();
}
const statusLabels={started:'已启动',queued:'队列中',running:'执行中',interrupt_requested:'中断中',interrupted:'已中断',cancel_requested:'取消中',cancelled:'已取消',timed_out:'已超时',failed:'失败',budget_exceeded:'预算耗尽',collecting_evidence:'收集证据',planning_action:'生成计划',waiting_approval:'等待审批',executing:'执行中',reviewing:'审核中',completed:'已完成',rejected:'已拒绝',review_failed:'审核失败',candidate:'待确认',conflicted:'有冲突',active:'已生效',superseded:'已替代',deleted:'已删除'};
const agentLabels={supervisor:'Supervisor',knowledge_agent:'Knowledge Agent',telemetry_agent:'Telemetry Agent',diagnosis_agent:'Diagnosis Agent',action_agent:'Action Agent',approval_gate:'Approval Gate',reviewer_agent:'Reviewer',finalizer:'Finalizer'};
function headers(body){const value={};if(!(body instanceof FormData))value['Content-Type']='application/json';if(state.session.accessToken)value.Authorization=`Bearer ${state.session.accessToken}`;else{value['X-Tenant-ID']=state.session.tenant;value['X-User-ID']=state.session.userId;value['X-User-Role']=state.session.role}if(state.session.apiKey)value['X-API-Key']=state.session.apiKey;return value}
async function parseApiResponse(response){if(!response.ok){let payload={};try{payload=await response.json()}catch{}const detail=payload.detail;const message=typeof detail==='object'&&detail?[detail.message,detail.hint].filter(Boolean).join('\n'):(detail||`请求失败 (${response.status})`);const error=new Error(message);error.status=response.status;if(typeof detail==='object'&&detail){error.code=detail.code;error.retryAfterSeconds=detail.retry_after_seconds;error.provider=detail.provider;error.hint=detail.hint}throw error}if(response.status===204)return null;return response.json()}
let accountRefreshInFlight=null;
function authRefreshExempt(path){
  return /^\/v1\/auth\/(login|register|refresh|logout)(?:\?|$)/.test(String(path||''));
}
function syncAccountTokensFromStorage(){
  state.session.accessToken=localStorage.getItem('arkflow.accessToken')||'';
  state.session.refreshToken=localStorage.getItem('arkflow.refreshToken')||'';
  return {accessToken:state.session.accessToken,refreshToken:state.session.refreshToken};
}
function expireAccountSession(message='登录已过期，请重新登录'){
  clearAccountSession();
  showAuthPane('login');
  const error=new Error(message);
  error.status=401;
  error.code='login_expired';
  throw error;
}
async function refreshAccountSession(){
  if(accountRefreshInFlight)return accountRefreshInFlight;
  const run=async()=>{
    const previous=state.session.refreshToken||localStorage.getItem('arkflow.refreshToken')||'';
    syncAccountTokensFromStorage();
    const token=state.session.refreshToken;
    if(!token)throw new Error('登录已过期');
    if(token!==previous&&state.session.accessToken)return syncAccountTokensFromStorage();
    const response=await fetch('/v1/auth/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refresh_token:token})});
    if(response.status===401){
      const stored=syncAccountTokensFromStorage();
      if(stored.refreshToken&&stored.refreshToken!==token&&stored.accessToken)return stored;
    }
    const result=await parseApiResponse(response);
    saveAccountSession(result);
    return result;
  };
  const pending=typeof navigator!=='undefined'&&navigator.locks?.request
    ?navigator.locks.request('arkflow-auth-refresh',run)
    :run();
  accountRefreshInFlight=Promise.resolve(pending).finally(()=>{accountRefreshInFlight=null});
  return accountRefreshInFlight;
}
async function authFetch(path,options={}){
  const send=()=>fetch(path,{...options,headers:{...headers(options.body),...(options.headers||{})}});
  let response=await send();
  if(response.status!==401||authRefreshExempt(path)||!(state.session.refreshToken||localStorage.getItem('arkflow.refreshToken')))return response;
  try{
    await refreshAccountSession();
  }catch{
    const stored=syncAccountTokensFromStorage();
    if(!(stored.accessToken&&stored.refreshToken))expireAccountSession();
  }
  response=await send();
  if(response.status===401)expireAccountSession();
  return response;
}
async function api(path,options={}){return parseApiResponse(await authFetch(path,options))}
async function downloadWorkspaceFile(path, sourceLink){
  if(!path){toast('没有可下载的文件','error');return}
  try{
    const auth={...headers()};
    delete auth['Content-Type'];
    const response=await authFetch(`/v1/agent/workspace/file?path=${encodeURIComponent(path)}`,{headers:auth});
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
const TOOL_CAPABILITY_AGENTS={amazon_finance_query:'amazon-finance-query',lingxing_profit_query:'lingxing-profit-report',profit_report_query:'profit-report-query',kingdee_cloud_query:'kingdee-cloud'};
function toolIconMarkup(tool){return tool.builtin?'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 12h8M12 8v8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><rect x="4" y="4" width="16" height="16" rx="4" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>':'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9.5 14.5 5 5M8.5 6.5l-3 3a2.1 2.1 0 0 0 0 3l7 7a2.1 2.1 0 0 0 3 0l3-3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M14 5l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'}
function capabilityAgent(toolName){const agentId=TOOL_CAPABILITY_AGENTS[toolName];return (state.agents||[]).find(item=>item.id===agentId)||null}
async function loadCatalog(){
  const root=$('#tool-grid');
  if(!root)return;
  state.catalog=state.catalog||await api('/v1/catalog');
  const {tools,tool_bindings:bindings=[]}=state.catalog;
  const bindingByTool=new Map(bindings.map(item=>[item.tool_name,item]));
  root.innerHTML=tools.map(tool=>{
    const binding=bindingByTool.get(tool.id);
    const capability=capabilityAgent(tool.id);
    const scopeName=binding?.resource_scope;
    const scopeValue=scopeName?(binding.resource_scopes?.[scopeName]||[]).join(','):'';
    const selector=binding?`<label class="tool-connection-select">Connection<select data-tool-connection="${escapeHTML(tool.id)}" ${binding.connections?.length?'':'disabled'}><option value="">未配置</option>${(binding.connections||[]).map(connection=>`<option value="${escapeHTML(connection.id)}"${connection.id===binding.connection_id?' selected':''}>${escapeHTML(connection.name)}${connection.enabled?'':'（已停用）'}</option>`).join('')}</select></label>${scopeName?`<label class="tool-connection-select">数据范围 · ${escapeHTML(scopeName)}<input data-tool-scope="${escapeHTML(tool.id)}" value="${escapeHTML(scopeValue)}" placeholder="逗号分隔，* 表示全部"><span class="form-hint">只能缩小 Connection 中配置的范围</span></label><button class="secondary" type="button" data-save-tool-binding="${escapeHTML(tool.id)}">保存连接与数据范围</button>`:''}${binding.connections?.length?'':`<button type="button" class="text-button" data-go="connectors">先创建连接器 →</button>`}`:'';
    const enableToggle=capability?`<label class="context-toggle tool-enable"><input type="checkbox" data-tool-enabled="${escapeHTML(tool.id)}" ${capability.enabled?'checked':''}>启用此工具</label>`:'';
    return`<article class="tool-card ${capability&&!capability.enabled?'disabled':''}"><div class="agent-icon tool-icon ${tool.builtin?'tool-icon-builtin':''}">${toolIconMarkup(tool)}</div><div class="tool-card-content"><h3>${escapeHTML(tool.id)}</h3><p>${tool.approval?'写操作 · 强制人工审批':'只读操作 · 策略自动批准'}</p><span class="tag">${escapeHTML(tool.risk)} risk</span><span class="tag">${escapeHTML(tool.mode)}</span>${tool.builtin?agentTagMarkup('builtin','内置'):''}${binding?agentTagMarkup('source-api',connectorLabel(binding.connector_type)):''}${enableToggle}${selector}</div></article>`;
  }).join('')||`<article class="panel empty"><b>没有已注册工具</b><span>Runtime 启动后会在这里列出。</span></article>`;
  $$('[data-tool-connection]',root).forEach(select=>select.addEventListener('change',()=>{if(!bindingByTool.get(select.dataset.toolConnection)?.resource_scope)bindToolConnection(select.dataset.toolConnection,select.value)}));
  $$('[data-save-tool-binding]',root).forEach(button=>button.addEventListener('click',()=>{const name=button.dataset.saveToolBinding;const connection=$(`[data-tool-connection="${CSS.escape(name)}"]`,root)?.value;const input=$(`[data-tool-scope="${CSS.escape(name)}"]`,root);bindToolConnection(name,connection,input?.value||'',bindingByTool.get(name)?.resource_scope)}));
  $$('[data-tool-enabled]',root).forEach(input=>input.addEventListener('change',()=>toggleToolCapability(input.dataset.toolEnabled,input.checked)));
  $$('[data-go]',root).forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.go)));
}
async function bindToolConnection(toolName,connectionId,scopeText='',scopeName=''){if(!connectionId){toast('请先创建并选择 Connection','error');return}const resource_scopes={};if(scopeName)resource_scopes[scopeName]=scopeText.split(',').map(item=>item.trim()).filter(Boolean);try{await api(`/v1/tools/${encodeURIComponent(toolName)}/connection`,{method:'PUT',body:JSON.stringify({connection_id:connectionId,resource_scopes})});toast('工具的连接与数据范围已保存','success');state.catalog=null;await loadToolsPage()}catch(error){toast(error.message,'error');state.catalog=null;await loadToolsPage()}}
async function toggleToolCapability(toolName,enabled){const agentId=TOOL_CAPABILITY_AGENTS[toolName];if(!agentId)return;try{await api(`/v1/agents/${encodeURIComponent(agentId)}`,{method:'PATCH',body:JSON.stringify({enabled})});toast(enabled?'工具已启用':'工具已停用','success');state.catalog=null;await loadToolsPage()}catch(error){toast(error.message,'error');state.catalog=null;await loadToolsPage()}}
function renderBuiltinToolChips(names,toolCatalog){const lookup=new Map((toolCatalog||[]).map(item=>[item.name,item]));$('#agent-builtin-tools').innerHTML=names.map(name=>{const tool=lookup.get(name)||{name};return `<span class="tool-chip locked" title="系统内置，不可移除">${escapeHTML(tool.name)}<span class="tag">builtin</span></span>`}).join('')||'<span class="form-hint">暂无</span>'}
function renderOptionalToolChips(names,toolCatalog){const lookup=new Map((toolCatalog||[]).map(item=>[item.name,item]));$('#agent-optional-tools').innerHTML=names.length?names.map(name=>{const tool=lookup.get(name)||{name,mode:'custom'};return `<span class="tool-chip" data-optional-tool="${escapeHTML(name)}">${escapeHTML(tool.name)}<span class="tag">${escapeHTML(tool.mode||'custom')}</span><button type="button" data-remove-tool="${escapeHTML(name)}" title="移除">×</button></span>`}).join(''):'<span class="form-hint">未追加额外工具，Runtime 可使用全部已注册工具。</span>';$$('[data-remove-tool]').forEach(button=>button.addEventListener('click',()=>removeOptionalTool(button.dataset.removeTool)))}
function renderToolMenuOptions(){const agent=state.editingAgent;if(!agent)return;const selected=new Set(state.editingOptionalTools||[]);const options=(agent.tool_catalog||[]).filter(tool=>!tool.builtin&&!selected.has(tool.name));const menu=$('#agent-tool-menu-list');if(!options.length){menu.innerHTML='<div class="form-hint" style="padding:8px">没有可添加的工具</div>';return}menu.innerHTML=options.map(tool=>`<button type="button" class="tool-picker-option" data-add-tool="${escapeHTML(tool.name)}"><strong>${escapeHTML(tool.name)}</strong><span>${escapeHTML(tool.mode||'custom')} · ${escapeHTML(tool.risk||'low')} risk</span></button>`).join('');$$('[data-add-tool]').forEach(button=>button.addEventListener('click',()=>addOptionalTool(button.dataset.addTool)))}
function closeToolMenu(){$('#agent-tool-menu').hidden=true}
function openToolMenu(){renderToolMenuOptions();$('#agent-tool-menu').hidden=false}
function addOptionalTool(name){if(!state.editingOptionalTools)state.editingOptionalTools=[];if(!state.editingOptionalTools.includes(name))state.editingOptionalTools.push(name);renderOptionalToolChips(state.editingOptionalTools,state.editingAgent?.tool_catalog);renderToolMenuOptions();closeToolMenu()}
function removeOptionalTool(name){state.editingOptionalTools=(state.editingOptionalTools||[]).filter(item=>item!==name);renderOptionalToolChips(state.editingOptionalTools,state.editingAgent?.tool_catalog);renderToolMenuOptions()}
function renderAgentToolPicker(agent){state.editingOptionalTools=[...(agent.optional_tools||[])];renderBuiltinToolChips(agent.builtin_tools||[],agent.tool_catalog);renderOptionalToolChips(state.editingOptionalTools,agent.tool_catalog);closeToolMenu()}
function agentStatusChip(status){const label=status==='active'?'运行中':status==='standby'?'待命':'已停用';return `<span class="status-chip ${status==='active'?'completed':status==='standby'?'waiting_approval':'disabled'}">${label}</span>`}
function agentIconMarkup(agent){
  const iconsById={
    'lingxing-profit-report':{cls:'api',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 17a4.5 4.5 0 1 1 9 0" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M11 17V9.5M11 9.5 8.5 7M11 9.5 13.5 7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 19h16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'},
    'profit-report-query':{cls:'database',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="6.5" rx="7" ry="3" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M5 6.5v4c0 1.66 3.13 3 7 3s7-1.34 7-3v-4M5 10.5v4c0 1.66 3.13 3 7 3s7-1.34 7-3v-4M5 14.5v4c0 1.66 3.13 3 7 3s7-1.34 7-3v-4" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>'},
    'kingdee-cloud':{cls:'erp',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5h14v14H5z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M8 9h8M8 13h5M8 17h3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'}
  };
  const custom=iconsById[agent.id];
  if(custom)return `<div class="agent-icon agent-icon-${custom.cls}" title="${escapeHTML(agent.name)}">${custom.svg}</div>`;
  const kind=agent.kind||'runtime';
  const iconKind=kind==='role'?'runtime':kind;
  const icons={runtime:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 9h8M8 13h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M7 4h10a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M16.5 17.5 19 20l3.5-3.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',role:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 9h8M8 13h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M7 4h10a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M16.5 17.5 19 20l3.5-3.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>','hybrid':'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 18V6a2 2 0 0 1 2-2h8l4 4v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M14 4v4h4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M8 13h8M8 17h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'};
  return `<div class="agent-icon agent-icon-${escapeHTML(iconKind)}" title="${escapeHTML(agent.name)}">${icons[kind]||icons.runtime}</div>`;
}
function agentTagMarkup(type,label){const icons={runtime:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5h10M3 8h6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',hybrid:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 12V4.5A1.5 1.5 0 0 1 4 3h5l2 2V12a1.5 1.5 0 0 1-1.5 1.5H4A1.5 1.5 0 0 1 2.5 12Z" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>',builtin:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" fill="none" stroke="currentColor" stroke-width="1.3"/><rect x="4" y="7" width="8" height="6" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>',tools:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5.8 10.2 3.3 12.7a1.2 1.2 0 1 1-1.7-1.7l2.5-2.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><path d="M9.5 3.5l3 3-5.8 5.8-3-3z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>','source-api':'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v5M8 7.5 6 5.5M8 7.5l2-2" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/><path d="M4 12.5h8" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>','source-db':'<svg viewBox="0 0 16 16" aria-hidden="true"><ellipse cx="8" cy="4.5" rx="5" ry="2" fill="none" stroke="currentColor" stroke-width="1.1"/><path d="M3 4.5v3c0 1.1 2.24 2 5 2s5-.9 5-2v-3M3 7.5v3c0 1.1 2.24 2 5 2s5-.9 5-2v-3" fill="none" stroke="currentColor" stroke-width="1.1"/></svg>'};return `<span class="tag tag-icon tag-${escapeHTML(type)}">${icons[type]||''}<span>${escapeHTML(label)}</span></span>`}
function agentTags(agent){const kindLabel=agent.kind==='runtime'?'Coordinator':agent.kind==='role'?'分析角色':agent.kind;const tags=[agentTagMarkup(agent.kind==='role'?'runtime':agent.kind,kindLabel)];if(agent.builtin)tags.push(agentTagMarkup('builtin','内置'));if(agent.allowed_tools?.length)tags.push(agentTagMarkup('tools',`${agent.allowed_tools.length} 工具`));return tags.join('')}
function renderAgentCard(agent,runtimeStatus=agent.status){
  return`<article class="agent-card ${runtimeStatus==='active'?'':'disabled'}"><div class="agent-card-head">${agentIconMarkup(agent)}${agentStatusChip(runtimeStatus)}</div><h3>${escapeHTML(agent.name)}</h3><p class="agent-role">${escapeHTML(agent.role)}</p><p class="form-hint agent-desc">${escapeHTML(agent.description||'')}</p><div class="agent-tags">${agentTags(agent)}</div><div class="agent-card-actions"><button class="secondary" data-edit-agent="${escapeHTML(agent.id)}">编辑配置</button></div></article>`;
}
function renderAgentCards(agents){
  const byId=Object.fromEntries(agents.map(item=>[item.id,item]));
  const coordinator=byId['function-calling-runtime'];
  const general=byId.analyst;
  const specialists=SPECIALIST_ANALYST_IDS.map(id=>byId[id]).filter(Boolean);
  const mode=state.configuration?.analyst_runtime?.mode||'general';
  const effectiveStatus=agent=>agent.status!=='active'?'disabled':agent.id==='function-calling-runtime'?'active':mode==='general'?(agent.id==='analyst'?'active':'standby'):(SPECIALIST_ANALYST_IDS.includes(agent.id)?'active':'standby');
  const root=$('#agent-sections');
  if(!root)return;
  const specialistSection=specialists.length?`<div class="agent-section"><h2 class="section-title">专业 Analyst <span class="section-sub">专业模式下按领域委派，最多并行 ${state.configuration?.analyst_runtime?.max_parallel||3} 个</span></h2><div class="agent-grid">${specialists.map(agent=>renderAgentCard(agent,effectiveStatus(agent))).join('')}</div></div>`:'';
  root.innerHTML=`<div class="agent-section"><h2 class="section-title">协调与通用分析</h2><div class="agent-grid agent-grid-core">${[coordinator,general].filter(Boolean).map(agent=>renderAgentCard(agent,effectiveStatus(agent))).join('')}</div></div>${specialistSection}`;
  $$('[data-edit-agent]',root).forEach(button=>button.addEventListener('click',()=>openAgentEditor(button.dataset.editAgent)));
}
function connectorLabel(type,databaseType='postgresql'){if(type==='analytics')return databaseType==='mysql'?'MySQL 数据库':'PostgreSQL 数据库';return({lingxing:'领星 OpenAPI',kingdee:'金蝶云星空',dingtalk:'钉钉 OpenAPI',qdrant:'Qdrant 向量数据库',milvus:'Milvus 向量数据库'})[type]||type}
function connectorIconMarkup(type){
  const icons={
    analytics:{cls:'database',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="6.5" rx="7" ry="3" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M5 6.5v4c0 1.66 3.13 3 7 3s7-1.34 7-3v-4M5 10.5v4c0 1.66 3.13 3 7 3s7-1.34 7-3v-4M5 14.5v4c0 1.66 3.13 3 7 3s7-1.34 7-3v-4" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>'},
    lingxing:{cls:'api',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 17a4.5 4.5 0 1 1 9 0" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M11 17V9.5M11 9.5 8.5 7M11 9.5 13.5 7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 19h16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'},
    kingdee:{cls:'erp',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5h14v14H5z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M8 9h8M8 13h5M8 17h3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'},
    dingtalk:{cls:'api',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 5 13 2-4 3 3 1-8 8 2-6-4-1 4-3z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>'},
    qdrant:{cls:'database',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="9" cy="10" r="1.5"/><circle cx="15" cy="8" r="1.5"/><circle cx="14" cy="15" r="1.5"/><path d="m10 10 4-2m-4 3 3 4" stroke="currentColor" stroke-width="1.4"/></svg>'},
    milvus:{cls:'database',svg:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7 12 3l7 4v10l-7 4-7-4z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="m5 7 7 4 7-4M12 11v10" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'}
  };
  const item=icons[type]||icons.analytics;
  return `<div class="agent-icon agent-icon-${item.cls}">${item.svg}</div>`;
}
function connectionHealthLabel(state){return({ready:'可用',misconfigured:'凭证不完整',circuit_open:'熔断'})[state]||(state||'未检测')}
function boundToolsForConnection(connectionId){
  return ((state.catalog||{}).tool_bindings||[]).filter(item=>item.connection_id===connectionId).map(item=>item.tool_name);
}
function renderConnectionCards(connections){
  const root=$('#connection-grid');
  if(!root)return;
  state.connections=connections;
  root.innerHTML=connections.length?connections.map(connection=>{
    const health=connection.health?.state||'未检测';
    const tools=boundToolsForConnection(connection.id);
    const vectorStore=['qdrant','milvus'].includes(connection.connector_type);
    return`<article class="agent-card ${connection.enabled&&health==='ready'?'':'disabled'}"><div class="agent-card-head">${connectorIconMarkup(connection.connector_type)}${agentStatusChip(health==='ready'?'active':'disabled')}</div><h3>${escapeHTML(connection.name)}</h3><p class="agent-role">${escapeHTML(connectorLabel(connection.connector_type,connection.database_type))}</p><p class="form-hint mono">${escapeHTML(connection.id)}</p><div class="agent-tags">${agentTagMarkup('source-api',connectionHealthLabel(health))}${tools.length?agentTagMarkup('tools',`${tools.length} 个工具`):agentTagMarkup('runtime',vectorStore?'知识库连接':'未绑定工具')}</div>${tools.length?`<p class="form-hint">${escapeHTML(tools.join(' · '))}</p>`:''}<div class="agent-card-actions"><button class="secondary" data-edit-connection="${escapeHTML(connection.id)}">编辑连接</button></div></article>`;
  }).join(''):`<article class="panel empty"><b>尚未配置连接器</b><span>先创建外部系统连接，再在工具页绑定。</span></article>`;
  $$('[data-edit-connection]',root).forEach(button=>button.addEventListener('click',()=>openConnectionEditor(button.dataset.editConnection)));
}
function csvValues(value){return String(value||'').split(',').map(item=>item.trim()).filter(Boolean)}
function updateAnalyticsDatabaseFields(){const databaseType=$('#connection-edit-database-type')?.value||'postgresql';const mysql=databaseType==='mysql';$('#connection-edit-dsn-label').textContent=mysql?'MySQL DSN':'PostgreSQL DSN';const dsn=$('#connection-edit-dsn');if(dsn&&!dsn.value&&!dsn.placeholder.startsWith('已配置'))dsn.placeholder=mysql?'mysql://user:password@host:3306/database':'postgresql://user:password@host/db'}
function updateConnectionFields(){const type=$('#connection-edit-type').value;$('#connection-fields-analytics').hidden=type!=='analytics';$('#connection-fields-lingxing').hidden=type!=='lingxing';$('#connection-fields-kingdee').hidden=type!=='kingdee';$('#connection-fields-dingtalk').hidden=type!=='dingtalk';$('#connection-fields-qdrant').hidden=type!=='qdrant';$('#connection-fields-milvus').hidden=type!=='milvus';$('#connection-scopes-data').hidden=['dingtalk','qdrant','milvus'].includes(type);$('#connection-scopes-dingtalk').hidden=type!=='dingtalk';updateAnalyticsDatabaseFields()}
function openConnectionEditor(connectionId){
  const connection=connectionId?state.connections.find(item=>item.id===connectionId):null;
  if(connectionId&&!connection){toast('连接器不存在','error');return}
  state.editingConnection=connection?{...connection,isNew:false}:{connector_type:'analytics',database_type:'postgresql',name:'',enabled:true,resource_scopes:{},isNew:true};
  const item=state.editingConnection;
  $('#connection-editor-title').textContent=item.isNew?'添加连接器':item.name;
  $('#connection-editor-subtitle').textContent=item.isNew?'在页面中保存连接凭证和数据范围':item.id;
  $('#connection-edit-name').value=item.name||'';$('#connection-edit-type').value=item.connector_type||'analytics';$('#connection-edit-type').disabled=!item.isNew;
  $('#connection-edit-database-type').value=item.database_type||'postgresql';$('#connection-edit-database-type').disabled=!item.isNew&&item.connector_type==='analytics';$('#connection-edit-enabled').checked=item.enabled!==false;
  $('#connection-edit-dsn').value='';$('#connection-edit-dsn').placeholder=item.dsn_configured?'已配置，留空保持不变':'';
  $('#connection-edit-lingxing-app-id').value=item.app_id||'';$('#connection-edit-lingxing-secret').value='';$('#connection-edit-lingxing-secret').placeholder=item.connector_type==='lingxing'&&item.app_secret_configured?'已配置，留空保持不变':'填写 App Secret';$('#connection-edit-lingxing-url').value=item.connector_type==='lingxing'?(item.base_url||'https://openapi.lingxing.com'):'https://openapi.lingxing.com';
  $('#connection-edit-kingdee-url').value=item.server_url||'';$('#connection-edit-kingdee-acct').value=item.acct_id||'';$('#connection-edit-kingdee-app-id').value=item.app_id||'';$('#connection-edit-kingdee-secret').value='';$('#connection-edit-kingdee-secret').placeholder=item.connector_type==='kingdee'&&item.app_secret_configured?'已配置，留空保持不变':'填写应用密钥';$('#connection-edit-kingdee-user').value=item.username||'';$('#connection-edit-kingdee-lcid').value=item.lcid||2052;
  $('#connection-edit-dingtalk-app-key').value=item.connector_type==='dingtalk'?(item.app_key||''):'';$('#connection-edit-dingtalk-secret').value='';$('#connection-edit-dingtalk-secret').placeholder=item.connector_type==='dingtalk'&&item.app_secret_configured?'已配置，留空保持不变':'填写 AppSecret';$('#connection-edit-dingtalk-robot-code').value=item.robot_code||'';$('#connection-edit-dingtalk-owner').value=item.default_todo_owner_union_id||'';$('#connection-edit-dingtalk-url').value=item.connector_type==='dingtalk'?(item.base_url||'https://api.dingtalk.com'):'https://api.dingtalk.com';
  $('#connection-edit-qdrant-url').value=item.connector_type==='qdrant'?(item.url||''):'';$('#connection-edit-qdrant-key').value='';$('#connection-edit-qdrant-key').placeholder=item.connector_type==='qdrant'&&item.api_key_configured?'已配置，留空保持不变':'本地无鉴权实例可留空';
  $('#connection-edit-milvus-uri').value=item.connector_type==='milvus'?(item.uri||''):'';$('#connection-edit-milvus-token').value='';$('#connection-edit-milvus-token').placeholder=item.connector_type==='milvus'&&item.token_configured?'已配置，留空保持不变':'无鉴权实例可留空';$('#connection-edit-milvus-database').value=item.connector_type==='milvus'?(item.db_name||'default'):'default';
  const scopes=item.resource_scopes||{};$('#connection-edit-stores').value=(scopes.store_names||[]).join(', ');$('#connection-edit-sids').value=(scopes.sids||[]).join(', ');$('#connection-edit-dingtalk-user-ids').value=(scopes.dingtalk_user_ids||[]).join(', ');$('#connection-edit-dingtalk-conversation-ids').value=(scopes.dingtalk_conversation_ids||[]).join(', ');$('#connection-edit-dingtalk-union-ids').value=(scopes.dingtalk_union_ids||[]).join(', ');
  $('#connection-edit-delete').hidden=item.isNew;updateConnectionFields();$('#connection-editor-drawer').classList.add('open');
}
function connectionEditorPayload(){
  const type=$('#connection-edit-type').value;const config={};const credentials={};
  if(type==='analytics'){config.database_type=$('#connection-edit-database-type').value||'postgresql';const dsn=$('#connection-edit-dsn').value.trim();if(dsn)credentials.dsn=dsn}
  else if(type==='lingxing'){config.app_id=$('#connection-edit-lingxing-app-id').value.trim();config.base_url=$('#connection-edit-lingxing-url').value.trim()||'https://openapi.lingxing.com';const secret=$('#connection-edit-lingxing-secret').value.trim();if(secret)credentials.app_secret=secret}
  else if(type==='kingdee'){config.server_url=$('#connection-edit-kingdee-url').value.trim();config.acct_id=$('#connection-edit-kingdee-acct').value.trim();config.app_id=$('#connection-edit-kingdee-app-id').value.trim();config.username=$('#connection-edit-kingdee-user').value.trim();config.lcid=Number($('#connection-edit-kingdee-lcid').value)||2052;const secret=$('#connection-edit-kingdee-secret').value.trim();if(secret)credentials.app_secret=secret}
  else if(type==='dingtalk'){config.app_key=$('#connection-edit-dingtalk-app-key').value.trim();config.robot_code=$('#connection-edit-dingtalk-robot-code').value.trim()||config.app_key;config.default_todo_owner_union_id=$('#connection-edit-dingtalk-owner').value.trim();config.base_url=$('#connection-edit-dingtalk-url').value.trim()||'https://api.dingtalk.com';const secret=$('#connection-edit-dingtalk-secret').value.trim();if(secret)credentials.app_secret=secret}
  else if(type==='qdrant'){config.url=$('#connection-edit-qdrant-url').value.trim();const apiKey=$('#connection-edit-qdrant-key').value.trim();if(apiKey)credentials.api_key=apiKey}
  else{config.uri=$('#connection-edit-milvus-uri').value.trim();config.db_name=$('#connection-edit-milvus-database').value.trim()||'default';const token=$('#connection-edit-milvus-token').value.trim();if(token)credentials.token=token}
  const resource_scopes={};
  if(type==='dingtalk'){const users=csvValues($('#connection-edit-dingtalk-user-ids').value);const groups=csvValues($('#connection-edit-dingtalk-conversation-ids').value);const unions=csvValues($('#connection-edit-dingtalk-union-ids').value);if(users.length)resource_scopes.dingtalk_user_ids=users;if(groups.length)resource_scopes.dingtalk_conversation_ids=groups;if(unions.length)resource_scopes.dingtalk_union_ids=unions}
  else if(!['qdrant','milvus'].includes(type)){const stores=csvValues($('#connection-edit-stores').value);const sids=csvValues($('#connection-edit-sids').value);if(stores.length)resource_scopes.store_names=stores;if(sids.length)resource_scopes.sids=sids}
  return{connector_type:type,name:$('#connection-edit-name').value.trim(),enabled:$('#connection-edit-enabled').checked,config,credentials,resource_scopes};
}
async function saveConnectionEditor(event){event.preventDefault();const editing=state.editingConnection;if(!editing)return;const payload=connectionEditorPayload();const submit=$('#connection-edit-save');submit.disabled=true;try{if(editing.isNew)await api('/v1/connections',{method:'POST',body:JSON.stringify(payload)});else{delete payload.connector_type;await api(`/v1/connections/${encodeURIComponent(editing.id)}`,{method:'PATCH',body:JSON.stringify(payload)})}toast(editing.isNew?'连接器已创建':'连接器已更新','success');$('#connection-editor-drawer').classList.remove('open');state.catalog=null;await loadConnectorsPage()}catch(error){toast(error.message,'error')}finally{submit.disabled=false}}
async function deleteConnectionEditor(){const editing=state.editingConnection;if(!editing||editing.isNew)return;if(!window.confirm(`确定删除连接器「${editing.name}」？`))return;try{await api(`/v1/connections/${encodeURIComponent(editing.id)}`,{method:'DELETE'});toast('连接器已删除','success');$('#connection-editor-drawer').classList.remove('open');state.catalog=null;await loadConnectorsPage()}catch(error){toast(error.message,'error')}}
async function loadAgentsPage(){const [agents,configuration]=await Promise.all([api('/v1/agents'),api('/v1/configuration')]);state.agents=agents.items;state.configuration=configuration;$('#analyst-mode-select').value=configuration.analyst_runtime?.mode||'general';renderAgentCards(agents.items)}
async function loadToolsPage(){const [agents,catalog]=await Promise.all([api('/v1/agents'),api('/v1/catalog')]);state.agents=agents.items;state.catalog=catalog;await loadCatalog()}
async function loadConnectorsPage(){const [connections,catalog]=await Promise.all([api('/v1/connections'),state.catalog?Promise.resolve(state.catalog):api('/v1/catalog')]);state.catalog=catalog;renderConnectionCards(connections.items)}
function knowledgeStatusChip(item){return `<span class="status-chip ${escapeHTML(item?.code||'')}">${escapeHTML(item?.label||'')}</span>`}
function knowledgeLibrary(){return state.knowledgeLibrary||{spaces:[],spaceId:'',categoryId:'',tree:[],documents:[],hits:[]}}
function renderKnowledgeTree(tree){
  const selected=knowledgeLibrary().categoryId||'';
  const walk=(nodes,depth)=>nodes.map(node=>{
    const active=selected===node.id?' active':'';
    const child=depth>0?' child':'';
    const virtual=node.virtual?' virtual':'';
    return `<button class="tree-item${active}${child}${virtual}" type="button" data-knowledge-category="${escapeHTML(node.id)}"><span>${escapeHTML(node.name)}</span><small>${node.document_count||0}</small></button>${walk(node.children||[],depth+1)}`;
  }).join('');
  $('#knowledge-tree').innerHTML=walk(tree||[],0);
  const parent=$('#knowledge-category-parent');
  const upload=$('#knowledge-upload-category');
  const options=[];
  const collect=(nodes,prefix)=>{nodes.forEach(node=>{if(node.virtual)return;options.push({id:node.id,name:`${prefix}${node.name}`});collect(node.children||[],`${prefix}${node.name} / `)})};
  collect(tree||[],'');
  const html=`<option value="">作为一级分类</option>${options.map(item=>`<option value="${escapeHTML(item.id)}">${escapeHTML(item.name)}</option>`).join('')}`;
  if(parent)parent.innerHTML=html;
  if(upload)upload.innerHTML=`<option value="">未分类</option>${options.map(item=>`<option value="${escapeHTML(item.id)}">${escapeHTML(item.name)}</option>`).join('')}`;
  $$('[data-knowledge-category]',$('#knowledge-tree')).forEach(button=>button.addEventListener('click',()=>{state.knowledgeLibrary.categoryId=button.dataset.knowledgeCategory||'';loadKnowledgeDocuments()}));
}
function renderKnowledgeDocuments(items){
  const empty=$('#knowledge-docs-empty');
  const body=$('#knowledge-docs');
  empty.hidden=items.length>0;
  body.innerHTML=items.map(doc=>`<tr data-knowledge-document="${escapeHTML(doc.id)}"><td><b>${escapeHTML(doc.title)}</b><small>${escapeHTML(doc.filename||'')}</small></td><td>${escapeHTML(doc.document_type_label||'')}</td><td>${escapeHTML(doc.category_name||'未分类')}</td><td>v${doc.version||1}</td><td>${knowledgeStatusChip(doc.parse_status)}</td><td>${knowledgeStatusChip(doc.index_status)}</td><td>${doc.chunk_count||'—'}</td></tr>`).join('');
  $$('[data-knowledge-document]',body).forEach(row=>row.addEventListener('click',()=>openKnowledgeDocument(row.dataset.knowledgeDocument)));
}
async function loadKnowledgeDocuments(){
  const lib=knowledgeLibrary();
  if(!lib.spaceId)return;
  const query=lib.categoryId?`?category_id=${encodeURIComponent(lib.categoryId)}`:'';
  const [categories,documents]=await Promise.all([
    api(`/v1/knowledge/library/spaces/${encodeURIComponent(lib.spaceId)}/categories`),
    api(`/v1/knowledge/library/spaces/${encodeURIComponent(lib.spaceId)}/documents${query}`),
  ]);
  lib.tree=categories.tree||[];
  lib.documents=documents.items||[];
  renderKnowledgeTree(lib.tree);
  renderKnowledgeDocuments(lib.documents);
  $('#knowledge-search-results').hidden=true;
  $('#knowledge-docs-wrap').hidden=false;
}
async function loadKnowledgePage(){
  const status=await api('/v1/knowledge/library/status');
  const unconfigured=$('#knowledge-unconfigured');
  const library=$('#knowledge-library');
  const upload=$('#knowledge-upload-button');
  const createSpace=$('#knowledge-create-space-button');
  if(!status.configured){
    unconfigured.hidden=false;
    library.hidden=true;
    if(upload)upload.hidden=true;
    if(createSpace)createSpace.hidden=true;
    return;
  }
  unconfigured.hidden=true;
  library.hidden=false;
  if(upload)upload.hidden=false;
  if(createSpace)createSpace.hidden=false;
  const listed=await api('/v1/knowledge/library/spaces');
  const spaces=listed.items||[];
  const stored=localStorage.getItem('arkflow.knowledgeSpaceId')||'';
  const spaceId=spaces.some(item=>item.id===stored)?stored:(spaces[0]?.id||'');
  state.knowledgeLibrary={spaces,spaceId,categoryId:'',tree:[],documents:[],hits:[]};
  fillKnowledgeSpaceSelect(spaces,spaceId);
  if(spaceId)await loadKnowledgeDocuments();
  else{$('#knowledge-tree').innerHTML='';renderKnowledgeDocuments([])}
}
function fillKnowledgeSpaceSelect(spaces,spaceId){
  const select=$('#knowledge-space-select');
  if(!select)return;
  select.innerHTML=spaces.length?spaces.map(item=>`<option value="${escapeHTML(item.id)}">${escapeHTML(item.name)} · ${item.document_count||0} 篇</option>`).join(''):'<option value="">请选择或新建知识空间</option>';
  select.value=spaceId||'';
}
async function openKnowledgeSpaceDrawer(){
  $('#knowledge-space-form').reset();
  const catalog=await api('/v1/knowledge/library/catalog');
  const select=$('#knowledge-space-embedding');
  const models=catalog.embedding_models||[];
  const selected=catalog.default_embedding_model||'';
  select.innerHTML=models.map(item=>`<option value="${escapeHTML(item.id)}"${item.id===selected?' selected':''}>${escapeHTML(item.label)} · ${item.vector_size} 维</option>`).join('');
  $('#knowledge-space-drawer').classList.add('open');
}
function closeKnowledgeSpaceDrawer(){$('#knowledge-space-drawer').classList.remove('open')}
async function createKnowledgeSpace(event){
  event.preventDefault();
  const created=await api('/v1/knowledge/library/spaces',{method:'POST',body:JSON.stringify({name:$('#knowledge-space-name').value.trim(),description:$('#knowledge-space-description').value.trim(),embedding_model:$('#knowledge-space-embedding').value})});
  localStorage.setItem('arkflow.knowledgeSpaceId',created.id);
  closeKnowledgeSpaceDrawer();
  toast('空间已创建并完成对接','success');
  await loadKnowledgePage();
}
async function createKnowledgeCategory(event){
  event.preventDefault();
  const lib=knowledgeLibrary();
  if(!lib.spaceId)return;
  await api(`/v1/knowledge/library/spaces/${encodeURIComponent(lib.spaceId)}/categories`,{method:'POST',body:JSON.stringify({name:$('#knowledge-category-name').value.trim(),parent_id:$('#knowledge-category-parent').value||null})});
  $('#knowledge-category-name').value='';
  toast('分类已创建','success');
  await loadKnowledgeDocuments();
}
async function searchKnowledgeLibrary(event){
  event.preventDefault();
  const lib=knowledgeLibrary();
  const query=$('#knowledge-search-query').value.trim();
  if(!lib.spaceId||!query)return;
  const result=await api(`/v1/knowledge/library/spaces/${encodeURIComponent(lib.spaceId)}/search`,{method:'POST',body:JSON.stringify({query,top_k:8,category_ids:lib.categoryId?[lib.categoryId]:[]})});
  const items=result.items||[];
  $('#knowledge-docs-wrap').hidden=true;
  const root=$('#knowledge-search-results');
  root.hidden=false;
  root.innerHTML=items.length?items.map(item=>`<article class="knowledge-content-item"><div class="knowledge-content-head"><span class="status-chip completed">${escapeHTML(item.title||'未命名')}</span><code>${Number(item.score||0).toFixed(4)}</code></div><p>${escapeHTML(item.text||'')}</p><div class="knowledge-content-meta">${item.page?`<span><b>页</b> ${item.page}</span>`:''}${item.category_id?`<span><b>分类</b> ${escapeHTML(item.category_id)}</span>`:''}</div></article>`).join(''):'<article class="empty"><b>没有命中切片</b><span>请确认文枢已按新 payload 重建向量，且租户为 tenant-a。</span></article>';
}
async function openKnowledgeDocument(documentId){
  $('#knowledge-content-drawer').classList.add('open');
  $('#knowledge-content-list').innerHTML='<article class="empty"><b>正在读取文档…</b></article>';
  try{
    const [doc,chunks]=await Promise.all([
      api(`/v1/knowledge/library/documents/${encodeURIComponent(documentId)}`),
      api(`/v1/knowledge/library/documents/${encodeURIComponent(documentId)}/chunks?limit=20`),
    ]);
    $('#knowledge-content-title').textContent=doc.title||'文档';
    $('#knowledge-content-summary').textContent=`${doc.filename||''} · ${doc.status_label||doc.status||''} · ${chunks.total||0} 个切片`;
    const jobs=(doc.jobs||[]).map(job=>`<span><b>${escapeHTML(job.job_type_label||job.job_type)}</b> ${escapeHTML(job.status_label||job.status)}</span>`).join('');
    const chunkHtml=(chunks.items||[]).map(chunk=>`<article class="knowledge-content-item"><div class="knowledge-content-head"><span class="status-chip completed">#${String((chunk.chunk_index||0)+1).padStart(3,'0')}</span>${chunk.page_start?`<code>第 ${chunk.page_start} 页</code>`:''}</div><p>${escapeHTML(chunk.text||'')}</p></article>`).join('');
    $('#knowledge-content-list').innerHTML=`<div class="knowledge-content-meta">${jobs}</div><div class="drawer-actions"><button class="secondary small" type="button" data-knowledge-reparse="${escapeHTML(doc.id)}">重新解析</button><button class="secondary small" type="button" data-knowledge-reindex="${escapeHTML(doc.id)}">重新向量化</button><button class="danger-button small" type="button" data-knowledge-delete="${escapeHTML(doc.id)}">删除</button></div>${chunkHtml||'<article class="empty"><b>还没有切片</b></article>'}`;
    $('[data-knowledge-reparse]')?.addEventListener('click',async()=>{await api(`/v1/knowledge/library/documents/${encodeURIComponent(doc.id)}/reparse`,{method:'POST'});toast('已提交重新解析','success');await openKnowledgeDocument(doc.id)});
    $('[data-knowledge-reindex]')?.addEventListener('click',async()=>{await api(`/v1/knowledge/library/documents/${encodeURIComponent(doc.id)}/reindex`,{method:'POST'});toast('已提交重新向量化','success');await openKnowledgeDocument(doc.id)});
    $('[data-knowledge-delete]')?.addEventListener('click',async()=>{if(!window.confirm(`确定删除「${doc.title}」？`))return;await api(`/v1/knowledge/library/documents/${encodeURIComponent(doc.id)}`,{method:'DELETE'});toast('文档已删除','success');$('#knowledge-content-drawer').classList.remove('open');await loadKnowledgeDocuments()});
  }catch(error){
    $('#knowledge-content-list').innerHTML=`<article class="empty"><b>读取失败</b><span>${escapeHTML(error.message)}</span></article>`;
    toast(error.message,'error');
  }
}
async function uploadKnowledgeDocument(event){
  event.preventDefault();
  const lib=knowledgeLibrary();
  const file=$('#knowledge-upload-file').files[0];
  if(!lib.spaceId||!file)return;
  const body=new FormData();
  body.append('file',file);
  body.append('title',$('#knowledge-upload-title').value.trim());
  body.append('document_type',$('#knowledge-upload-type').value);
  body.append('tags',$('#knowledge-upload-tags').value.trim());
  body.append('category_id',$('#knowledge-upload-category').value);
  const submit=$('#knowledge-upload-save');
  submit.disabled=true;
  try{
    const result=await api(`/v1/knowledge/library/spaces/${encodeURIComponent(lib.spaceId)}/documents`,{method:'POST',body});
    toast(result.skipped?'检测到相同内容，已跳过':'文件已上传，正在处理','success');
    $('#knowledge-upload-drawer').classList.remove('open');
    $('#knowledge-upload-form').reset();
    await loadKnowledgeDocuments();
  }catch(error){toast(error.message,'error')}
  finally{submit.disabled=false}
}
async function openAgentEditor(agentId){try{const agent=await api(`/v1/agents/${agentId}`);state.editingAgent=agent;$('#agent-editor-title').textContent=agent.name;$('#agent-editor-subtitle').textContent=`${agent.id} · ${agent.kind}`;$('#agent-edit-name').value=agent.name;$('#agent-edit-role').value=agent.role;$('#agent-edit-description').value=agent.description||'';$('#agent-edit-enabled').checked=!!agent.enabled;$('#agent-edit-system-prompt').value=agent.system_prompt||'';const toolsWrap=$('#agent-edit-tools-wrap');if(agent.id==='function-calling-runtime'||agent.kind==='role'){toolsWrap.hidden=false;const roleTools=agent.role_tools||agent.allowed_tools||[];renderBuiltinToolChips(roleTools,agent.tool_catalog);const optional=document.getElementById('agent-optional-tools');if(optional&&optional.parentElement)optional.parentElement.hidden=true;const add=document.getElementById('agent-add-tool-button');if(add&&add.parentElement)add.parentElement.hidden=true;const hint=toolsWrap.querySelector('.form-hint');if(hint)hint.textContent=agent.kind==='role'?'Analyst 使用独立严格工具白名单，不能继续委派。':'Coordinator 只保留委派工具；查数由当前模式允许的 Analyst 执行。';state.editingOptionalTools=[]}else{toolsWrap.hidden=true;state.editingOptionalTools=[]}$('#agent-editor-drawer').classList.add('open')}catch(error){toast(error.message)}}
async function saveAnalystMode(event){event.preventDefault();const mode=$('#analyst-mode-select').value;try{const result=await api('/v1/configuration/analyst-runtime',{method:'PATCH',body:JSON.stringify({mode})});if(state.configuration)state.configuration.analyst_runtime=result.analyst_runtime;toast(mode==='general'?'已切换为通用 Analyst':'已切换为并行专业 Analyst','success');await loadAgentsPage()}catch(error){toast(error.message,'error')}}
async function saveAgentEditor(event){event.preventDefault();const agent=state.editingAgent;if(!agent)return;const payload={name:$('#agent-edit-name').value.trim(),role:$('#agent-edit-role').value.trim(),description:$('#agent-edit-description').value.trim(),enabled:$('#agent-edit-enabled').checked,system_prompt:$('#agent-edit-system-prompt').value};const submit=$('#agent-edit-save');submit.disabled=true;try{await api(`/v1/agents/${agent.id}`,{method:'PATCH',body:JSON.stringify(payload)});toast('Agent 配置已保存');$('#agent-editor-drawer').classList.remove('open');closeToolMenu();await loadAgentsPage()}catch(error){toast(error.message)}finally{submit.disabled=false}}
async function loadConfiguration(){
  state.configuration=await api('/v1/configuration');
  const c=state.configuration;
  const system=$('#system-settings');
  if(system){
    const defaultModel=c.models?.items?.find(item=>item.is_default)||c.models?.items?.[0];
    const modelSummary=defaultModel?`${defaultModel.name} (${defaultModel.provider}/${defaultModel.model_name})`:'尚未配置';
    system.innerHTML=[['运行环境',c.environment,'当前部署环境'],['Session 事件',c.persistence.session_events,'Agent 对话事件库'],['默认模型',modelSummary,`已配置 ${c.models?.count||0} 个模型`],['控制面数据库',c.persistence.control_plane,'审计与配置索引'],['本回合 Token 预算',String(c.limits.run_token_budget),'超限会停止本回合，不是滑动窗口'],['安全策略','Secrets hidden','API 不返回任何密钥']].map(settingCard).join('');
  }
  await loadModelSettings();
  fillContextWindowForm(c.context_window);
}
function modelProviderLabel(provider){return ({zhipu:'智谱',qwen:'通义千问',deepseek:'DeepSeek',openai:'OpenAI'})[provider]||provider}
function renderModelCard(model){
  const capabilities=model.supports_image||model.supports_audio?[model.supports_image?'图片':'',model.supports_audio?'语音':''].filter(Boolean).join(' + '):'仅文本';
  return`<article class="agent-card ${model.enabled?'':'disabled'}"><div class="agent-card-head"><div class="agent-icon agent-icon-runtime" title="${escapeHTML(model.name)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l7 4v10l-7 4-7-4V7z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 12 19 8M12 12v9M12 12 5 8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></div>${model.is_default?'<span class="status-chip completed">默认</span>':agentStatusChip(model.enabled?'active':'disabled')}</div><h3>${escapeHTML(model.name)}</h3><p class="agent-role mono">${escapeHTML(model.id)}</p><p class="form-hint agent-desc">${escapeHTML(modelProviderLabel(model.provider))} · ${escapeHTML(model.model_name)}</p><div class="agent-tags">${model.api_key_configured?agentTagMarkup('tools','Key 已配置'):agentTagMarkup('runtime','未配置 Key')}${agentTagMarkup('runtime',capabilities)}</div><div class="agent-card-actions"><button class="secondary" data-edit-model="${escapeHTML(model.id)}">编辑配置</button></div></article>`;
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
  $('#model-edit-supports-image').checked=!!(model.supports_image_input??model.supports_image??model.supports_vision);
  $('#model-edit-supports-image').onchange=updateModelCapabilityFields;
  $('#model-edit-supports-audio').checked=!!(model.supports_audio_input??model.supports_audio);
  $('#model-edit-temperature').value=model.temperature??'';
  $('#model-edit-enable-thinking').checked=model.enable_thinking!==false;
  $('#model-edit-thinking-budget').value=model.thinking_budget??'';
  $('#model-edit-reasoning-effort').value=model.reasoning_effort||'high';
  $('#model-edit-enabled').checked=model.enabled!==false;
  $('#model-edit-default').checked=!!model.is_default;
  $('#model-edit-delete').hidden=isNew;
  applyModelProviderDefaults({preserveValues:true});
  updateModelCapabilityFields();
}
function updateModelCapabilityFields(){
  const supportsImage=!!$('#model-edit-supports-image')?.checked;
  const visionWrap=$('#model-edit-vision-model-wrap');
  if(visionWrap)visionWrap.hidden=!supportsImage;
}
const QWEN_DEFAULT_BASE_URL='https://dashscope.aliyuncs.com/compatible-mode/v1';
const DEEPSEEK_DEFAULT_BASE_URL='https://api.deepseek.com';
function applyModelProviderDefaults({preserveValues=false}={}){
  const provider=$('#model-edit-provider')?.value;
  const thinkingWrap=$('#model-edit-thinking-wrap');
  const budgetWrap=$('#model-edit-thinking-budget-wrap');
  const effortWrap=$('#model-edit-reasoning-effort-wrap');
  const hint=$('#model-edit-thinking-hint');
  if(thinkingWrap)thinkingWrap.hidden=provider!=='qwen'&&provider!=='deepseek'&&provider!=='zhipu';
  if(budgetWrap)budgetWrap.hidden=provider!=='qwen';
  if(effortWrap)effortWrap.hidden=provider!=='deepseek'&&provider!=='zhipu';
  if(hint){
    hint.textContent=provider==='deepseek'
      ?'普通多轮不必回传思考过程。一旦调用了工具，后续请求会自动带回 reasoning_content，避免 400。名称含 reasoner 的模型会强制开启思考。'
      :provider==='zhipu'
        ?'GLM-4.5 及以上支持思考。调用工具时会回传 reasoning_content 并设置 clear_thinking=false。GLM-4.7 / GLM-5.3 为强制思考；reasoning_effort 仅 GLM-5.2 及以上生效。glm-4-flash 不支持思考。'
        :'多轮对话会保留问答和工具结果，但不会把上一轮思考过程回传给模型。名称含 thinking 的模型会强制开启。';
  }
  const base=$('#model-edit-base-url');
  const name=$('#model-edit-model-name');
  if(!base||!name)return;
  if(provider==='qwen'){
    if(!base.value.trim())base.value=QWEN_DEFAULT_BASE_URL;
    base.placeholder=QWEN_DEFAULT_BASE_URL;
    name.placeholder='qwen3.7-plus';
  }else if(provider==='deepseek'){
    if(!base.value.trim())base.value=DEEPSEEK_DEFAULT_BASE_URL;
    base.placeholder=DEEPSEEK_DEFAULT_BASE_URL;
    name.placeholder='deepseek-chat';
  }else if(!preserveValues){
    base.placeholder='可选，智谱默认留空';
    name.placeholder='glm-5.2';
  }else{
    base.placeholder='可选，智谱默认留空';
    name.placeholder='glm-5.2';
  }
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
    vision_model_name:$('#model-edit-supports-image').checked?$('#model-edit-vision-model').value.trim():'',
    supports_image_input:$('#model-edit-supports-image').checked,
    supports_audio_input:$('#model-edit-supports-audio').checked,
    enabled:$('#model-edit-enabled').checked,
    is_default:$('#model-edit-default').checked,
  };
  const temperature=$('#model-edit-temperature').value.trim();
  if(temperature!=='')payload.temperature=Number(temperature);
  if(payload.provider==='qwen'||payload.provider==='deepseek'||payload.provider==='zhipu'){
    payload.enable_thinking=$('#model-edit-enable-thinking').checked;
  }
  if(payload.provider==='qwen'){
    const budget=$('#model-edit-thinking-budget').value.trim();
    if(budget!=='')payload.thinking_budget=Number(budget);
  }
  if(payload.provider==='deepseek'||payload.provider==='zhipu'){
    payload.reasoning_effort=$('#model-edit-reasoning-effort').value||'high';
  }
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
  state.agentChat.models=models;
  const selected=selectedAgentModelId(models,data.default_model_id);
  state.agentChat.selectedModelId=selected;
  if(selected)localStorage.setItem('arkflow.modelId',selected);
  select.innerHTML=models.map(item=>`<option value="${escapeHTML(item.id)}"${item.id===selected?' selected':''}>${escapeHTML(item.name)} (${escapeHTML(item.provider)}/${escapeHTML(item.model_name)})</option>`).join('');
  if(!models.length)select.innerHTML='<option value="">请先在系统设置中配置模型</option>';
  select.disabled=!models.length;
  updateAgentAttachmentCapability();
  updateAgentSendButton();
}
function selectedAgentModel(){return(state.agentChat.models||[]).find(item=>item.id===state.agentChat.selectedModelId)||null}
function updateAgentAttachmentCapability(){const button=$('#agent-attach-image');const model=selectedAgentModel();if(!button)return;button.disabled=!model||!model.supports_image||state.agentChat.sending;button.title=model&&!model.supports_image?'当前模型未开启图片输入能力':''}
function onAgentModelChange(){
  const select=$('#agent-model-select');
  if(!select)return;
  state.agentChat.selectedModelId=select.value;
  if(select.value)localStorage.setItem('arkflow.modelId',select.value);
  else localStorage.removeItem('arkflow.modelId');
  const model=selectedAgentModel();
  if(state.agentChat.pendingAttachments.length&&model&&!model.supports_image){state.agentChat.pendingAttachments=[];renderAgentAttachmentPreview();toast('当前模型不支持图片，已移除待发送图片','error')}
  updateAgentAttachmentCapability();
  updateAgentSendButton();
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
const auditActionLabels={'account.registered':'账户注册','account.login_succeeded':'登录成功','account.login_failed':'登录失败','account.password_changed':'修改密码','account.password_reset':'重置密码','access.user_saved':'保存用户','access.permission_group_updated':'修改权限组','agent.updated':'更新 Agent','agent_session.deleted':'删除会话','connection.created':'创建连接','connection.updated':'更新连接','connection.deleted':'删除连接','model.created':'创建模型','model.updated':'更新模型','model.deleted':'删除模型','tool.connection_bound':'绑定 Tool 连接','analyst_runtime.mode_updated':'切换 Analyst 模式','amazon_finance.queried':'查询 Amazon 财务','lingxing_profit.queried':'查询领星利润','profit_report.queried':'查询利润报表','kingdee_cloud.queried':'查询金蝶云','memory.created':'创建记忆','memory.confirmed':'确认记忆','memory.rejected':'拒绝记忆','memory.corrected':'纠正记忆','memory.forgotten':'删除记忆','memory.compliance_deleted':'合规删除记忆'};
const auditResourceLabels={account:'账户',access_user:'用户',permission_group:'权限组',agent:'Agent',agent_session:'Agent 会话',agent_tool_approval:'Tool 审批',connection:'连接',model:'模型',tool:'Tool',runtime_configuration:'运行时配置',amazon_finance:'Amazon 财务',lingxing_profit:'领星利润',profit_report:'利润报表',kingdee_cloud:'金蝶云',memory:'记忆',user_memory:'用户记忆'};
function auditStatus(action){if(action.endsWith('_failed')||action.endsWith('.rejected'))return ['失败','audit-failed'];if(action.includes('deleted')||action.endsWith('.forgotten'))return ['已删除','audit-warning'];if(action.endsWith('_succeeded')||action.endsWith('.confirmed')||action.endsWith('.created')||action.endsWith('.registered'))return ['成功','audit-success'];return ['已记录','audit-neutral']}
function auditDetail(detail){if(!detail||!Object.keys(detail).length)return '—';if(detail.code==='invalid_credentials')return '账号或密码错误';return Object.entries(detail).map(([key,value])=>`${key}: ${typeof value==='object'?JSON.stringify(value):value}`).join(' · ')}
async function loadAudit(){const data=await api('/v1/audit-events');state.audit=data.items;$('#audit-table').innerHTML=data.items.length?data.items.map(item=>{const [status,statusClass]=auditStatus(item.action);return `<tr><td>${formatTime(item.created_at)}</td><td><b>${escapeHTML(item.actor_id)}</b><br><span class="mono">${escapeHTML(item.actor_role==='unknown'?'未认证':item.actor_role)}</span></td><td><span class="audit-status ${statusClass}">${status}</span><b class="audit-action">${escapeHTML(auditActionLabels[item.action]||item.action)}</b><span class="mono audit-code">${escapeHTML(item.action)}</span></td><td>${escapeHTML(auditResourceLabels[item.resource_type]||item.resource_type)}<br><span class="mono">${escapeHTML(item.resource_id.slice(0,18))}</span></td><td class="audit-detail">${escapeHTML(auditDetail(item.detail))}</td></tr>`}).join(''):`<tr><td colspan="5"><div class="empty">暂无审计事件</div></td></tr>`}
function agentStorageKey(){return`arkflow.agentSessions.${state.session.tenant}.${state.session.userId}`}
function loadStoredAgentSessions(){try{return JSON.parse(localStorage.getItem(agentStorageKey())||'[]')}catch{return[]}}
function rememberAgentSession(id,title){const sessions=loadStoredAgentSessions().filter(item=>item.id!==id);sessions.unshift({id,title:title.slice(0,48),updatedAt:new Date().toISOString()});state.agentChat.sessions=sessions.slice(0,50);localStorage.setItem(agentStorageKey(),JSON.stringify(state.agentChat.sessions));localStorage.setItem(`${agentStorageKey()}.current`,id)}
function forgetAgentSession(id){state.agentChat.sessions=loadStoredAgentSessions().filter(item=>item.id!==id);localStorage.setItem(agentStorageKey(),JSON.stringify(state.agentChat.sessions));if(localStorage.getItem(`${agentStorageKey()}.current`)===id)localStorage.removeItem(`${agentStorageKey()}.current`)}
function saveAgentSessions(sessions){state.agentChat.sessions=sessions;localStorage.setItem(agentStorageKey(),JSON.stringify(sessions))}
async function restoreAgentSessions(){const local=loadStoredAgentSessions();const response=await api('/v1/agent/sessions?limit=50');const remote=(response.items||[]).map(item=>({id:item.session_id,title:item.title,updatedAt:item.updated_at}));const remoteById=new Map(remote.map(item=>[item.id,item]));const merged=[];for(const item of local){const current=remoteById.get(item.id);if(!current)continue;merged.push({...current,title:item.title||current.title});remoteById.delete(item.id)}for(const item of remote){if(remoteById.has(item.id)){merged.push(item);remoteById.delete(item.id)}}saveAgentSessions(merged);const currentId=localStorage.getItem(`${agentStorageKey()}.current`);if(currentId&&!merged.some(item=>item.id===currentId))localStorage.removeItem(`${agentStorageKey()}.current`)}
function moveAgentSession(id,direction){const sessions=[...state.agentChat.sessions];const index=sessions.findIndex(item=>item.id===id);if(index<0)return;const target=index+direction;if(target<0||target>=sessions.length)return;[sessions[index],sessions[target]]=[sessions[target],sessions[index]];saveAgentSessions(sessions);renderAgentSessionList();toast(direction<0?'会话已上移':'会话已下移')}
function closeAgentSessionMenu(){$$('.agent-session-menu-panel').forEach(panel=>{panel.hidden=true});$$('.agent-session-menu').forEach(menu=>menu.classList.remove('open'))}
function toggleAgentSessionMenu(id,trigger){const panel=trigger.parentElement.querySelector('.agent-session-menu-panel');const opening=panel.hidden;closeAgentSessionMenu();if(opening){panel.hidden=false;trigger.closest('.agent-session-menu').classList.add('open')}}
function updateAgentSessionActions(){const button=$('#agent-delete-session');if(!button)return;button.disabled=!state.agentChat.sessionId||state.agentChat.sending}
function readFileDataUrl(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=()=>reject(reader.error);reader.readAsDataURL(file)})}
async function uploadAgentImage(file){const dataUrl=await readFileDataUrl(file);const [prefix,dataBase64]=dataUrl.split(',',2);const mediaType=(prefix.match(/^data:([^;]+);base64$/)||[])[1];if(!mediaType)throw new Error('无法识别图片格式');const reference=await api('/v1/agent/attachments',{method:'POST',body:JSON.stringify({name:file.name,media_type:mediaType,data_base64:dataBase64})});return{reference,dataUrl}}
async function handleAgentImageSelect(event){const files=[...(event.target.files||[])];if(!files.length)return;const model=selectedAgentModel();if(!model?.supports_image){toast('当前模型不支持图片输入，请先更换模型','error');event.target.value='';return}$('#agent-attach-image').disabled=true;try{for(const file of files){if(state.agentChat.pendingAttachments.length>=20)throw new Error('每条消息最多 20 张图片');const uploaded=await uploadAgentImage(file);state.agentChat.pendingAttachments.push(uploaded)}renderAgentAttachmentPreview()}catch(error){toast(error.message)}finally{updateAgentAttachmentCapability();event.target.value=''}}
function renderAgentAttachmentPreview(){const root=$('#agent-image-preview');root.innerHTML=state.agentChat.pendingAttachments.map((item,index)=>`<span class="chat-image-item"><img src="${item.dataUrl}" alt="${escapeHTML(item.reference.name||'图片')}"><button type="button" data-remove-agent-image="${index}" aria-label="移除">×</button></span>`).join('');$$('[data-remove-agent-image]',root).forEach(button=>button.addEventListener('click',()=>{state.agentChat.pendingAttachments.splice(Number(button.dataset.removeAgentImage),1);renderAgentAttachmentPreview()}))}
const DEFERRED_ANSWER_MARKERS=['正在收集','正在获取','正在查询','请稍等','请稍候','稍后反馈','完成后反馈','完成后汇总'];
function isDeferredAnswer(text){return DEFERRED_ANSWER_MARKERS.some(marker=>String(text||'').includes(marker))}
function specialistAnswerFromSteps(steps){
  const sections=[];
  for(const step of steps||[]){
    if(step.kind!=='tool'||step.name!=='delegate_specialists'||!step.ok)continue;
    for(const task of step.output?.tasks||[]){
      const answer=String(task?.answer||'').trim();
      if(answer)sections.push(`### ${task.agent_id||'专业 Analyst'}\n${answer}`);
    }
  }
  return sections.join('\n\n');
}
function projectAgentTurns(events){
  const items=[];
  let turn=null;
  const ensureTurn=(meta={})=>{
    if(!turn){
      turn={kind:'turn',provider:meta.provider||'',model:meta.model||'',texts:[],steps:[],final:null,reasoning:''};
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
      if(payload.reasoning_content)current.reasoning=String(payload.reasoning_content);
      continue;
    }
    if(event.event_type==='delegation.parallelized'){
      ensureTurn({}).steps.push({kind:'parallel',...payload});
      continue;
    }
    if(event.event_type==='turn.interrupt_requested'||event.event_type==='turn.interrupted'){
      ensureTurn({}).steps.push({kind:'control',eventType:event.event_type,...payload});
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
      const current=ensureTurn({});
      const existing=payload.task_id
        ?current.steps.find(step=>step.kind==='subagent'&&step.task_id===payload.task_id)
        :null;
      if(existing){
        existing.eventType=event.event_type;
        Object.assign(existing,payload);
      }else{
        current.steps.push({kind:'subagent',eventType:event.event_type,...payload});
      }
      continue;
    }
    if(event.event_type==='turn.completed'){
      const current=ensureTurn({});
      if(payload.status==='waiting_approval')continue;
      let answer=visibleAssistantText(payload.answer||'');
      const specialistAnswer=specialistAnswerFromSteps(current.steps);
      if(specialistAnswer&&(!answer||isDeferredAnswer(answer))){
        answer=specialistAnswer;
        current.texts=current.texts.filter(text=>!isDeferredAnswer(text));
      }
      if(answer==='高风险工具正在等待逐次人工审批。')continue;
      if(answer&&answer!==current.texts.at(-1))current.final={content:answer,status:payload.status};
    }
  }
  return items;
}
function liveAgentStatus(){
  if(!state.agentChat.sending)return'';
  if(state.agentChat.stream?.text)return'正在生成回答';
  if(state.agentChat.stream?.reasoning)return'正在思考';
  const last=state.agentChat.events.at(-1);
  const type=last?.event_type;
  const payload=last?.payload||{};
  if(type==='subagent.started')return`队列中 · ${payload.agent_id||'Analyst'}`;
  if(type==='subagent.running')return`执行中 · ${payload.agent_id||'Analyst'}`;
  if(type==='subagent.finished')return'子任务完成，正在汇总';
  if(type==='delegation.parallelized')return'正在创建并行任务队列';
  if(type==='turn.interrupt_requested')return'正在保存中断检查点';
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
    delegate_subagent:'交给分析助手',
    delegate_specialists:'并行分析',
    sandbox_workspace_write:'写入工作区',
    sandbox_read_only:'读取本地文件',
    sandbox_full_access:'无隔离命令',
    amazon_finance_query:'Amazon 结算查询',
    lingxing_profit_query:'领星开放平台 API',
    profit_report_query:'利润报表 PostgreSQL',
    kingdee_cloud_query:'金蝶云星空 WebAPI',
    dingtalk_send_direct_message:'发送钉钉单聊',
    dingtalk_send_group_message:'发送钉钉群聊',
    dingtalk_create_todo:'创建钉钉待办',
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
  if(name==='dingtalk_send_direct_message')return `向 ${args.user_id||'未知用户'} 发送单聊：${String(args.content||'').slice(0,120)}`;
  if(name==='dingtalk_send_group_message')return `向群 ${args.open_conversation_id||'未知群'} 发送消息：${String(args.content||'').slice(0,120)}`;
  if(name==='dingtalk_create_todo')return `创建待办「${args.subject||'未填写标题'}」，执行人：${(args.executor_union_ids||[]).join('、')||'未指定'}`;
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
  const resultRef=output?.result_ref;
  const resultAction=resultRef?`<button type="button" class="secondary tool-result-action" data-view-result="${escapeHTML(resultRef)}">查看完整结果 · ${escapeHTML(output.returned_rows??'')} 行</button>`:'';
  if(!blocks.length&&!resultAction)return'';
  const action=step.name?toolDisplayName(step.name):'执行';
  return `${resultAction}<details class="tool-trace"><summary>查看${escapeHTML(action)}详情</summary>${blocks.join('')}</details>`;
}
function resultCell(value){if(value==null)return'';if(typeof value==='object')return JSON.stringify(value);return String(value)}
function renderResultViewer(data){
  state.resultViewer.data=data;
  $('#result-viewer-title').textContent=`${data.tool_name} · 完整结果`;
  $('#result-viewer-summary').textContent=data.summary||'该结果由确定性查询或计算引擎生成。';
  const quality=data.data_quality||{};
  const calculation=data.calculation||{};
  $('#result-viewer-stats').innerHTML=[['源数据行',data.source_rows],['结果行',data.returned_rows],['空值单元格',quality.null_cells??0],['计算引擎',calculation.engine||'工具']].map(([label,value])=>`<div class="result-stat"><b>${escapeHTML(value??'-')}</b><span>${escapeHTML(label)}</span></div>`).join('');
  const columns=(data.columns||[]).slice(0,50);
  $('#result-viewer-head').innerHTML=`<tr>${columns.map(name=>`<th>${escapeHTML(name)}</th>`).join('')}</tr>`;
  $('#result-viewer-body').innerHTML=(data.rows||[]).map(row=>`<tr>${columns.map(name=>`<td>${escapeHTML(resultCell(row?.[name]))}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${Math.max(1,columns.length)}">该页没有数据</td></tr>`;
  const start=data.returned_rows?data.offset+1:0;
  const end=data.offset+(data.rows||[]).length;
  $('#result-viewer-page').textContent=`${start}-${end} / ${data.returned_rows} 行（源数据 ${data.source_rows} 行）`;
  $('#result-viewer-prev').disabled=data.offset<=0;
  $('#result-viewer-next').disabled=!data.has_more;
}
async function openResultViewer(resultRef,offset=0){
  try{
    const limit=state.resultViewer.limit;
    const data=await api(`/v1/agent/results/${encodeURIComponent(resultRef)}?offset=${offset}&limit=${limit}`);
    state.resultViewer={resultRef,offset,limit,data};
    renderResultViewer(data);
    $('#result-viewer-drawer').classList.add('open');
  }catch(error){toast(error.message,'error')}
}
function moveResultPage(direction){const viewer=state.resultViewer;if(!viewer.resultRef)return;openResultViewer(viewer.resultRef,Math.max(0,viewer.offset+direction*viewer.limit))}
function renderToolStep(step,key){
  const enter=enterClass(key);
  if(step.kind==='parallel'){
    return`<div class="chat-step${enter}"><div class="tool-event subagent-event"><b>并行计划已生效</b><span>${escapeHTML(step.original_calls||0)} 个委派 → ${escapeHTML(step.parallel_batches||0)} 个批次，每批最多 ${escapeHTML(step.max_parallel||3)} 个</span></div></div>`;
  }
  if(step.kind==='control'){
    const interrupted=step.eventType==='turn.interrupted';
    return`<div class="chat-step${enter}"><div class="tool-event governance-event"><b>${interrupted?'执行已中断':'正在中断'}</b><span>${interrupted?'可以点击“继续执行”从检查点恢复':'正在停止当前模型与子任务'}</span></div></div>`;
  }
  if(step.kind==='governance'){
    const summary=summarizeTool(step.tool_name,step.arguments||{});
    if(step.eventType==='approval.requested'&&!step.decided){
      return`<div class="chat-step governance${enter}"><div class="tool-event governance-event"><div><b>等待审批</b><span>${escapeHTML(summary)}</span></div><div class="chat-approval-actions"><button type="button" class="danger-button" data-chat-reject="${escapeHTML(step.approval_id)}">拒绝</button><button type="button" class="primary small" data-chat-approve="${escapeHTML(step.approval_id)}">批准本次</button></div></div>${toolTraceHtml(step)}</div>`;
    }
    const verdict=step.approved===false?'已拒绝':'已批准';
    return`<div class="chat-step${enter}"><div class="tool-event governance-event"><b>${verdict}</b><span>${escapeHTML(summary||step.tool_name||step.call_id||'')}</span></div>${toolTraceHtml(step)}</div>`;
  }
  if(step.kind==='subagent'){
    const status=step.status||(step.eventType==='subagent.started'?'queued':'');
    const statusText={queued:'队列中',running:'执行中',completed:'已完成',failed:'失败',cancelled:'已取消',timed_out:'已超时',budget_exceeded:'预算耗尽'}[status]||status||'状态更新';
    const title=step.agent_id?`${statusText} · ${step.agent_id}`:statusText;
    return`<div class="chat-step${enter}"><div class="tool-event subagent-event ${escapeHTML(status)}"><b>${escapeHTML(title)}</b><span>${escapeHTML(step.objective||step.task_id||'')}</span></div></div>`;
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
function renderReasoningBlock(text,{open=false,liveId='',live=false}={}){
  const content=String(text||'').trim();
  if(!content)return '';
  const body=`<pre class="chat-reasoning-body"${liveId?` id="${liveId}"`:''}>${escapeHTML(content)}</pre>`;
  return `<details class="chat-reasoning${live?' live':''}"${open?' open':''}><summary>思考过程</summary>${body}</details>`;
}
function groupCodexBlocks(items){
  const blocks=[];
  for(let index=0;index<items.length;index+=1){
    const item=items[index];
    if(item.kind==='user'){
      const next=items[index+1];
      if(next&&next.kind==='turn'){
        blocks.push({user:item,run:next,userIndex:index,runIndex:index+1});
        index+=1;
      }else{
        blocks.push({user:item,run:null,userIndex:index,runIndex:-1});
      }
    }else{
      blocks.push({user:null,run:item,userIndex:-1,runIndex:index});
    }
  }
  return blocks;
}
function renderCodexPrompt(item,index){
  const enter=enterClass(`user-${index}`);
  const attachments=item.attachments?.length?`<div class="chat-attachment-badges">${item.attachments.map(file=>`<span class="tag">${escapeHTML(file.name||file.media_type)} · ${file.width}×${file.height}</span>`).join('')}</div>`:'';
  return`<div class="codex-prompt${enter}"><div class="codex-prompt-meta">你 · ${formatTime(item.at)}</div><p>${escapeHTML(item.content)}</p>${attachments}</div>`;
}
function renderCodexRun(item,index){
  const enter=enterClass(`turn-${index}`);
  const steps=item.steps.map((step,stepIndex)=>renderToolStep(step,`step-${index}-${stepIndex}`)).join('');
  const texts=item.texts.map(text=>`<div class="chat-answer markdown-body">${renderMarkdown(text)}</div>`).join('');
  const final=item.final&&item.final.content!==item.texts.at(-1)?`<div class="chat-final-text markdown-body">${renderMarkdown(item.final.content)}</div>`:'';
  const streamText=item.streamText?`<div id="agent-stream-text" class="chat-answer markdown-body">${renderMarkdown(item.streamText)}</div>`:'';
  const reasoningText=item.live?(item.streamReasoning||item.reasoning||''):item.reasoning;
  const reasoning=renderReasoningBlock(reasoningText,{open:!!item.live&&!!reasoningText,live:!!item.live,liveId:item.live?'agent-stream-reasoning':''});
  const thinking=!item.streamText&&!item.streamReasoning&&item.live?`<div class="chat-thinking" id="agent-thinking"><span></span><span></span><span></span><b>${escapeHTML(item.status||'正在思考')}</b></div>`:'';
  const status=item.live?item.status||'工作中':'已完成';
  const activity=reasoning||steps||thinking;
  const answer=texts+final+streamText;
  return`<div class="codex-run chat-turn${enter}${item.live?' live':''}"><div class="codex-run-head chat-meta">${escapeHTML(status)}${item.provider||item.model?` · ${escapeHTML(modelLabel(item.provider,item.model))}`:''}</div>${activity?`<div class="codex-activity"><div class="codex-section-label">过程</div>${reasoning}${steps?`<div class="chat-steps">${steps}</div>`:''}${thinking}</div>`:''}${answer?`<div class="codex-answer chat-turn-card${item.live?' live-card':''}${item.live&&item.streamText?' streaming':''}"><div class="codex-section-label">回答</div>${answer}</div>`:''}</div>`;
}
function renderAgentMessages(){
  const root=$('#agent-message-list');
  const turns=projectAgentTurns(state.agentChat.events);
  const stream=state.agentChat.stream;
  if(state.agentChat.sending){
    let last=turns.at(-1);
    if(!last||last.kind==='user'){
      last={kind:'turn',provider:stream?.provider||'',model:stream?.model||'',texts:[],steps:[],final:null,reasoning:'',live:true};
      turns.push(last);
    }else{
      last.live=true;
      if(stream?.provider)last.provider=stream.provider;
      if(stream?.model)last.model=stream.model;
    }
    last.streamText=visibleAssistantText(stream?.text||'');
    last.streamReasoning=stream?.reasoning||'';
    last.status=liveAgentStatus();
  }
  const html=turns.length?groupCodexBlocks(turns).map(block=>{
    const prompt=block.user?renderCodexPrompt(block.user,block.userIndex):'';
    const run=block.run?renderCodexRun(block.run,block.runIndex):'';
    return`<section class="codex-block">${prompt}${run}</section>`;
  }).join(''):'';
  root.innerHTML=html?`${html}<div id="agent-scroll-anchor" aria-hidden="true"></div>`:`<div class="chat-empty"><div class="chat-empty-mark">A</div><b>给 Agent 一个任务</b><span>描述你想查的数据、分析目标或要执行的操作。它会先思考、调用工具，再给出结果；完成后可以继续追问。</span></div>`;
  const status=$('#agent-live-status');
  if(status){
    status.textContent=state.agentChat.sending?liveAgentStatus():(turns.length?'同一会话可继续追问':'等待任务');
    status.classList.toggle('is-live',state.agentChat.sending);
  }
  $$('[data-chat-approve]',root).forEach(button=>button.addEventListener('click',()=>decideRuntimeApproval(button.dataset.chatApprove,true)));
  $$('[data-chat-reject]',root).forEach(button=>button.addEventListener('click',()=>decideRuntimeApproval(button.dataset.chatReject,false)));
  $$('[data-view-result]',root).forEach(button=>button.addEventListener('click',()=>openResultViewer(button.dataset.viewResult)));
  $$('a.workspace-file-link',root).forEach(link=>link.addEventListener('click',event=>{
    event.preventDefault();
    downloadWorkspaceFile(link.dataset.workspacePath||'',link);
  }));
  bindAgentTranscriptScroll();
  watchAgentTranscriptSize();
  scrollAgentTranscript();
}
function renderAgentSessionList(){const root=$('#agent-session-list');const sessions=state.agentChat.sessions;root.innerHTML=sessions.length?sessions.map((item,index)=>{const isFirst=index===0;const isLast=index===sessions.length-1;return`<div class="agent-session-row ${item.id===state.agentChat.sessionId?'active':''}"><button type="button" class="agent-session-button" data-agent-session="${escapeHTML(item.id)}"><b>${escapeHTML(item.title||'未命名会话')}</b><span>${formatTime(item.updatedAt)}</span></button><div class="agent-session-menu"><button type="button" class="agent-session-menu-trigger" data-session-menu="${escapeHTML(item.id)}" aria-label="更多操作" title="更多操作">⋯</button><div class="agent-session-menu-panel" hidden><button type="button" class="agent-session-menu-item" data-session-move-up="${escapeHTML(item.id)}" ${isFirst?'disabled':''}>上移</button><button type="button" class="agent-session-menu-item" data-session-move-down="${escapeHTML(item.id)}" ${isLast?'disabled':''}>下移</button><button type="button" class="agent-session-menu-item danger" data-delete-session="${escapeHTML(item.id)}">删除</button></div></div></div>`}).join(''):`<div class="empty compact">还没有会话</div>`;$$('[data-agent-session]',root).forEach(button=>button.addEventListener('click',()=>{closeAgentSessionMenu();selectAgentSession(button.dataset.agentSession)}));$$('[data-session-menu]',root).forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();toggleAgentSessionMenu(button.dataset.sessionMenu,button)}));$$('[data-session-move-up]',root).forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();closeAgentSessionMenu();moveAgentSession(button.dataset.sessionMoveUp,-1)}));$$('[data-session-move-down]',root).forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();closeAgentSessionMenu();moveAgentSession(button.dataset.sessionMoveDown,1)}));$$('[data-delete-session]',root).forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();closeAgentSessionMenu();deleteAgentSession(button.dataset.deleteSession)}));updateAgentSessionActions()}
function renderAgentSubagents(){const root=$('#agent-subagent-list');root.innerHTML=state.agentChat.subagents.length?state.agentChat.subagents.slice(0,8).map(item=>`<div class="subagent-task"><b>${escapeHTML(item.objective)}</b><span class="status-chip ${item.status==='completed'?'completed':['failed','timed_out','cancelled'].includes(item.status)?'rejected':'waiting_approval'}">${escapeHTML(statusLabels[item.status]||item.status)}</span><small>${escapeHTML(item.child_session_id.slice(0,8))} · depth ${item.depth}</small>${['queued','running','cancel_requested'].includes(item.status)?`<button class="text-button" data-cancel-subagent="${escapeHTML(item.task_id)}">取消</button>`:''}</div>`).join(''):`<div class="empty compact">暂无后台任务</div>`;$$('[data-cancel-subagent]',root).forEach(button=>button.addEventListener('click',()=>cancelSubagent(button.dataset.cancelSubagent)))}
async function loadAgentSubagents(){const suffix=state.agentChat.sessionId?`?parent_session_id=${encodeURIComponent(state.agentChat.sessionId)}`:'';const data=await api(`/v1/agent/subagents${suffix}`);state.agentChat.subagents=data.items;renderAgentSubagents()}
async function cancelSubagent(taskId){try{await api(`/v1/agent/subagents/${encodeURIComponent(taskId)}/cancel`,{method:'POST'});toast('已请求取消 Subagent');await loadAgentSubagents()}catch(error){toast(error.message)}}
async function refreshAgentEvents(){if(!state.agentChat.sessionId){state.agentChat.events=[];return}const data=await api(`/v1/agent/sessions/${encodeURIComponent(state.agentChat.sessionId)}/events`);state.agentChat.events=data.items}
async function selectAgentSession(id){state.agentChat.sessionId=id;localStorage.setItem(`${agentStorageKey()}.current`,id);state.agentChat.motionKeys=new Set();state.agentChat.stickToBottom=true;try{await Promise.all([refreshAgentEvents(),loadAgentSubagents()]);setAgentSessionLabel(id);renderAgentSessionList();renderAgentMessages();await maybeResumeAgentTurn()}catch(error){toast(error.message)}}
function startNewAgentSession(){state.agentChat.sessionId=null;localStorage.removeItem(`${agentStorageKey()}.current`);state.agentChat.events=[];state.agentChat.subagents=[];state.agentChat.pendingAttachments=[];state.agentChat.stream=null;state.agentChat.interrupting=false;state.agentChat.motionKeys=new Set();state.agentChat.stickToBottom=true;setAgentSessionLabel(null);renderAgentSessionList();renderAgentMessages();renderAgentSubagents();renderAgentAttachmentPreview();updateAgentSendButton()}
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
function isInterruptedAgentTurn(events){
  for(let index=events.length-1;index>=0;index-=1){
    const type=events[index].event_type;
    if(type==='turn.completed'||type==='user.message')return false;
    if(type==='turn.interrupted')return true;
  }
  return false;
}
async function loadAgentChat(){
  await restoreAgentSessions();
  if(!state.agentChat.sessionId)state.agentChat.sessionId=localStorage.getItem(`${agentStorageKey()}.current`);
  const configurationPromise=state.configuration?Promise.resolve(state.configuration):api('/v1/configuration');
  const [,configuration]=await Promise.all([refreshAgentModelSelect(),configurationPromise]);
  state.configuration=configuration;
  const specialist=configuration.analyst_runtime?.mode==='specialized_parallel';
  $('#agent-runtime-mode').textContent=specialist?`专业并行 · 最多 ${configuration.analyst_runtime?.max_parallel||3}`:'通用 Analyst · 单任务';
  if(state.agentChat.sessionId){
    try{await refreshAgentEvents()}
    catch{startNewAgentSession()}
  }
  renderAgentSessionList();
  renderAgentMessages();
  setAgentSessionLabel(state.agentChat.sessionId);
  await loadAgentSubagents();
  updateAgentSendButton();
  await maybeResumeAgentTurn();
}
let agentCooldownTimer=null;
function updateAgentSendButton(){
  const button=$('#agent-send-button');
  const interrupt=$('#agent-interrupt-button');
  const resume=$('#agent-resume-button');
  const question=$('#agent-question');
  const modelSelect=$('#agent-model-select');
  const noModel=!modelSelect||modelSelect.disabled||!modelSelect.value;
  const unsupportedImages=state.agentChat.pendingAttachments.length>0&&!selectedAgentModel()?.supports_image;
  const remaining=Math.max(0,Math.ceil((state.agentChat.cooldownUntil-Date.now())/1000));
  const interrupted=isInterruptedAgentTurn(state.agentChat.events);
  const hasThread=state.agentChat.events.some(item=>item.event_type==='user.message');
  button.disabled=remaining>0||noModel||unsupportedImages;
  button.hidden=state.agentChat.sending||interrupted;
  button.textContent=noModel?'未配置模型':remaining>0?`请等待 ${remaining}s`:(hasThread?'继续':'运行');
  interrupt.hidden=!state.agentChat.sending||!state.agentChat.sessionId;
  interrupt.disabled=state.agentChat.interrupting;
  interrupt.textContent=state.agentChat.interrupting?'停止中…':'停止';
  resume.hidden=state.agentChat.sending||!interrupted;
  if(question){
    question.disabled=state.agentChat.sending;
    question.placeholder=hasThread?'继续追问，或补充约束。Enter 运行，Shift+Enter 换行':'描述一个任务，Enter 运行，Shift+Enter 换行';
  }
  updateAgentAttachmentCapability();
  updateAgentSessionActions();
  if(!remaining&&agentCooldownTimer){clearInterval(agentCooldownTimer);agentCooldownTimer=null}
}
function autosizeAgentQuestion(){
  const el=$('#agent-question');
  if(!el)return;
  el.style.height='auto';
  el.style.height=`${Math.min(180,Math.max(56,el.scrollHeight))}px`;
}
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
  state.agentChat.interrupting=false;
  updateAgentSendButton();
  if(state.agentChat.sessionId){
    try{
      await Promise.all([refreshAgentEvents(),loadAgentSubagents()]);
      renderAgentSessionList();
      renderAgentMessages();
      updateAgentSendButton();
    }catch{renderAgentMessages()}
  }
}
async function maybeResumeAgentTurn(force=false){
  if(!state.agentChat.sessionId||state.agentChat.sending)return;
  if(!isOpenAgentTurn(state.agentChat.events))return;
  if(isInterruptedAgentTurn(state.agentChat.events)&&!force)return;
  const question=lastUserQuestion(state.agentChat.events);
  state.agentChat.sending=true;
  updateAgentSendButton();
  lockAgentMotionKeys();
  state.agentChat.stickToBottom=true;
  reasoningStickToBottom=true;
  state.agentChat.stream={provider:'',model:'',text:'',reasoning:''};
  renderAgentMessages();
  try{
    const response=await authFetch('/v1/agent/query/resume',{method:'POST',body:JSON.stringify({session_id:state.agentChat.sessionId})});
    await consumeAgentSse(response,question);
  }catch(error){
    await finishAgentStream(error);
    return;
  }
  await finishAgentStream();
}
async function interruptAgentTurn(){
  if(!state.agentChat.sessionId||!state.agentChat.sending||state.agentChat.interrupting)return;
  state.agentChat.interrupting=true;
  updateAgentSendButton();
  try{
    await api(`/v1/agent/sessions/${encodeURIComponent(state.agentChat.sessionId)}/interrupt`,{method:'POST'});
    state.agentChat.events=[...state.agentChat.events,{event_type:'turn.interrupt_requested',payload:{status:'interrupt_requested'},created_at:new Date().toISOString()}];
    renderAgentMessages();
    toast('已请求中断，正在保存恢复检查点');
  }catch(error){state.agentChat.interrupting=false;updateAgentSendButton();toast(error.message,'error')}
}
async function resumeInterruptedTurn(){await maybeResumeAgentTurn(true)}
async function sendAgentMessage(event){event.preventDefault();if(state.agentChat.sending)return;if(isInterruptedAgentTurn(state.agentChat.events)){toast('请先继续执行当前任务，或新建会话','error');return}if(Date.now()<state.agentChat.cooldownUntil){updateAgentSendButton();return}const question=$('#agent-question').value.trim();if(question.length<2){toast('请输入至少 2 个字符');return}state.agentChat.sending=true;updateAgentSendButton();const modelId=$('#agent-model-select')?.value||state.agentChat.selectedModelId||undefined;const payload={question,session_id:state.agentChat.sessionId||undefined,model_id:modelId,attachment_ids:state.agentChat.pendingAttachments.map(item=>item.reference.attachment_id)};lockAgentMotionKeys();state.agentChat.stickToBottom=true;reasoningStickToBottom=true;state.agentChat.events=[...state.agentChat.events,{event_type:'user.message',created_at:new Date().toISOString(),payload:{content:question,attachments:state.agentChat.pendingAttachments.map(item=>item.reference)}}];state.agentChat.stream={provider:'',model:'',text:'',reasoning:''};$('#agent-question').value='';autosizeAgentQuestion();state.agentChat.pendingAttachments=[];renderAgentAttachmentPreview();renderAgentMessages();try{const response=await authFetch('/v1/agent/query/stream',{method:'POST',body:JSON.stringify(payload)});await consumeAgentSse(response,question);await finishAgentStream()}catch(error){await finishAgentStream(error)}}
function parseSseBlocks(buffer){const parts=buffer.split('\n\n');const rest=parts.pop()||'';const events=[];for(const block of parts){let name='message';const dataLines=[];for(const line of block.split('\n')){if(line.startsWith('event:'))name=line.slice(6).trim();else if(line.startsWith('data:'))dataLines.push(line.slice(5).trimStart())}if(!dataLines.length)continue;try{events.push({name,data:JSON.parse(dataLines.join('\n'))})}catch{}}return{events,rest}}
function handleAgentStreamEvent(name,data,question){
  if(name==='session'&&data.session_id){
    state.agentChat.sessionId=data.session_id;
    rememberAgentSession(data.session_id,question);
    setAgentSessionLabel(data.session_id);
    renderAgentSessionList();
    updateAgentSendButton();
    return;
  }
  if(name==='token'||name==='reasoning'){
    if(!state.agentChat.stream)state.agentChat.stream={provider:'',model:'',text:'',reasoning:''};
    state.agentChat.stream.provider=data.provider||state.agentChat.stream.provider;
    state.agentChat.stream.model=data.model||state.agentChat.stream.model;
    if(name==='reasoning'){
      state.agentChat.stream.reasoning=(state.agentChat.stream.reasoning||'')+(data.text||'');
      const el=$('#agent-stream-reasoning');
      if(el){
        reasoningProgrammaticScroll=true;
        el.textContent=state.agentChat.stream.reasoning;
        const box=el.closest('.chat-reasoning');
        if(box){box.open=true;box.classList.add('live')}
        const thinking=$('#agent-thinking');
        if(thinking)thinking.remove();
        const meta=el.closest('.codex-run')?.querySelector('.chat-meta');
        if(meta)meta.textContent=`${modelLabel(state.agentChat.stream.provider,state.agentChat.stream.model)} · ${liveAgentStatus()}`;
        watchAgentTranscriptSize();
        scrollAgentTranscript();
      }else renderAgentMessages();
      return;
    }
    state.agentChat.stream.text+=data.text||'';
    const el=$('#agent-stream-text');
    if(el){
      el.innerHTML=renderMarkdown(visibleAssistantText(state.agentChat.stream.text)||'正在思考…');
      const meta=el.closest('.codex-run')?.querySelector('.chat-meta');
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
    toast(data.status==='waiting_approval'?'工具调用正在等待逐次审批':data.status==='interrupted'?'执行已中断，可继续恢复':`完成 · ${data.provider}/${data.model}`);
    return;
  }
  if(data.type==='user.message')return;
  if(data.payload&&data.type&&data.type!=='token'&&data.type!=='session'){
    if(data.type==='model.response'){
      state.agentChat.stream=null;
    }
    const last=state.agentChat.events.at(-1);
    if(!(last&&last.event_type===data.type&&JSON.stringify(last.payload||{})===JSON.stringify(data.payload||{}))){
      state.agentChat.events=[...state.agentChat.events,{event_type:data.type,payload:data.payload,created_at:data.created_at}];
      renderAgentMessages();
      updateAgentSendButton();
    }
  }
}
const memoryScopeLabels={user:'用户',profile:'用户画像',tenant:'组织',agent:'Agent'};
function renderMemoryItems(items){
  const counts=items.reduce((result,item)=>{result[item.status]=(result[item.status]||0)+1;return result},{});
  $('#memory-metrics').innerHTML=[['记忆总数',items.length],['已生效',counts.active||0],['待确认',(counts.candidate||0)+(counts.conflicted||0)],['已删除/替代',(counts.deleted||0)+(counts.superseded||0)]].map(([label,value])=>`<article class="metric-card"><span>${label}</span><strong>${value}</strong></article>`).join('');
  $('#memory-list').innerHTML=items.map(item=>{const review=['candidate','conflicted'].includes(item.status);const owner=item.user_id||item.agent_id||state.session.tenant;return`<article class="panel memory-card"><div class="memory-card-head"><div><span class="status-chip ${escapeHTML(item.status)}">${escapeHTML(statusLabels[item.status]||item.status)}</span><span class="access-chip">${escapeHTML(memoryScopeLabels[item.scope]||item.scope)}</span><h3>${escapeHTML(item.key)}</h3></div><span class="mono">${formatTime(item.updated_at)}</span></div><p class="memory-content">${escapeHTML(item.content)}</p><div class="memory-meta"><span>所有者：${escapeHTML(owner)}</span><span>重要度 ${Number(item.importance||0).toFixed(2)}</span><span>质量 ${Number(item.quality_score||0).toFixed(2)}</span><span>${item.expires_at?`过期 ${formatTime(item.expires_at)}`:'永久'}</span></div><div class="memory-actions">${review?`<button class="primary small" data-memory-confirm="${escapeHTML(item.id)}">确认生效</button><button class="secondary" data-memory-reject="${escapeHTML(item.id)}">拒绝</button>`:''}${item.status!=='deleted'?`<button class="secondary" data-memory-correct="${escapeHTML(item.id)}">纠正</button><button class="danger-button" data-memory-delete="${escapeHTML(item.id)}">删除</button>`:''}${item.user_id?`<button class="text-button access-danger" data-memory-compliance="${escapeHTML(item.user_id)}">合规删除该用户全部记忆</button>`:''}</div></article>`}).join('')||'<article class="panel empty"><b>没有匹配的记忆</b><span>可添加记忆，或等待系统产生候选记忆。</span></article>';
  $$('[data-memory-confirm]').forEach(button=>button.addEventListener('click',()=>memoryAction(`/v1/memories/${encodeURIComponent(button.dataset.memoryConfirm)}/confirm`,{method:'POST',body:JSON.stringify({replace_conflicts:true})},'记忆已确认')));
  $$('[data-memory-reject]').forEach(button=>button.addEventListener('click',()=>memoryAction(`/v1/memories/${encodeURIComponent(button.dataset.memoryReject)}/reject`,{method:'POST'},'候选已拒绝')));
  $$('[data-memory-correct]').forEach(button=>button.addEventListener('click',async()=>{const current=state.memories.find(item=>item.id===button.dataset.memoryCorrect);const content=window.prompt('输入纠正后的记忆内容',current?.content||'');if(content?.trim())await memoryAction(`/v1/memories/${encodeURIComponent(button.dataset.memoryCorrect)}/correct`,{method:'POST',body:JSON.stringify({content:content.trim()})},'记忆已纠正')}));
  $$('[data-memory-delete]').forEach(button=>button.addEventListener('click',async()=>{if(window.confirm('确定删除并擦除这条记忆内容？'))await memoryAction(`/v1/memories/${encodeURIComponent(button.dataset.memoryDelete)}`,{method:'DELETE',body:JSON.stringify({reason:'admin_requested'})},'记忆已删除')}));
  $$('[data-memory-compliance]').forEach(button=>button.addEventListener('click',async()=>{if(window.confirm(`将不可逆擦除用户 ${button.dataset.memoryCompliance} 的全部记忆内容，是否继续？`))await memoryAction(`/v1/memories/users/${encodeURIComponent(button.dataset.memoryCompliance)}/compliance`,{method:'DELETE'},'用户记忆已合规删除')}));
}
async function loadMemory(){
  const query=$('#memory-filter-query')?.value.trim()||'';
  const owner=$('#memory-filter-user')?.value.trim()||'';
  if(query){const params=new URLSearchParams({query,limit:'50'});if(owner)params.set('owner_user_id',owner);const result=await api(`/v1/memories/search?${params}`);state.memories=result.items;renderMemoryItems(state.memories);return}
  const params=new URLSearchParams({include_deleted:'true'});const status=$('#memory-filter-status')?.value||'';const scope=$('#memory-filter-scope')?.value||'';if(status)params.set('status',status);if(scope)params.set('scope',scope);if(owner)params.set('owner_user_id',owner);const result=await api(`/v1/memories?${params}`);state.memories=result.items;renderMemoryItems(state.memories);
}
async function memoryAction(path,options,message){try{await api(path,options);toast(message,'success');await loadMemory()}catch(error){toast(error.message,'error')}}
function updateMemoryScopeFields(){const scope=$('#memory-scope').value;$('#memory-agent-field').hidden=scope!=='agent';$('#memory-owner-field').hidden=!['user','profile'].includes(scope)}
function openMemoryEditor(){$('#memory-editor-form').reset();$('#memory-importance').value='0.5';updateMemoryScopeFields();$('#memory-editor-drawer').classList.add('open');window.setTimeout(()=>$('#memory-content').focus(),0)}
function closeMemoryEditor(){$('#memory-editor-drawer').classList.remove('open')}
async function saveMemory(event){event.preventDefault();const scope=$('#memory-scope').value;const expiry=$('#memory-expiry').value;const payload={content:$('#memory-content').value.trim(),key:$('#memory-key').value.trim(),scope,kind:$('#memory-kind').value,owner_user_id:$('#memory-owner-user').value.trim()||null,agent_id:scope==='agent'?$('#memory-agent-id').value.trim()||null:null,importance:Number($('#memory-importance').value||0.5),expires_in_days:expiry?Number(expiry):null};try{await api('/v1/memories',{method:'POST',body:JSON.stringify(payload)});closeMemoryEditor();toast('记忆已保存','success');await loadMemory()}catch(error){toast(error.message,'error')}}

async function loadAccess(){
  state.access=await api('/v1/access-control');
  const access=state.access;
  const groupOptions=(access.groups||[]).map(group=>`<option value="${escapeHTML(group.id)}">${escapeHTML(group.name)}</option>`).join('');
  const groupMap=new Map((access.groups||[]).map(item=>[item.id,item]));
  $('#access-user-grid').innerHTML=(access.users||[]).map(user=>`<article class="agent-card"><button class="text-button access-danger" data-delete-access-user="${escapeHTML(user.id)}">删除</button><h3>${escapeHTML(user.name)}</h3><p class="mono">${escapeHTML(user.id)}</p><div><span class="status-chip ${user.enabled?'completed':'disabled'}">${user.enabled?'已启用':'已停用'}</span>${user.account?` <span class="status-chip">${escapeHTML(user.account.role)}</span>${user.account.must_change_password?' <span class="status-chip waiting_approval">待首次改密</span>':''}`:' <span class="status-chip disabled">无登录账户</span>'}</div>${user.account?`<button class="text-button" data-reset-password="${escapeHTML(user.id)}">重置临时密码</button>`:''}<div>${(user.group_ids||[]).map(id=>`<span class="access-chip">${escapeHTML(groupMap.get(id)?.name||id)}<button data-unbind-user="${escapeHTML(user.id)}" data-group-id="${escapeHTML(id)}">×</button></span>`).join('')||'<p>尚未加入权限组</p>'}</div><div class="access-card-select"><label>加入权限组<select data-user-group-select="${escapeHTML(user.id)}"><option value="">选择权限组</option>${groupOptions}</select></label><button class="secondary" data-bind-user="${escapeHTML(user.id)}">绑定</button></div></article>`).join('')||'<article class="panel empty"><b>尚未启用 RBAC</b><span>创建第一个用户后，未录入用户将无法调用 Tool。</span></article>';
  $('#access-group-grid').innerHTML=(access.groups||[]).map(group=>{const selected=new Set(group.tool_names||[]);const toolOptions=(access.tool_catalog||[]).map(tool=>`<label class="tool-multiselect-option"><input type="checkbox" value="${escapeHTML(tool.id)}" data-group-tool-option="${escapeHTML(group.id)}" ${selected.has(tool.id)?'checked':''}><span>${escapeHTML(tool.name||tool.id)}</span></label>`).join('');return `<article class="agent-card"><div class="access-card-actions"><button class="text-button" data-edit-access-group="${escapeHTML(group.id)}">编辑</button><button class="text-button access-danger" data-delete-access-group="${escapeHTML(group.id)}">删除</button></div><h3>${escapeHTML(group.name)}</h3><p>${escapeHTML(group.description||'未填写说明')}</p><div class="access-relation-title">已授权 ${selected.size} 个业务 Tool</div><div class="tool-multiselect" data-tool-multiselect="${escapeHTML(group.id)}"><span class="tool-multiselect-label">可使用的 Tool</span><button type="button" class="tool-multiselect-trigger" data-group-tools-trigger="${escapeHTML(group.id)}" aria-expanded="false"><span data-group-tools-summary="${escapeHTML(group.id)}">${selected.size?`已选 ${selected.size} 个 Tool`:'请选择 Tool'}</span><span aria-hidden="true">⌄</span></button><div class="tool-multiselect-menu" data-group-tools-menu="${escapeHTML(group.id)}" hidden>${toolOptions||'<p class="tool-multiselect-empty">暂无可配置的 Tool</p>'}</div></div><p class="form-hint">可多选；同一 Tool 可在多个权限组中授权。</p><button class="primary" data-save-group-tools="${escapeHTML(group.id)}">保存 Tool 权限</button></article>`}).join('')||'<article class="panel empty"><b>没有权限组</b></article>';
  $('#access-rule-grid').innerHTML=(access.rules||[]).map(rule=>`<article class="agent-card"><h3>${escapeHTML(rule.name)}</h3><p>${escapeHTML(rule.description||'允许列出的 Tool')}</p><div>${(rule.tool_names||[]).map(name=>`<span class="access-chip">${escapeHTML(name)}</span>`).join('')||'<p>该规则不包含业务 Tool</p>'}</div><p class="form-hint">只读明细；Tool 授权请在上方权限组中配置。</p></article>`).join('')||'<article class="panel empty"><b>没有权限规则</b></article>';
  $$('[data-bind-user]').forEach(button=>button.addEventListener('click',()=>bindAccess(`/v1/access-control/users/${encodeURIComponent(button.dataset.bindUser)}/groups`,$(`[data-user-group-select="${CSS.escape(button.dataset.bindUser)}"]`).value)));
  $$('[data-save-group-tools]').forEach(button=>button.addEventListener('click',()=>saveAccessGroupTools(button.dataset.saveGroupTools)));
  $$('[data-group-tools-trigger]').forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();const groupId=button.dataset.groupToolsTrigger;const menu=$(`[data-group-tools-menu="${CSS.escape(groupId)}"]`);const opening=menu.hidden;closeGroupToolMenus();menu.hidden=!opening;button.setAttribute('aria-expanded',String(opening))}));
  $$('[data-group-tool-option]').forEach(input=>input.addEventListener('change',()=>updateGroupToolsSummary(input.dataset.groupToolOption)));
  $$('[data-unbind-user]').forEach(button=>button.addEventListener('click',()=>removeAccess(`/v1/access-control/users/${encodeURIComponent(button.dataset.unbindUser)}/groups/${encodeURIComponent(button.dataset.groupId)}`)));
  $$('[data-delete-access-user]').forEach(button=>button.addEventListener('click',()=>removeAccess(`/v1/access-control/users/${encodeURIComponent(button.dataset.deleteAccessUser)}`,true)));
  $$('[data-reset-password]').forEach(button=>button.addEventListener('click',()=>resetAccessPassword(button.dataset.resetPassword)));
  $$('[data-delete-access-group]').forEach(button=>button.addEventListener('click',()=>removeAccess(`/v1/access-control/groups/${encodeURIComponent(button.dataset.deleteAccessGroup)}`,true,'删除权限组会移除用户绑定和该组的 Tool 授权，确认继续？')));
  $$('[data-edit-access-group]').forEach(button=>button.addEventListener('click',()=>openAccessGroupEditor(button.dataset.editAccessGroup)));
}
async function bindAccess(path,targetId){if(!targetId){toast('请先选择绑定对象','error');return}try{await api(path,{method:'PUT',body:JSON.stringify({target_id:targetId})});toast('权限关系已更新','success');await loadAccess()}catch(error){toast(error.message,'error')}}
function updateGroupToolsSummary(groupId){const selected=$$(`[data-group-tool-option="${CSS.escape(groupId)}"]:checked`);const summary=$(`[data-group-tools-summary="${CSS.escape(groupId)}"]`);if(summary)summary.textContent=selected.length?`已选 ${selected.length} 个 Tool`:'请选择 Tool'}
async function saveAccessGroupTools(groupId){const tool_names=$$(`[data-group-tool-option="${CSS.escape(groupId)}"]:checked`).map(item=>item.value);try{await api(`/v1/access-control/groups/${encodeURIComponent(groupId)}/tools`,{method:'PUT',body:JSON.stringify({tool_names})});toast('Tool 权限已保存','success');await loadAccess()}catch(error){toast(error.message,'error')}}
function closeGroupToolMenus(){$$('[data-group-tools-menu]').forEach(menu=>{menu.hidden=true});$$('[data-group-tools-trigger]').forEach(button=>button.setAttribute('aria-expanded','false'))}
async function removeAccess(path,confirmDelete=false,confirmMessage='确认删除？关联绑定会一并移除。'){if(confirmDelete&&!window.confirm(confirmMessage))return;try{await api(path,{method:'DELETE'});toast('已移除','success');await loadAccess()}catch(error){toast(error.message,'error')}}
const accessEditorMeta={
  user:{title:'添加用户',description:'创建一个可加入权限组的用户身份。'},
  group:{title:'添加权限组',description:'创建权限集合，随后可绑定用户并直接配置多个 Tool 权限。'},
};
let editingAccessGroupId=null;
function openAccessEditor(kind){
  const meta=accessEditorMeta[kind];
  if(!meta)return;
  $('#access-editor-title').textContent=meta.title;
  $('#access-editor-description').textContent=meta.description;
  if(kind==='group'){
    editingAccessGroupId=null;
    $('#access-group-form').reset();
    $('#access-group-submit').textContent='创建权限组';
  }
  $$('[data-access-form]').forEach(form=>{form.hidden=form.dataset.accessForm!==kind});
  $('#access-editor-drawer').classList.add('open');
  const input=$(`[data-access-form="${kind}"] input, [data-access-form="${kind}"] select`);
  window.setTimeout(()=>input?.focus(),0);
}
function openAccessGroupEditor(groupId){const group=(state.access?.groups||[]).find(item=>item.id===groupId);if(!group)return;editingAccessGroupId=groupId;$('#access-editor-title').textContent='编辑权限组';$('#access-editor-description').textContent='修改权限组名称和说明，现有用户绑定和 Tool 权限保持不变。';$$('[data-access-form]').forEach(form=>{form.hidden=form.dataset.accessForm!=='group'});$('#access-group-name').value=group.name||'';$('#access-group-description').value=group.description||'';$('#access-group-submit').textContent='保存修改';$('#access-editor-drawer').classList.add('open');window.setTimeout(()=>$('#access-group-name').focus(),0)}
function closeAccessEditor(){editingAccessGroupId=null;$('#access-editor-drawer').classList.remove('open')}
function revealTemporaryPassword(password){if(!password)return;window.prompt('临时密码只显示这一次，请立即复制并安全发送给用户：',password)}
async function resetAccessPassword(id){if(!window.confirm(`为 ${id} 生成新的临时密码？该用户的现有会话会立即失效。`))return;try{const result=await api(`/v1/access-control/users/${encodeURIComponent(id)}/reset-password`,{method:'POST',body:JSON.stringify({generate_temporary_password:true})});revealTemporaryPassword(result.temporary_password);toast('临时密码已重置','success');await loadAccess()}catch(error){toast(error.message,'error')}}
async function saveAccessUser(event){event.preventDefault();const id=$('#access-user-id').value.trim();const generate=$('#access-user-generate-password').checked;const temporary=$('#access-user-password').value;try{const result=await api(`/v1/access-control/users/${encodeURIComponent(id)}`,{method:'PUT',body:JSON.stringify({id,name:$('#access-user-name').value.trim(),enabled:$('#access-user-enabled').checked,role:$('#access-user-role').value,temporary_password:generate?null:temporary,generate_temporary_password:generate})});event.target.reset();$('#access-user-enabled').checked=true;closeAccessEditor();revealTemporaryPassword(result.temporary_password);toast('账户已创建，首次登录必须修改密码','success');await loadAccess()}catch(error){toast(error.message,'error')}}
async function saveAccessGroup(event){event.preventDefault();const editing=editingAccessGroupId;const path=editing?`/v1/access-control/groups/${encodeURIComponent(editing)}`:'/v1/access-control/groups';try{await api(path,{method:editing?'PUT':'POST',body:JSON.stringify({name:$('#access-group-name').value.trim(),description:$('#access-group-description').value.trim()})});event.target.reset();closeAccessEditor();toast(editing?'权限组已更新':'权限组已创建','success');await loadAccess()}catch(error){toast(error.message,'error')}}
function saveAccountSession(result){
  const identityChanged=state.session.tenant!==result.account.tenant_id||state.session.userId!==result.account.user_id;
  state.session.accessToken=result.access_token;state.session.refreshToken=result.refresh_token;state.session.account=result.account;
  state.session.tenant=result.account.tenant_id;state.session.userId=result.account.user_id;state.session.role=result.account.role;
  if(identityChanged){state.agentChat.sessionId=null;state.agentChat.sessions=[];state.agentChat.events=[];state.agentChat.subagents=[];state.agentChat.stream=null}
  localStorage.setItem('arkflow.accessToken',result.access_token);localStorage.setItem('arkflow.refreshToken',result.refresh_token);
  localStorage.setItem('arkflow.tenant',state.session.tenant);localStorage.setItem('arkflow.userId',state.session.userId);localStorage.setItem('arkflow.role',state.session.role);
  renderCurrentAccount();
}
function clearAccountSession(){state.session.accessToken='';state.session.refreshToken='';state.session.account=null;state.agentChat.sessionId=null;state.agentChat.sessions=[];state.agentChat.events=[];state.agentChat.subagents=[];state.agentChat.stream=null;localStorage.removeItem('arkflow.accessToken');localStorage.removeItem('arkflow.refreshToken')}
function roleCanAccessPage(page){const role=state.session.role;const restricted={dashboard:['admin'],approvals:['admin','approver'],connectors:['admin'],knowledge:['admin'],memory:['admin'],access:['admin'],audit:['admin'],settings:['admin']};return !restricted[page]||restricted[page].includes(role)}
function applyRoleVisibility(){const role=state.session.role;$$('[data-role-allow]').forEach(element=>{element.hidden=!String(element.dataset.roleAllow||'').split(/\s+/).includes(role)});$$('[data-go="connectors"]').forEach(element=>{element.hidden=role!=='admin'})}
function renderCurrentAccount(){applyRoleVisibility();const account=state.session.account;if(!account)return;$('#account-name').textContent=account.display_name||account.user_id;$('#account-meta').textContent=`${account.tenant_id} · ${String(account.role).toUpperCase()}`;$('#account-avatar').textContent=String(account.display_name||account.user_id).slice(0,2).toUpperCase()}
function setAuthRestoring(on){document.documentElement.classList.toggle('auth-restoring',!!on)}
function showAuthPane(kind){setAuthRestoring(false);$('#auth-gate').classList.add('open');$('#auth-login-pane').hidden=kind!=='login';$('#auth-register-pane').hidden=kind!=='register';$('#auth-password-pane').hidden=kind!=='password';$('#auth-error').textContent=''}
function authError(error){$('#auth-error').textContent=error.message||'操作失败，请重试'}
async function enterApplication(){$('#auth-gate').classList.remove('open');setAuthRestoring(false);renderCurrentAccount();await navigate(pageFromLocation(),{replace:true})}
async function submitLogin(event){event.preventDefault();$('#auth-error').textContent='';try{const password=$('#login-password').value;const result=await parseApiResponse(await fetch('/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tenant_id:$('#login-tenant').value.trim(),user_id:$('#login-user').value.trim(),password})}));saveAccountSession(result);if(result.account.must_change_password){$('#change-current-password').value=password;showAuthPane('password')}else await enterApplication()}catch(error){authError(error)}}
async function submitRegister(event){event.preventDefault();$('#auth-error').textContent='';const password=$('#register-password').value;if(password!==$('#register-confirm').value){authError(new Error('两次输入的密码不一致。'));return}try{const result=await parseApiResponse(await fetch('/v1/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tenant_id:$('#register-tenant').value.trim(),user_id:$('#register-user').value.trim(),display_name:$('#register-name').value.trim(),password})}));saveAccountSession(result);await enterApplication()}catch(error){authError(error)}}
async function submitPasswordChange(event){event.preventDefault();$('#auth-error').textContent='';const password=$('#change-new-password').value;if(password!==$('#change-confirm-password').value){authError(new Error('两次输入的新密码不一致。'));return}try{const result=await api('/v1/auth/change-password',{method:'POST',body:JSON.stringify({current_password:$('#change-current-password').value,new_password:password})});saveAccountSession(result);event.target.reset();await enterApplication()}catch(error){authError(error)}}
async function logoutAccount(){const refresh=state.session.refreshToken;try{if(refresh)await fetch('/v1/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refresh_token:refresh})})}finally{clearAccountSession();showAuthPane('login')}}
async function bootstrapAuth(){loadHealth();if(!state.session.accessToken&&!state.session.refreshToken){showAuthPane('login');return}setAuthRestoring(true);try{const account=await api('/v1/auth/me');state.session.account=account;state.session.tenant=account.tenant_id;state.session.userId=account.user_id;state.session.role=account.role;renderCurrentAccount();if(account.must_change_password)showAuthPane('password');else await enterApplication()}catch{clearAccountSession();showAuthPane('login')}}
const loaders={dashboard:loadDashboard,approvals:loadApprovals,'agent-chat':loadAgentChat,agents:loadAgentsPage,tools:loadToolsPage,connectors:loadConnectorsPage,knowledge:loadKnowledgePage,memory:loadMemory,access:loadAccess,audit:loadAudit,settings:loadConfiguration};const pageNames={dashboard:'运行概览',approvals:'审批中心','agent-chat':'任务',agents:'Agents',tools:'工具',connectors:'连接器',knowledge:'知识库',memory:'长期记忆',access:'用户与权限',audit:'审计日志',settings:'系统设置'};
function pageFromLocation(){
  const fromHash=String(location.hash||'').replace(/^#\/?/,'').split(/[/?#]/)[0];
  if(fromHash&&loaders[fromHash])return fromHash;
  const fromQuery=new URLSearchParams(location.search).get('page');
  if(fromQuery&&loaders[fromQuery])return fromQuery;
  return 'dashboard';
}
function syncPageUrl(page,{replace=false}={}){
  const next=`#/${page}`;
  const current=location.hash==='#'?'' : location.hash;
  if(replace||current===next||current===`#${page}`)history.replaceState({page},'',next);
  else history.pushState({page},'',next);
}
async function navigate(page,options={}){
  applyRoleVisibility();
  if(!loaders[page]||!roleCanAccessPage(page))page='agent-chat';
  state.page=page;
  $$('.page').forEach(el=>el.classList.toggle('active',el.id===`page-${page}`));
  $$('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
  $('#page-breadcrumb').textContent=pageNames[page];
  $('.sidebar').classList.remove('open');
  if(!options.skipHistory)syncPageUrl(page,{replace:!!options.replace});
  try{await loaders[page]()}catch(error){toast(error.message)}
}
$('#analyst-mode-form')?.addEventListener('submit',saveAnalystMode);
$('#access-user-form')?.addEventListener('submit',saveAccessUser);
$('#access-group-form')?.addEventListener('submit',saveAccessGroup);
$$('[data-open-access-editor]').forEach(button=>button.addEventListener('click',()=>openAccessEditor(button.dataset.openAccessEditor)));
$$('[data-close-access-editor]').forEach(button=>button.addEventListener('click',closeAccessEditor));
$('#access-editor-drawer')?.addEventListener('click',event=>{if(event.target.id==='access-editor-drawer')closeAccessEditor()});
document.addEventListener('click',event=>{if(!event.target.closest('.tool-multiselect'))closeGroupToolMenus()});
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeGroupToolMenus()});
$('#memory-add-button')?.addEventListener('click',openMemoryEditor);
$('#memory-editor-form')?.addEventListener('submit',saveMemory);
$('#memory-scope')?.addEventListener('change',updateMemoryScopeFields);
$('#memory-search-button')?.addEventListener('click',loadMemory);
$$('[data-close-memory-editor]').forEach(button=>button.addEventListener('click',closeMemoryEditor));
$('#memory-editor-drawer')?.addEventListener('click',event=>{if(event.target.id==='memory-editor-drawer')closeMemoryEditor()});
$('#result-viewer-prev')?.addEventListener('click',()=>moveResultPage(-1));
$('#result-viewer-next')?.addEventListener('click',()=>moveResultPage(1));
$$('[data-close-result-viewer]').forEach(button=>button.addEventListener('click',()=>$('#result-viewer-drawer').classList.remove('open')));
$('#result-viewer-drawer')?.addEventListener('click',event=>{if(event.target.id==='result-viewer-drawer')event.currentTarget.classList.remove('open')});
$('#knowledge-upload-button')?.addEventListener('click',()=>$('#knowledge-upload-drawer').classList.add('open'));
$('#knowledge-upload-form')?.addEventListener('submit',uploadKnowledgeDocument);
$$('[data-close-knowledge-upload]').forEach(button=>button.addEventListener('click',()=>$('#knowledge-upload-drawer').classList.remove('open')));
$('#knowledge-upload-drawer')?.addEventListener('click',event=>{if(event.target.id==='knowledge-upload-drawer')event.currentTarget.classList.remove('open')});
$('#knowledge-category-form')?.addEventListener('submit',event=>createKnowledgeCategory(event).catch(error=>toast(error.message,'error')));
$('#knowledge-search-form')?.addEventListener('submit',event=>searchKnowledgeLibrary(event).catch(error=>toast(error.message,'error')));
$('#knowledge-search-clear')?.addEventListener('click',()=>loadKnowledgeDocuments().catch(error=>toast(error.message,'error')));
$('#knowledge-space-select')?.addEventListener('change',event=>{if(!state.knowledgeLibrary)return;state.knowledgeLibrary.spaceId=event.target.value;state.knowledgeLibrary.categoryId='';if(event.target.value)localStorage.setItem('arkflow.knowledgeSpaceId',event.target.value);loadKnowledgeDocuments().catch(error=>toast(error.message,'error'))});
$('#knowledge-create-space-button')?.addEventListener('click',()=>openKnowledgeSpaceDrawer().catch(error=>toast(error.message,'error')));
$('#knowledge-space-form')?.addEventListener('submit',event=>createKnowledgeSpace(event).catch(error=>toast(error.message,'error')));
$$('[data-close-knowledge-space]').forEach(button=>button.addEventListener('click',closeKnowledgeSpaceDrawer));
$('#knowledge-space-drawer')?.addEventListener('click',event=>{if(event.target.id==='knowledge-space-drawer')closeKnowledgeSpaceDrawer()});
$('#knowledge-reindex-button')?.addEventListener('click',async()=>{const lib=knowledgeLibrary();if(!lib.spaceId)return;if(!window.confirm('将按新 payload 重建当前空间已有切片的向量，可能需要几分钟。继续？'))return;try{const result=await api(`/v1/knowledge/library/spaces/${encodeURIComponent(lib.spaceId)}/reindex`,{method:'POST'});toast(`已排队 ${result.queued||0} 个向量任务`,'success')}catch(error){toast(error.message,'error')}});
$$('[data-close-knowledge-content]').forEach(button=>button.addEventListener('click',()=>$('#knowledge-content-drawer').classList.remove('open')));
$('#knowledge-content-drawer')?.addEventListener('click',event=>{if(event.target.id==='knowledge-content-drawer')event.currentTarget.classList.remove('open')});
function handleAgentQuestionKeydown(event){
  if(event.key!=='Enter'||event.shiftKey||event.altKey||event.ctrlKey||event.metaKey)return;
  if(event.isComposing||event.keyCode===229)return;
  event.preventDefault();
  const form=$('#agent-chat-form');
  if(typeof form.requestSubmit==='function')form.requestSubmit();
  else form.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));
}
$('#agent-question')?.addEventListener('input',autosizeAgentQuestion);
autosizeAgentQuestion();
$$('.nav-item').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.page)));$$('[data-go]').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.go)));$('#context-window-form').addEventListener('submit',saveContextWindow);$('#agent-editor-form').addEventListener('submit',saveAgentEditor);$('#connection-editor-form')?.addEventListener('submit',saveConnectionEditor);$('#connection-add-button')?.addEventListener('click',()=>openConnectionEditor());$('#connection-edit-type')?.addEventListener('change',updateConnectionFields);$('#connection-edit-delete')?.addEventListener('click',deleteConnectionEditor);$$('[data-close-connection-editor]').forEach(button=>button.addEventListener('click',()=>$('#connection-editor-drawer').classList.remove('open')));$('#connection-editor-drawer')?.addEventListener('click',event=>{if(event.target.id==='connection-editor-drawer')event.currentTarget.classList.remove('open')});$('#model-editor-form').addEventListener('submit',saveModelEditor);$('#model-add-button')?.addEventListener('click',()=>openModelEditor());$('#model-edit-delete')?.addEventListener('click',deleteModelEditor);$('#model-edit-provider')?.addEventListener('change',()=>applyModelProviderDefaults());$('#agent-model-select')?.addEventListener('change',onAgentModelChange);$$('[data-close-model-editor]').forEach(button=>button.addEventListener('click',()=>$('#model-editor-drawer').classList.remove('open')));$('#model-editor-drawer')?.addEventListener('click',event=>{if(event.target.id==='model-editor-drawer')event.currentTarget.classList.remove('open')});$$('[data-close-agent-editor]').forEach(button=>button.addEventListener('click',()=>{closeToolMenu();$('#agent-editor-drawer').classList.remove('open')}));$('#agent-editor-drawer').addEventListener('click',event=>{if(event.target.id==='agent-editor-drawer'){closeToolMenu();event.currentTarget.classList.remove('open')}});$('#agent-add-tool-button').addEventListener('click',event=>{event.stopPropagation();const menu=$('#agent-tool-menu');menu.hidden=!menu.hidden;if(!menu.hidden)renderToolMenuOptions()});document.addEventListener('click',event=>{if(!event.target.closest('.tool-picker-add'))closeToolMenu();if(!event.target.closest('.agent-session-menu'))closeAgentSessionMenu()});$('#context-window-form').addEventListener('submit',saveContextWindow);$('#agent-chat-form').addEventListener('submit',sendAgentMessage);$('#agent-question').addEventListener('keydown',handleAgentQuestionKeydown);$('#agent-new-session').addEventListener('click',startNewAgentSession);$('#agent-delete-session').addEventListener('click',()=>deleteAgentSession(state.agentChat.sessionId));$('#agent-attach-image').addEventListener('click',()=>$('#agent-image-input').click());$('#agent-image-input').addEventListener('change',handleAgentImageSelect);$('#agent-interrupt-button').addEventListener('click',interruptAgentTurn);$('#agent-resume-button').addEventListener('click',resumeInterruptedTurn);$('#refresh-button').addEventListener('click',()=>navigate(state.page,{replace:true}));$('#mobile-menu').addEventListener('click',()=>$('.sidebar').classList.toggle('open'));$('#api-key-input').value=state.session.apiKey;$('#tenant-input').value=state.session.tenant;$('#user-id-input').value=state.session.userId;$('#role-input').value=state.session.role;$('#save-session').addEventListener('click',()=>{state.session.apiKey=$('#api-key-input').value;state.session.tenant=$('#tenant-input').value||'tenant-a';state.session.userId=$('#user-id-input').value||'local-admin';state.session.role=$('#role-input').value;localStorage.setItem('arkflow.apiKey',state.session.apiKey);localStorage.setItem('arkflow.tenant',state.session.tenant);localStorage.setItem('arkflow.userId',state.session.userId);localStorage.setItem('arkflow.role',state.session.role);state.agentChat.sessionId=null;state.catalog=null;state.access=null;toast('开发会话已保存');navigate(state.page)});
$('#login-form').addEventListener('submit',submitLogin);$('#register-form').addEventListener('submit',submitRegister);$('#change-password-form').addEventListener('submit',submitPasswordChange);$('#show-register').addEventListener('click',()=>showAuthPane('register'));$('#show-login').addEventListener('click',()=>showAuthPane('login'));$('#logout-button').addEventListener('click',logoutAccount);$('#access-user-generate-password').addEventListener('change',event=>{$('#access-user-password').disabled=event.target.checked});
window.addEventListener('storage',event=>{
  if(event.key==='arkflow.accessToken')state.session.accessToken=event.newValue||'';
  if(event.key==='arkflow.refreshToken'){
    state.session.refreshToken=event.newValue||'';
    if(!event.newValue){state.session.account=null;showAuthPane('login')}
  }
});
window.addEventListener('popstate',()=>{if(!$('#auth-gate').classList.contains('open'))navigate(pageFromLocation(),{skipHistory:true})});bootstrapAuth();
