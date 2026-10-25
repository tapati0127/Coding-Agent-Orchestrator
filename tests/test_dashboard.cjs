// Run with: node --test tests/test_dashboard.cjs
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");

test("dashboard starts projects from chat, reopens history, and receives timeout after chat ends", async () => {
  class Element {
    constructor() { this.children = []; this.options = []; this.value = ""; this.textContent = ""; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; this.options = children; }
    add(option) { this.options.push(option); }
    remove() {}
    scrollIntoView() {}
  }
  const elements = Object.fromEntries([
    "error", "welcome", "conversation", "progress", "send", "stop", "project", "connect", "token", "connection", "new-chat", "message",
  ].map(id => [id, new Element()]));
  elements.token.value = "test-token";
  const calls = [], subscriptions = [];
  let created = false;
  const encoder = new TextEncoder();
  const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  const json = data => ({ok:true, json:async () => data});
  const context = vm.createContext({
    document: {getElementById:id => elements[id], createElement:() => new Element()},
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    crypto:webcrypto, AbortController, TextDecoder,
    fetch:async (url, options = {}) => {
      calls.push({url, options});
      if (url === "/api/projects") return json({projects:[
        {project_id:"old", title:"Existing portal"}, ...(created ? [{project_id:"auto", title:"New portal"}] : []),
      ]});
      if (url === "/api/projects/old/snapshot") return json({
        conversations:[{Messages:[{type:"human", content:"Build portal"}, {type:"ai", content:"Who uses it?"}]}],
        notifications:[], approvals:[{status:"PENDING"}],
      });
      if (url.endsWith("/events")) return {ok:true, body:new ReadableStream({start(controller) {
        subscriptions.push({url, controller, signal:options.signal});
        options.signal.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError")));
      }})};
      if (url === "/api/chat/stream") {
        created = true;
        return {ok:true, body:new ReadableStream({start(controller) {
          controller.enqueue(encoder.encode(
            frame("thread", {thread_id:"thread-auto", project_id:"auto"}) +
            frame("custom|communicate:child", {type:"project", project_id:"auto"}) +
            frame("custom|communicate:child", {type:"reply", id:"reply", content:"What is the scope?"}) +
            frame("custom|communicate:child", {type:"status", status:"waiting_for_user"})));
          controller.close();
        }})};
      }
      throw Error(`Unexpected request: ${url}`);
    },
  });
  vm.runInContext(readFileSync(new URL("../dashboard/app.js", `file://${__filename}`), "utf8"), context);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  const contents = () => elements.conversation.children.map(row => row.children[1].textContent);
  try {
    await elements.connect.onsubmit({preventDefault(){}});
    assert.deepEqual(elements.project.options.map(option => option.value), ["", "old"]);
    elements.project.value = "old";
    await elements.project.onchange();
    await flush();
    assert.deepEqual(contents(), ["Build portal", "Who uses it?"]);
    assert.equal(elements.progress.textContent, "Đang chờ bạn trả lời");
    const oldSubscription = subscriptions[0];
    elements["new-chat"].onclick();
    assert.equal(elements.project.value, "");
    assert.equal(oldSubscription.signal.aborted, true);
    assert.deepEqual(contents(), []);
    await elements.message.onsubmit({preventDefault(){}, target:{elements:{message:{value:"Build new portal"}}, reset(){}}});
    await flush();
    const request = JSON.parse(calls.find(call => call.url === "/api/chat/stream").options.body);
    assert.equal(request.project_id, null);
    assert.equal(request.thread_id, null);
    assert.equal(request.messages.length, 1);
    assert.equal(elements.project.value, "auto");
    assert.equal(elements.progress.textContent, "Đang chờ bạn trả lời");
    const subscription = subscriptions.at(-1);
    assert.equal(subscription.url, "/api/projects/auto/events");
    const timeout = {kind:"notification", message:"Timeout: continuing to Analyze with assumptions."};
    subscription.controller.enqueue(encoder.encode(frame("message", timeout) + frame("message", timeout)));
    await flush();
    assert.equal(contents().filter(content => content === timeout.message).length, 1);
    assert.equal(elements.send.disabled, false);
  } finally {
    elements["new-chat"].onclick();
    await flush();
  }
});
