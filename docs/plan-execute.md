# Cấu hình và hợp đồng graph

Entry point toàn hệ thống là `graph.py:graph` hoặc `graph.py:build_graph()`.
Graph Initialize và ba role coding trong `orchestrator/agents/` chỉ chứa nghiệp vụ của role. Mọi role gọi
`runtime/two_phase.py:build_two_phase_graph()`, dùng chung lifecycle chạy CLI,
resolve endpoint/key/model, timeout, structured output, validate Pydantic và
xử lý lỗi. Execute và Reviewer dùng thêm adapter task/report ở `agents/assessed.py`.

## Cấu hình

`config/initialize.yaml`, `plan.yaml`, `execute.yaml`, `reviewer.yaml` có chung:

- `cli`: `qwen` hoặc `opencode` cho ba role coding; Initialize gọi ChatOpenAI.
- `timeout`: timeout CLI.
- `structure_timeout`: timeout ChatOpenAI.
- `structured_output_method`: `json_schema`, `function_calling` hoặc `json_mode`.
- `prompt`: prompt cho CLI.
- `structure_prompt`: prompt cho bước chuyển đổi/đánh giá structured output.

Plan có thêm `max_replans` và `replan_prompt`. `max_replans: 2` nghĩa là tối đa
hai lần lập lại sau lần chạy đầu; `0` để tắt. CLI Plan hoặc structured DAG lỗi
thì dừng, không retry. `dispatch_execute=False` trong factory root chỉ chạy Plan.

`build_graph(config_path=..., initialize_config_path=..., execute_config_path=..., reviewer_config_path=...)`
cho phép đổi config từng role. Các tham số LLM dùng chung:
`base_url`, `model`, `api_key` cho CLI; `structure_base_url`, `structure_model`,
`structure_api_key` cho ChatOpenAI. ChatOpenAI ưu tiên structure_* tường minh,
sau đó DECISION_* từ môi trường, cuối cùng CLI params/QWEN_*.
URL phải bao gồm API prefix, thường `/v1`. Caller tự load `.env`; ví dụ CLI có
sẵn load_dotenv. Import graph.py không thực hiện network hay chạy CLI.

## State và kết quả

Đầu vào root chỉ cần `messages: list[AnyMessage]`. Workspace tự lấy từ
WORKSPACE_ROOT hoặc thư mục làm việc hiện tại; vẫn hỗ trợ input workspace cũ. Reducer add_messages
chấp nhận cả tuple/dict message chuẩn LangGraph. Chưa gắn checkpointer: lịch sử
và approval không tự lưu qua lần gọi mới. Nội dung multimodal chỉ serialize cho
CLI, không đảm bảo CLI đọc được ảnh/binary nhúng.

Plan CLI trả `natural_plan`; model structured trả `plan` với đúng `nodes` và
`edges`. Graph phải là DAG, node ID duy nhất, edge không trùng và endpoint tồn tại.
Runtime tự nối START/END, build graph task theo phụ thuộc. Mỗi task là graph
Execute → Reviewer có conditional edge: chỉ gọi Reviewer khi Execute completed.

Execute và Reviewer đều trả báo cáo tự nhiên ở `output` và `assessment`:

```json
{
  "status": "fail",
  "notes": ["Test chưa chạy do thiếu dependency"],
  "explanation": "Chưa đủ bằng chứng đạt tiêu chí nghiệm thu",
  "blockers": ["Thiếu dependency"]
}
```

Assessment pass ánh xạ status completed; fail ánh xạ failed. CLI có lỗi,
blockers hoặc structured output lỗi luôn fail. CLI exit 0 không tự đồng nghĩa
pass. Reviewer CLI đọc code/chạy kiểm tra thực tế; model structured chỉ đánh giá
báo cáo đó. Reviewer nhận toàn bộ messages, task, findings và kết quả Execute.

Root trả `results` của mạng gần nhất; từng node có output/assessment Execute và
`review` chứa output/assessment Reviewer. `approved_work` chứa task đã review
pass. `execution_history` giữ mọi lần chạy; `replan_count` là số lần lập lại.
Reviewer fail được đưa về Plan như Execute fail, gồm đầy đủ notes và explanation.

