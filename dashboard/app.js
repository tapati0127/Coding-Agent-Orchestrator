/* No framework, external assets or HTML interpolation from Store data. */
let token = "", selectedProject = null, selectedTask = null, current = null, refreshTimer, eventController;
const $ = (id) => document.getElementById(id);
const el = (tag, text, cls) => { const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node; };
const empty = (node, text) => node.replaceChildren(el("div", text, "empty"));
const showError = (error) => { $("error").textContent = error.message || String(error); $("error").hidden = false; };
async function api(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET", headers: {"Authorization": `Bearer ${token}`, ...(body ? {"Content-Type":"application/json"} : {})}, ...(body ? {body: JSON.stringify(body)} : {})});
  if (!response.ok) { let detail; try { detail = (await response.json()).detail; } catch {} throw Error(typeof detail === "string" ? detail : `HTTP ${response.status}`); }
  return response.json();
}
function receipt(message, result) { $("receipts").prepend(el("p", `${message} · ${result.request_id}`)); }
async function loadProjects() {
  const {projects} = await api("/api/projects");
  $("projects").replaceChildren();
  if (!projects.length) empty($("projects"), "Chưa có dự án. Gửi yêu cầu tạo dự án bên dưới.");
  for (const project of projects) {
    const button = el("button", project.title || project.project_id, `project${selectedProject === project.project_id ? " active" : ""}`);
    button.onclick = () => { selectedProject = project.project_id; selectedTask = null; loadProjects().then(refresh).then(startEvents).catch(showError); };
    $("projects").append(button);
  }
  if (!selectedProject && projects.length) selectedProject = projects[0].project_id;
}
async function startEvents() {
  if (eventController) eventController.abort();
  if (!selectedProject) return;
  const controller = new AbortController(); eventController = controller;
  const streamProject = selectedProject;
  // Fetch streaming permits an Authorization header, unlike EventSource.
  (async () => {
    while (!controller.signal.aborted) {
      try {
        const response = await fetch(`/api/projects/${encodeURIComponent(streamProject)}/events`, {headers:{"Authorization":`Bearer ${token}`},signal:controller.signal});
        if (!response.ok) throw Error(`Event stream HTTP ${response.status}`);
        const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="";
        while (true) {
          const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});
          let split;
          while((split=buffer.indexOf("\n\n"))!==-1){const frame=buffer.slice(0,split);buffer=buffer.slice(split+2);if(!frame.startsWith("data: "))continue;const event=JSON.parse(frame.slice(6));if(current&&selectedProject===streamProject){const key=JSON.stringify(event);if(!current.events.some(e=>JSON.stringify(e)===key)){current.events.push(event);current.events=current.events.slice(-200);renderTimeline();}}}
        }
      } catch(error) { if(controller.signal.aborted)return;$("connection").textContent="Đang kết nối lại luồng sự kiện…"; }
      await new Promise(resolve=>setTimeout(resolve,2000));
    }
  })();
}
async function refresh() {
  if (!selectedProject) return;
  current = await api(`/api/projects/${encodeURIComponent(selectedProject)}/snapshot`);
  $("project-title").textContent = current.project.title || selectedProject;
  $("project-meta").textContent = `${selectedProject} · ${current.project.repository_path || "Repository chưa đăng ký"}`;
  $("error").hidden = true; $("connection").textContent = "Đã kết nối · cập nhật mỗi 2 giây";
  renderStats(); renderDag(); renderWorkers(); renderApprovals(); renderConversation(); renderTimeline(); renderArtifacts(); renderTask();
}
function renderStats() {
  $("stats").replaceChildren();
  for (const [label, count] of [["TASKS", current.tasks.length], ["ĐANG CHẠY", current.tasks.filter(t => ["DOING","IN_REVIEW"].includes(t.status)).length], ["HOÀN TẤT", current.tasks.filter(t => t.status === "CLOSED").length]]) {
    const s = el("div", null, "stat"); s.append(el("strong", count), el("span", label)); $("stats").append(s);
  }
}
function renderDag() {
  const tasks = current.tasks, host = $("dag"); host.replaceChildren();
  if (!tasks.length) return empty(host, "Chưa có task. Gửi yêu cầu cho Communicator để bắt đầu.");
  const ids = new Set(tasks.map(t=>t.task_id)), layers = new Map(), remaining = new Set(ids);
  // Defensive fallback prevents a corrupt imported DAG from hanging the UI.
  for (let round=0; remaining.size && round<=tasks.length; round++) {
    let advanced = false;
    for (const t of tasks) if (remaining.has(t.task_id) && (t.depends_on || []).every(id=>!ids.has(id)||layers.has(id))) {
      layers.set(t.task_id, Math.max(-1,...(t.depends_on||[]).map(id=>layers.get(id)??-1))+1); remaining.delete(t.task_id); advanced=true;
    }
    if (!advanced) { for (const id of remaining) layers.set(id,0); break; }
  }
  const inner = el("div", null, "dag-inner"), nodes = new Map();
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); inner.append(svg);
  for (let level=0; level<=Math.max(...layers.values()); level++) {
    const column=el("div",null,"dag-layer");
    for (const t of tasks.filter(t=>layers.get(t.task_id)===level)) {
      const button=el("button",null,`task-node${selectedTask===t.task_id?" selected":""}`);
      button.append(el("span",t.status,`badge ${t.status}`),el("strong",t.title||t.task_id),el("small",`${t.agent} · retry ${t.attempt||0} · backward ${t.backward_count||0}`));
      button.onclick=()=>{selectedTask=t.task_id;renderTask();renderDag();}; nodes.set(t.task_id,button);column.append(button);
    }
    inner.append(column);
  }
  host.append(inner);
  requestAnimationFrame(()=>{
    const box=inner.getBoundingClientRect();svg.setAttribute("width",inner.scrollWidth);svg.setAttribute("height",inner.scrollHeight);
    for(const t of tasks)for(const dep of t.depends_on||[]){const a=nodes.get(dep),b=nodes.get(t.task_id);if(!a||!b)continue;const ar=a.getBoundingClientRect(),br=b.getBoundingClientRect(),x1=ar.right-box.left,y1=ar.top+ar.height/2-box.top,x2=br.left-box.left,y2=br.top+br.height/2-box.top;const line=document.createElementNS(svg.namespaceURI,"path");line.setAttribute("d",`M${x1},${y1} C${x1+34},${y1} ${x2-34},${y2} ${x2},${y2}`);line.setAttribute("fill","none");line.setAttribute("stroke","#9cb4bc");line.setAttribute("stroke-width","1.5");svg.append(line);const arrow=document.createElementNS(svg.namespaceURI,"path");arrow.setAttribute("d",`M${x2-5},${y2-4} L${x2},${y2} L${x2-5},${y2+4}`);arrow.setAttribute("fill","none");arrow.setAttribute("stroke","#9cb4bc");svg.append(arrow);}
  });
}
function renderTask() {
  const task=current.tasks.find(t=>t.task_id===selectedTask),node=$("task-detail");
  if(!task)return empty(node,"Chọn một task trên DAG để xem kết quả, blockers và worktree.");
  node.replaceChildren(el("strong",task.title||task.task_id),el("p",task.description||""));
  const dl=el("dl");
  for(const [name,value]of [["ID",task.task_id],["Agent",task.agent],["Trạng thái",`${task.status} / ${task.outcome||"—"}`],["Task thay thế",task.replaced_by_task_id],["Blockers",(task.blocking_reasons||[]).join("; ")||"Không có dependency blocker"],["Lượt thử",`${task.attempt||0} / ${task.max_attempts||3}`],["Backward",task.backward_count||0],["Worker",task.assigned_worker],["Worktree",task.worktree_path],["Commit",task.commit_sha],["Baseline",task.base_commit],["Nghiệm thu",(task.acceptance_criteria||[]).join("; ")],["Review",task.review],["Báo cáo cuối",task.final_report],["Artifacts",(task.result_refs||task.artifact_refs||[]).join("\n")]]) if(value!==null&&value!==undefined&&value!=="")dl.append(el("dt",name),el("dd",typeof value==="object"?JSON.stringify(value):value));
  node.append(dl);
}
function renderWorkers(){
  const node=$("workers");node.replaceChildren();const workers=[...current.workers];
  for(let i=1;i<=5;i++){const id=`execute-${String(i).padStart(2,"0")}`;if(!workers.some(w=>w.worker_id===id))workers.push({worker_id:id,agent:"EXECUTE",status:"Chưa có heartbeat"});}
  for(const w of workers){const card=el("div",null,"worker");card.append(el("strong",w.worker_id||w.agent),el("span",w.status||(w.task_id?"BUSY":"IDLE"),"badge"),el("p",w.task_id||"Không có task"),el("p",w.worktree_path||"—"));if(w.progress)card.append(el("p",`Qwen stream: ${w.progress.chunks||0} chunks · ${w.progress.bytes||0} bytes`));node.append(card);}
}
function renderConversation(){
  const host=$("conversation");host.replaceChildren();
  const messages=(current.conversations||[]).flatMap(c=>c.Messages||[]);
  for(const message of messages){const row=el("div",null,"chat-message");row.append(el("strong",message.type==="human"?"Bạn":"Communicator"),el("p",message.content));host.append(row);}
  if(!messages.length)for(const task of current.tasks.filter(t=>t.agent==="ANALYZE"&&!t.parent_task_id)){const row=el("div",null,"chat-message");row.append(el("strong","Bạn"),el("p",task.description||task.title));host.append(row);}
  for(const notification of (current.notifications||[]).filter(n=>!messages.some(m=>m.content===n.message))){const row=el("div",null,"chat-message");row.append(el("strong","Communicator"),el("p",notification.message));host.append(row);}
}
function renderApprovals(){
  const host=$("approvals");host.replaceChildren();
  for(const approval of current.approvals.filter(a=>!["APPROVED","REJECTED","RESOLVED","TIMED_OUT","approved","rejected","resolved"].includes(a.status))){
    const card=el("div",null,"approval"),answer=el("textarea");answer.rows=2;answer.placeholder="Câu trả lời hoặc lý do quyết định";card.append(el("p",approval.question||approval.reason||"Cần quyết định của bạn"),answer);if(approval.expires_at)card.append(el("small",`Nếu chưa có trả lời trước ${new Date(approval.expires_at*1000).toLocaleString()}, Communicator sẽ tiếp tục với giả định được nêu rõ.`));const actions=el("div",null,"actions");
    for(const[decision,label]of [["approve","Chấp thuận / Trả lời"],["reject","Từ chối"]]){const button=el("button",label,decision==="reject"?"secondary":"");button.onclick=async()=>{try{const result=await api(`/api/projects/${selectedProject}/approvals/${approval.approval_id}`,{decision,answer:answer.value});receipt("Đã gửi quyết định",result);button.disabled=true;await refresh();}catch(e){showError(e);}};actions.append(button);}card.append(actions);host.append(card);
  }
}
function renderTimeline(){
  const host=$("timeline");host.replaceChildren();
  const events=[...current.events].sort((a,b)=>String(b.created_at||b.timestamp||"").localeCompare(String(a.created_at||a.timestamp||"")));
  if(!events.length)return empty(host,"Chưa có sự kiện công khai.");
  for(const event of events){const row=el("div",null,"event");row.append(el("time",event.created_at||event.timestamp||""),el("p",event.message||event.summary||event.reason||event.notification||event.kind||event.type),el("code",[event.task_id,event.worker_id].filter(Boolean).join(" · ")));host.append(row);}
}
function renderArtifacts(){
  const host=$("artifacts");host.replaceChildren();
  const commits=current.tasks.filter(t=>t.commit_sha);for(const t of commits){const row=el("div",null,"artifact");row.append(el("strong",t.title||t.task_id),el("p",`${t.agent} · ${t.status} · ${t.outcome||"—"}`),el("code",t.commit_sha));host.append(row);}
  for(const artifact of current.artifacts){const row=el("div",null,"artifact");row.append(el("strong",artifact.title||artifact.artifact_id||artifact.ref||"Artifact"),el("p",artifact.summary||artifact.kind||""),el("code",artifact.ref||""));if(artifact.artifact_id){const button=el("button","Xem nội dung","secondary");button.onclick=async()=>{try{const result=await api(`/api/projects/${selectedProject}/artifacts/${artifact.artifact_id}`);$("artifact-content").hidden=false;$("artifact-content").textContent=result.content||JSON.stringify(result,null,2);}catch(e){showError(e);}};row.append(el("br"),button);}host.append(row);}
  if(!commits.length&&!current.artifacts.length)empty(host,"Chưa có commit hoặc artifact được công bố.");
}
$("connect").onsubmit=async(event)=>{event.preventDefault();token=$("token").value;$("token").value="";try{await loadProjects();await refresh();await startEvents();clearInterval(refreshTimer);refreshTimer=setInterval(()=>loadProjects().then(refresh).catch(showError),2000);}catch(e){showError(e);}};
$("new-project").onsubmit=async(event)=>{event.preventDefault();const data=Object.fromEntries(new FormData(event.target));if(!data.repository_path)delete data.repository_path;try{const result=await api("/api/projects",data);receipt("Đã gửi yêu cầu tạo dự án",result);event.target.reset();await loadProjects();}catch(e){showError(e);}};
$("message").onsubmit=async(event)=>{event.preventDefault();if(!selectedProject)return showError(Error("Hãy chọn dự án trước khi gửi yêu cầu."));try{const result=await api(`/api/projects/${selectedProject}/messages`,Object.fromEntries(new FormData(event.target)));receipt("Đã gửi yêu cầu",result);event.target.reset();await refresh();}catch(e){showError(e);}};
