// The server relays LangGraph SDK messages/custom events, including subgraphs.
let token = "", threadId = null, history = [], controller = null;
const $ = (id) => document.getElementById(id);
const showError = (e) => { $("error").textContent = e.message || String(e); $("error").hidden = false; };
async function api(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET", headers: {
    Authorization: `Bearer ${token}`, ...(body ? {"Content-Type": "application/json"} : {})
  }, ...(body ? {body: JSON.stringify(body)} : {})});
  if (!response.ok) throw Error(`HTTP ${response.status}`);
  return response.json();
}
function bubble(role, text, namespace = "") {
  $("welcome")?.remove();
  const row = document.createElement("article"); row.className = `chat-message ${role}`;
  const label = document.createElement("strong"); label.textContent = role === "user" ? "Bạn" : "Communicator";
  const content = document.createElement("p"); content.textContent = text;
  row.append(label, content);
  if (namespace) { const source = document.createElement("small"); source.textContent = namespace; row.append(source); }
  $("conversation").append(row);
  row.scrollIntoView({block: "nearest"});
  return content;
}
function resetChat() {
  controller?.abort(); controller = null; threadId = null; history = [];
  $("conversation").replaceChildren(); $("progress").textContent = "";
  $("send").disabled = !token; $("stop").hidden = true;
}
async function loadProjects() {
  const {projects} = await api("/api/projects"), selected = $("project").value;
  $("project").replaceChildren(new Option("Chat trực tiếp", ""));
  for (const project of projects) $("project").add(new Option(project.title || project.project_id, project.project_id));
  $("project").value = selected;
}
function contentText(content) {
  return typeof content === "string" ? content : (content || []).filter(b => b.type === "text").map(b => b.text).join("");
}
async function send(messages) {
  // Closing a stream stops this chat run, while Dispatcher tasks keep running.
  controller?.abort();
  const active = new AbortController(); controller = active;
  $("stop").hidden = false; $("send").disabled = true; $("error").hidden = true;
  const replies = new Map(); let current = null;
  function receive(event, data) {
    const [kind, ...namespace] = event.split("|"), source = namespace.join(" / ");
    if (kind === "thread") { threadId = data.thread_id; return; }
    if (kind === "error") throw Error(data.message || data.error || "Chat stream failed");
    if (kind === "messages/partial" || kind === "messages/complete") {
      for (const message of data) {
        if (message.type !== "ai" || !message.content) continue;
        const key = message.id;
        if (!replies.has(key)) replies.set(key, bubble("assistant", "", source));
        current = replies.get(key); current.textContent = contentText(message.content);
      }
    } else if (kind === "messages") {
      const [message, metadata] = data;
      if (message.type !== "AIMessageChunk" && message.type !== "ai") return;
      const key = message.id;
      if (!replies.has(key)) replies.set(key, bubble("assistant", "", source || metadata?.langgraph_node));
      current = replies.get(key); current.textContent += contentText(message.content);
    } else if (kind === "custom" && data.type === "reply") {
      const key = data.id || crypto.randomUUID();
      if (!replies.has(key)) replies.set(key, bubble("assistant", "", source));
      current = replies.get(key); current.textContent = contentText(data.content);
    } else if (kind === "custom" && data.type === "status") {
      const statuses = {submitted:"Đã gửi yêu cầu", running:"Đang xử lý", completed:"Đã hoàn tất", waiting_for_user:"Đang chờ bạn trả lời", blocked:"Cần kiểm tra task bị chặn"};
      $("progress").textContent = statuses[data.status] || data.status;
    }
  }
  try {
    const response = await fetch("/api/chat/stream", {method:"POST", headers:{
      Authorization:`Bearer ${token}`, "Content-Type":"application/json"
    }, body:JSON.stringify({messages, thread_id:threadId, project_id:$("project").value || null}), signal:active.signal});
    if (!response.ok) throw Error(`Chat HTTP ${response.status}`);
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "";
    while (true) {
      const {value, done} = await reader.read(); if (done) break;
      buffer += decoder.decode(value, {stream:true}).replace(/\r\n/g, "\n");
      let split;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, split); buffer = buffer.slice(split + 2);
        let event = "message"; const lines = [];
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          if (line.startsWith("data:")) lines.push(line.slice(5).trimStart());
        }
        if (lines.length) receive(event, JSON.parse(lines.join("\n")));
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") showError(e);
  } finally {
    if (controller === active) {
      // Responses are supplied as input on the next turn; the graph node does
      // not return assistant messages to the state reducer.
      for (const content of new Set(replies.values())) if (content.textContent) history.push({
        id:crypto.randomUUID(), role:"assistant", content:content.textContent
      });
      controller = null; $("stop").hidden = true; $("send").disabled = false;
    }
  }
}
$("connect").onsubmit = async (e) => {
  e.preventDefault(); token = $("token").value; $("token").value = "";
  try { await loadProjects(); $("connection").textContent = "Đã kết nối"; $("send").disabled = false; $("error").hidden = true; }
  catch (error) { showError(error); }
};
$("new-chat").onclick = resetChat;
$("project").onchange = resetChat;
$("stop").onclick = () => { controller?.abort(); $("progress").textContent = "Đã dừng theo dõi"; };
$("new-project").onsubmit = async (e) => {
  e.preventDefault(); const data = Object.fromEntries(new FormData(e.target));
  if (!data.repository_path) delete data.repository_path;
  try { await api("/api/projects", data); await new Promise(resolve => setTimeout(resolve, 500)); await loadProjects(); e.target.reset(); }
  catch (error) { showError(error); }
};
$("message").onsubmit = async (e) => {
  e.preventDefault(); if (!token) return;
  const content = e.target.elements.message.value.trim(); if (!content) return;
  const message = {id:crypto.randomUUID(), role:"user", content};
  history.push(message); bubble("user", content); e.target.reset(); await send([...history]);
};