## Replan và phần việc đã duyệt

Các nhánh độc lập hoàn tất trước khi replan. Workspace giữ cả thay đổi thành
công và thay đổi dở dang. Prompt yêu cầu kiểm tra hiện trạng, không reset/xóa
thay đổi và chỉ lập việc còn thiếu. Cả hai bước của Plan đều nhận approved_work.
Task có cùng instructions, related_files, acceptance_criteria, constraints được
runtime tái sử dụng kể cả đổi ID (`reused: true`). Trùng ý nghĩa nhưng đổi cách
diễn đạt chỉ được xử lý bằng prompt, chưa có semantic deduplication. Approval
không tự vô hiệu hóa nếu các task sau thay đổi code đã duyệt.

Mọi task dùng chung workspace; Plan phải nối phụ thuộc giữa task sửa chồng file.
Read-only của Plan/Reviewer là prompt instruction, chưa cưỡng chế bằng OS sandbox.
Coding CLI luôn tự chấp thuận: Qwen --yolo và OpenCode permission=allow. Caller cancellation vẫn
được truyền lên. Dùng ainvoke/astream cho root do DAG Execute/Reviewer là async;
các graph role riêng hỗ trợ cả invoke và ainvoke.

## Kiểm tra

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Test chỉ giả lập CLI/LLM bên ngoài. Test tích hợp dùng cả ba graph role thực để
kiểm tra review fail → replan, giữ approval, không chạy lại task thành công,
message/context propagation, fan-out và join có nhánh dài ngắn khác nhau.

## Initialize và hợp đồng messages/stream

Initialize là subgraph đầu tiên, dùng cùng graph hai phase với natural_node
ChatOpenAI thay coding CLI. Lần đầu tạo AIMessage hỏi làm rõ hoặc xác nhận phạm
vi; lần thứ hai gọi structured output ClarificationDecision gồm ready_for_plan,
summary, missing_information và explanation. Chỉ ready_for_plan=true và không
còn missing_information mới chuyển Plan. Ngược lại graph END với status
needs_clarification, giữ câu hỏi trong messages. Lỗi model trả failed, không
chuyển Plan. Vòng replan kỹ thuật quay thẳng về Plan, không hỏi lại Initialize.

Mọi lần tạo nội dung tự nhiên (Initialize/Plan/Execute/Reviewer) thêm AIMessage
mới, name tương ứng role. Reducer add_messages giữ lịch sử; graph động gom các
message mới của Execute/Reviewer về root, không nhân bản lịch sử hay approval
được tái sử dụng. Bước structured chỉ cập nhật field nghiệp vụ, không trả message.
Tất cả node và model invocation structured có tag no_stream; ChatOpenAI của
bước này dùng streaming=False, disable_streaming=True. Khi dùng stream_mode
messages, UI lọc event metadata tags chứa no_stream vì đây là tag dành cho UI,
không phải cơ chế tự lọc của mọi consumer LangGraph.

Với invoke thủ công, lượt tiếp theo truyền:

```python
result = await workflow.ainvoke({"messages": [*result["messages"], ("user", answer)]})
```

Hoặc dùng lịch sử được host LangGraph lưu trong thread. Các model vẫn dùng cấu
hình endpoint nội bộ nêu trên. Initialize không đọc code hoặc chạy CLI.

## Thông báo theo mốc, không có timer

Graph role coding được chia thành:
`prepare → announce_start → coding → structure → announce_result`.
announce_start trả AIMessage trước khi CLI chạy; announce_result đọc status và
assessment, trả AIMessage pass/fail sau khi structured output hoàn tất. Nội dung
báo cáo CLI vẫn là message riêng. Initialize giữ hai bước hội thoại và quyết định.

