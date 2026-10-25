"""Local Chat Completions fixture for real process/queue/server integration tests."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest


@pytest.fixture
def decision_endpoint():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if request.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for text in ("Tiến độ: ", "đã cập nhật."):
                    chunk = {"id": f"chat-fixture-{len(requests)}", "object": "chat.completion.chunk", "created": 1,
                             "model": "fixture", "choices": [{"index": 0, "finish_reason": None,
                             "delta": {"role": "assistant", "content": text}}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()
                self.wfile.write(b'data: [DONE]\n\n')
                return
            payload = None
            for message in request["messages"]:
                text = message.get("content", "")
                if not isinstance(text, str):
                    continue
                for line in text.splitlines():
                    if line.startswith('{"task":'):
                        payload = json.loads(line)
                        break
            assert payload is not None
            task, context = payload["task"], payload["context"]
            output = {"project_id": task["project_id"], "source_task_id": task["task_id"],
                "producer_agent": task["agent"], "execution_chain_id": task["execution_chain_id"],
                "summary": "Fixture requirements processed", "source_result": {"outcome": "SUCCESS"},
                "nodes": [], "edges": []}
            if task["agent"] == "COMMUNICATOR" and not request.get("response_format"):
                output = "I have understood your requirements and will prepare the next step."
            elif task["agent"] in {"COMMUNICATOR", "ANALYZE"}:
                target = "ANALYZE" if task["agent"] == "COMMUNICATOR" else "PLAN_DELEGATE"
                output["nodes"] = [{"id": "next", "target_agent": target, "title": "Process requirements"}]
            elif task["agent"] == "PLAN_DELEGATE":
                output["nodes"] = [
                    {"id": "api", "target_agent": "EXECUTE", "title": "Implement API"},
                    {"id": "ui", "target_agent": "EXECUTE", "title": "Implement UI"},
                    {"id": "verify", "target_agent": "VERIFY_INTEGRATE", "title": "Verify integration"},
                    {"id": "final", "target_agent": "FINALIZE", "title": "Accept the result"},
                ]
                output["edges"] = [{"from": "api", "to": "verify"}, {"from": "ui", "to": "verify"},
                                   {"from": "verify", "to": "final"}]
            elif task["agent"] == "FINALIZE":
                output["source_result"].update(proposed_status="CLOSED",
                    close_task_ids=[row["task_id"] for row in context["chain_tasks"]])
            data = json.dumps({"id": "fixture", "object": "chat.completion", "created": 1,
                "model": "fixture", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": output if isinstance(output, str) else json.dumps(output)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"DECISION_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
               "DECISION_API_KEY": "local-fixture-key", "DECISION_MODEL": "fixture"}, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