Root chạy `plan → announce_execution → execute_network → summarize_execution`.
Summary nêu số task được duyệt, task fail/skipped và có replan tiếp hay không;
không gọi thêm LLM, không có timer. prepare_replan cũng trả thông báo trước khi
quay về Plan. ID team-progress dùng chung cho summary toàn team; từng role/task
có ID progress:<role>:<task_id>. Reducer thay nội dung message cũ theo ID.

Để thấy cập nhật trước khi toàn bộ workflow xong, dùng stream_mode updates và
subgraphs=True, đọc messages của mỗi node; UI upsert theo message.id. Chỉ dùng
ainvoke sẽ nhận toàn bộ kết quả sau khi workflow kết thúc. stream_mode messages
chủ yếu dành cho token từ model, không thay thế updates cho AIMessage do node tạo.
Xem ví dụ: `python -m examples.stream_progress 'Thêm API health và test'`.

## Workdir riêng cho từng coding CLI

Mỗi lần gọi run_llm_cli tạo TemporaryDirectory riêng, kể cả khi các task chạy
đồng thời hoặc Plan lập lại. Process cwd là thư mục đó, có project/ liên kết tới
workspace thật và tmp/ cho file tạm. Prompt truyền rõ đường dẫn repository và
hướng dẫn chạy test/build/git trong project/. TMPDIR, AGENT_WORKDIR và
AGENT_PROJECT_DIR được truyền riêng vào subprocess, không đổi môi trường cha.

Workdir tự dọn khi CLI thành công, lỗi hoặc timeout. Thay đổi code trong project/
được giữ để Reviewer và task phụ thuộc sử dụng. Đây là scratch directory riêng,
không phải bản sao repository/git worktree; các task sửa chung file vẫn phải có
edges phụ thuộc. Cleanup không xóa lịch sử/cache CLI lưu bên ngoài workdir.
Có thể đặt AGENT_WORKDIR_ROOT để chọn thư mục cha; mặc định dùng temp hệ thống.
Qwen luôn có --yolo; OpenCode dùng permission=allow và OPENCODE_PERMISSION cho
mọi tool. Plan/Reviewer chỉ đọc vẫn do prompt hướng dẫn.

## Docker CLI runner

LangGraph dev ở host, riêng Plan/Execute/Reviewer CLI nằm trong Docker image.
CODING_CLI_RUNTIME=docker chọn runner; CODING_CLI_IMAGE chọn image, mặc định
coding-agent-cli:local. local vẫn hỗ trợ cho kiểm thử/môi trường cũ.
Dockerfile cài cả @qwen-code/qwen-code và opencode-ai cùng Node 22/Python 3.13.
Mỗi lần gọi docker run --rm --init có cidfile riêng; timeout/exception dọn đúng
container đó. Workspace và scratch mount cùng đường dẫn host để liên kết project/
vẫn hợp lệ. CA bundle mount readonly; env SSL_CERT_FILE/NODE_EXTRA_CA_CERTS trỏ
đúng path trong container. API key forward bằng tên env, không ghi vào image.
Trong coding container dùng python của image, không dùng .venv host.

## Compose: cả ba công cụ trong một container

`docker compose up --build -d` dùng target team trong Dockerfile. Service duy nhất
team chứa LangGraph dev và hai CLI; port host 2026 nối port container 2026.
CODING_CLI_RUNTIME=local nghĩa là CLI chạy trực tiếp trong container này. Compose
override giá trị runtime từ .env, không khởi tạo container con và không mount
Docker socket. Env LLM/API keys lấy từ .env. Config langgraph.docker.json không
nạp lại .env để tránh ghi đè đường dẫn host lên môi trường container.

Source mount /app, repository mục tiêu mount /workspace, CA mount readonly ở
/certs/ca-bundle.pem. Hai biến SSL_CERT_FILE/NODE_EXTRA_CA_CERTS trỏ vào path này.
PROJECT_WORKSPACE mặc định ., có thể đổi repository host. Volume team-state giữ
/app/.langgraph_api và tách khỏi persistence của server host cũ. Build secret
ca_bundle chỉ dùng khi npm/pip cài package, không ghi CA/key vào image.
