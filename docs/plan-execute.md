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
Runtime dùng `review_network.py` để chạy task theo dependency và tạo mốc
review trước điểm nối, sau task review_after=true, và cuối kế hoạch. Chuỗi nối
tiếp không review sau từng task. `build_task_graph(executor, reviewer)` vẫn là
API độc lập để caller chủ động chạy review riêng một task.

Execute và Reviewer đều trả báo cáo tự nhiên ở `output` và `assessment`:

```json
{
  "status": "fail",
  "notes": ["Test chưa chạy do thiếu dependency"],
  "explanation": "Chưa đủ bằng chứng đạt tiêu chí nghiệm thu",
  "blockers": ["Thiếu dependency"]
}
```

Trong role, assessment pass ánh xạ status completed; fail ánh xạ failed.
Trong network, Execute pass chỉ là executed cho đến khi nhóm review pass. CLI có lỗi,
blockers hoặc structured output lỗi luôn fail. CLI exit 0 không tự đồng nghĩa
pass. Reviewer CLI đọc code/chạy kiểm tra thực tế; model structured chỉ đánh giá
báo cáo đó. Reviewer nhận messages đã lọc artifact hiển thị, task, findings và kết quả Execute.

Root trả `results` của mạng gần nhất; từng node có output/assessment Execute và
`review` chứa output/assessment Reviewer, review_group và task_ids của cả nhóm. `approved_work` chứa task đã review
pass. `execution_history` giữ mọi lần chạy; `replan_count` là số lần lập lại.
Reviewer fail được đưa về Plan như Execute fail, gồm đầy đủ notes và explanation.

## Replan và phần việc đã duyệt

Các nhánh độc lập hoàn tất trước khi replan. Khi bật Git isolation, workspace chính chỉ chứa thay đổi đã merge; task lỗi giữ
code ở nhánh/worktree riêng trong metadata integration. Khi tắt isolation,
workspace giữ cả thay đổi thành công và thay đổi dở dang. Prompt yêu cầu kiểm tra hiện trạng, không reset/xóa
thay đổi và chỉ lập việc còn thiếu. Cả hai bước của Plan đều nhận approved_work.
Task có cùng instructions, related_files, acceptance_criteria, constraints được
runtime tái sử dụng kể cả đổi ID (`reused: true`). Khi bật Git isolation, chỉ tái
sử dụng nếu integration.status=merged và merged_commit còn thuộc lịch sử nhánh đích. Trùng ý nghĩa nhưng đổi cách
diễn đạt chỉ được xử lý bằng prompt, chưa có semantic deduplication. Approval
không tự vô hiệu hóa nếu các task sau thay đổi code đã duyệt.

Mỗi task dùng worktree riêng mặc định; Plan vẫn phải nối phụ thuộc giữa task
sửa chồng file hoặc phụ thuộc interface để tránh conflict khi tích hợp.
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
vi; câu trả lời hiển thị trực tiếp, không qua model viết lại. Lần thứ hai gọi
structured output ClarificationDecision gồm short_explanation rồi ready_for_plan.
Chỉ ready_for_plan=true mới chuyển Plan. Ngược lại graph END với status
needs_clarification, giữ câu hỏi trong messages. Lỗi model trả failed, không
chuyển Plan. Vòng replan kỹ thuật quay thẳng về Plan, không hỏi lại Initialize.

Initialize thêm AIMessage hội thoại; Plan phát một thông báo kèm artifact;
Execute/Reviewer chỉ cập nhật message tiến độ, không đưa báo cáo CLI vào chat.
Reducer add_messages giữ lịch sử; graph động gom các
message mới của Execute/Reviewer về root, không nhân bản lịch sử hay approval
được tái sử dụng. Bước structured cập nhật field nghiệp vụ; riêng Plan trả thêm
AIMessage lời tóm tắt kèm artifact sau khi DAG được validate. Payload artifact
và phần lời Plan đều dựng bằng code. Initialize phát
thêm phản hồi giải thích nếu bước readiness thất bại.
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
announce_start trả AIMessage trước khi CLI chạy, nêu tên CLI và liên kết
`/terminals`. Node coding giữ báo cáo trong output và trả AIMessage thông báo CLI đã
kết thúc và đang xử lý structured output; lỗi/timeout cũng có thông báo tương ứng.
announce_result đọc status và assessment, trả AIMessage pass/fail sau khi
structured output hoàn tất. Báo cáo CLI không đưa vào chat. Initialize giữ hai bước hội thoại và quyết định.

Root chạy `initialize → announce_plan → plan → request_plan_feedback →
assess_plan_approval → announce_execution → execute_network → summarize_execution`. Nếu cần hỏi làm rõ thì dừng ngay sau Initialize.
Summary nêu số task được duyệt, task fail/skipped và có replan tiếp hay không;
các số liệu được tính tất định, còn lời diễn đạt qua bước user_response; không có timer. prepare_replan cũng trả thông báo trước khi
quay qua announce_plan rồi vào Plan. ID team-progress dùng chung cho summary toàn team; từng role/task
có ID progress:<role>:<task_id>. Reducer thay nội dung message cũ theo ID.

Để thấy cập nhật trước khi toàn bộ workflow xong, dùng stream_mode updates và
subgraphs=True, đọc messages của mỗi node; UI upsert theo message.id. Chỉ dùng
ainvoke sẽ nhận toàn bộ kết quả sau khi workflow kết thúc. stream_mode messages
chủ yếu dành cho token từ model, không thay thế updates cho AIMessage do node tạo.
Scheduler còn phát custom event `{node, update}` sau mỗi Execute/mốc review;
consumer có thể đọc stream_mode=["updates", "custom"] để cập nhật trạng thái
executed/completed và messages theo ID.
Xem ví dụ: `python -m examples.stream_progress 'Thêm API health và test'`.

## Workdir riêng cho từng coding CLI

Mỗi lần gọi run_llm_cli tạo TemporaryDirectory riêng, kể cả khi các task chạy
đồng thời hoặc Plan lập lại. Process cwd là thư mục đó, có project/ liên kết tới
checkout được giao và tmp/ cho file tạm. Với DAG isolation, checkout của Executor
là nhánh task riêng, checkout Reviewer là bản merge thử trên nhánh chính mới nhất. Prompt truyền rõ đường dẫn repository và
hướng dẫn chạy test/build/git trong project/. TMPDIR, AGENT_WORKDIR và
AGENT_PROJECT_DIR được truyền riêng vào subprocess, không đổi môi trường cha.

Scratch workdir tự dọn khi CLI thành công, lỗi hoặc timeout. Thay đổi code trong
project/ được giữ trong checkout của task; runtime chuyển kết quả qua commit/merge
để Reviewer và task phụ thuộc sử dụng. Đây là scratch directory riêng,
không thay thế Git worktree theo task; các task sửa chồng file vẫn phải có
edges phụ thuộc. Tắt GIT_TASK_ISOLATION sẽ quay lại sửa chung workspace. Cleanup không xóa lịch sử/cache CLI lưu bên ngoài workdir.
Có thể đặt AGENT_WORKDIR_ROOT để chọn thư mục cha; mặc định dùng temp hệ thống.
Qwen luôn có --yolo; OpenCode dùng permission=allow và OPENCODE_PERMISSION cho
mọi tool. Plan/Reviewer chỉ đọc vẫn do prompt hướng dẫn.

## Docker CLI runner

LangGraph dev ở host, riêng Plan/Execute/Reviewer CLI nằm trong Docker image.
CODING_CLI_RUNTIME=docker chọn runner; CODING_CLI_IMAGE chọn image, mặc định
coding-agent-cli:local. local chạy CLI trực tiếp và là chế độ mặc định của Compose.
Dockerfile cài @qwen-code/qwen-code, opencode-ai và glab cùng Node 22/Python 3.13.
Build image CLI riêng cần `docker build --target cli ...`; target cuối mặc định là team.
Mỗi lần gọi docker run --rm --init có cidfile riêng; timeout/exception dọn đúng
container đó. Workspace và scratch mount cùng đường dẫn host để liên kết project/
vẫn hợp lệ. CA bundle mount readonly; env SSL_CERT_FILE/NODE_EXTRA_CA_CERTS trỏ
đúng path trong container. API key forward bằng tên env, không ghi vào image.
Khi project là linked worktree, runner mount thêm Git common directory tại đúng
đường dẫn tuyệt đối để git truy cập metadata. Trong coding container dùng python
của image, không dùng .venv host.

## Compose: LangGraph và các CLI trong một container

`docker compose up --build -d` dùng target team trong Dockerfile. Service duy nhất
team chứa LangGraph dev, Qwen, OpenCode và glab; port host 2026 nối port container 2026.
CODING_CLI_RUNTIME=local nghĩa là CLI chạy trực tiếp trong container này. Compose
override giá trị runtime từ .env, không khởi tạo container con và không mount
Docker socket. Env LLM/API keys lấy từ .env. Config langgraph.docker.json không
nạp lại .env để tránh ghi đè đường dẫn host lên môi trường container.

Source mount /app, repository mục tiêu mount /workspace, CA mount readonly ở
/certs/ca-bundle.pem. Hai biến SSL_CERT_FILE/NODE_EXTRA_CA_CERTS trỏ vào path này.
PROJECT_WORKSPACE mặc định ., có thể đổi repository host. Volume team-state giữ
/app/.langgraph_api và tách khỏi persistence của server host cũ. Build secret
ca_bundle chỉ dùng khi npm/pip cài package, không ghi CA/key vào image.

## Thinking global và override từng role

LLM_THINKING=false và LLM_THINKING_FORMAT=enable_thinking là mặc định chung.
Ưu tiên: YAML thinking/thinking_format không null → factory options → .env.
ChatOpenAI được truyền extra_body trực tiếp, không phụ thuộc hostname endpoint.
Qwen dùng model.generationConfig.extra_body trong .qwen/settings.json tạm;
OpenCode dùng options của model internal. File cấu hình Qwen được tự dọn.

Format enable_thinking gửi {"enable_thinking": false}; chat_template_kwargs gửi
{"chat_template_kwargs": {"enable_thinking": false}}. Giá trị true ghi đè để bật.
YAML thinking phải là boolean thật hoặc null. Server/model cần hỗ trợ tham số.
Không thay đổi pass/fail assessment. Báo cáo gốc và phản hồi hiển thị đều ẩn token
trung gian bằng no_stream; UI nhận AIMessage hoàn chỉnh qua updates.


## Artifact chỉ đọc và preprocessing messages

Node `announce_plan` chạy trước khi gọi Plan, cả lần đầu và mỗi lần replan.
Node trả AIMessage có name `plan_handoff`, ID `plan-handoff:<attempt>:<uuid>`,
nội dung Markdown về các bước tiếp theo, số công việc đã duyệt khi replan và
liên kết `/terminals`. UUID giúp các lượt chạy trên cùng thread không ghi đè nhau.
Không tạo artifact bàn giao khi Initialize chưa đủ thông tin hoặc bị lỗi.

Sau khi structured PlanGraph được validate thành công, `agents/plan.py:finish()`
gọi `runtime/artifacts.py:plan_artifact()`. AIMessage có name `plan`, nội dung
Mermaid `flowchart TD` và bảng nhiệm vụ/phụ thuộc/tiêu chí hoàn thành. Sơ đồ nối
START với các root, các edge đúng DAG và các leaf với END; mỗi node được chạy
qua Execute; Reviewer được chèn tại các mốc nhóm nêu trong artifact. Đây là kế hoạch, không phải trạng thái thực thi từng
node. Client không render Mermaid vẫn đọc được bảng Markdown.

Artifact-only có metadata `additional_kwargs.orchestrator_artifact`, gồm type,
mime_type, version, read_only và attempt. Với message vừa có lời vừa có artifact,
`orchestrator_artifacts` là list payload kèm content Markdown; `orchestrator_text`
giữ đúng phần lời chưa ghép artifact. Phần lời và artifact cùng xuất hiện trong
AIMessage.content; model chỉ sinh phần lời, runtime ghép payload sau đó.

`runtime/artifacts.py:preprocess_messages(messages)` trả list mới, loại artifact-only
và bỏ payload của mọi type trong namespace artifact của orchestrator. Với mixed
message, hàm giữ phần lời và bỏ metadata artifact/marker trình bày. Không xóa state
chat hoặc `state.plan`. Initialize lọc cho cả hội thoại/structured payload, Plan
lọc trước serialize prompt, network lọc trước Execute/Reviewer và Reviewer lọc
khi enrich task. Model viết phản hồi cũng dùng cùng hàm trước khi lấy lịch sử/draft.

`message_delta()` bỏ qua khác biệt chỉ do preprocessing: lịch sử sạch từ agent
không ghi đè mixed message chứa artifact trong graph cha. Thay đổi nội dung thực
sự cùng ID vẫn được truyền lên và upsert. Agent/model mới nhận messages phải dùng
preprocessing tại biên đầu vào.

## Terminal Monitor và log CLI

Cả `langgraph.json` và `langgraph.docker.json` dùng
`http.app=./orchestrator/terminal_monitor.py:app`. Starlette app cung cấp:

| Endpoint GET | Nội dung |
| --- | --- |
| `/terminals`, `/terminals/` | Giao diện chỉ đọc, tự cập nhật log và hỗ trợ ANSI cơ bản |
| `/terminals/api/files` | `files`: tên file tương thích cũ; `sessions`: tiêu đề, vai trò, task_id, trạng thái, thời gian; ưu tiên phiên đang chạy |
| `/terminals/api/read` | Đọc theo `name`, byte `offset` và `file_id`; dữ liệu base64 |

Compose dùng `http://localhost:2026/terminals`; host dev mặc định dùng
`http://localhost:2024/terminals`. Không chạy HTTP server riêng hoặc mở thêm cổng.
Viewer không bổ sung cơ chế đăng nhập riêng.

Đặt `CLI_LOG_DIR=.local/cli-logs` trong `.env` để bật `run_logged()` cho local
và Docker runner; để trống/bỏ biến để giữ runner capture output thông thường.
Đường dẫn tương đối được resolve từ cwd của tiến trình LangGraph. Log mỗi lần
chạy có timestamp UTC và UUID; sidecar `<name>.log.json` lưu metadata, được ghi
atomically với quyền 0600. Log và metadata xuất hiện trước khi launch CLI, có
trạng thái `running`, rồi chuyển `completed`/`failed`/`timed_out` khi kết thúc.
Viewer dùng tiêu đề nhiệm vụ và vai trò thay tên file; instructions được rút gọn
tối đa 160 ký tự, Plan lấy yêu cầu người dùng gần nhất, không gọi LLM đặt tên.
Phiên cũ không có metadata dùng nhãn lịch sử. Danh sách và nội dung poll mỗi
500 ms; chuyển phiên sẽ loại bỏ phản hồi đang đọc của phiên trước.

Qwen chạy `--output-format stream-json --include-partial-messages`, OpenCode chạy
`run --format json`. `cli_events.EventRenderer` chuyển JSONL thành văn bản tiến
độ, phản hồi và tool; Qwen text delta xuất hiện ngay, OpenCode text/tool xuất hiện
theo sự kiện hoàn tất từng phần do CLI hỗ trợ. Chưa có sự kiện mới thì viewer giữ
nội dung hiện tại. `run_logged()` vẫn giữ raw stdout/stderr riêng trong kết quả;
`run_llm_cli()` tách báo cáo assistant khỏi protocol bằng `report_text()` trước
khi đưa vào structured assessment. Không gửi JSON sự kiện/echo prompt vào báo cáo.

Log che giá trị các biến môi trường
có tên chứa TOKEN, API_KEY, PASSWORD hoặc SECRET, kể cả chuỗi bị chia qua các chunk
và giá trị JSON-escaped. Bộ lọc chỉ giữ suffix có thể là phần đầu token để log
ngắn cũng được flush ngay; tiêu đề/metadata cũng che các giá trị này;
đây không phải bộ phát hiện mọi dữ liệu nhạy cảm trong nội dung CLI.

Log nằm ngoài scratch workdir, có quyền file 0600 và không tự xóa. Với Compose,
đường dẫn mặc định được giữ trên host qua bind mount `/app`; nếu đổi sang thư mục
khác trong container thì cần mount phù hợp để giữ log khi tạo lại container.
Reader từ chối path traversal/symlink, giới hạn chunk 64 KiB và lấy phần cuối
128 KiB khi mở log lớn. `file_id`/kích thước hỗ trợ reset khi file đổi hoặc bị cắt.

## GitLab CLI trong image

Dockerfile copy binary glab từ stage `gitlab/glab:${GLAB_VERSION}`; mặc định tag
latest, có thể pin bằng build arg GLAB_VERSION hoặc biến cùng tên của Compose.
Image kiểm tra `glab --version` khi build. Điền GITLAB_TOKEN và GITLAB_HOST
(nếu dùng GitLab riêng) vào `.env`; Compose nạp trực tiếp vào container team,
không cần chạy glab auth login hoặc ghi token vào Dockerfile/build args.

Sau khi đổi image: `docker compose up -d --build team`. Kiểm tra:
`docker compose exec team glab --version`. Runner Docker CLI riêng chỉ forward
các biến trong `FORWARDED_ENV`; cần bổ sung danh sách đó nếu muốn truyền thêm
biến GitLab vào container con. Không commit `.env` hoặc file log chứa dữ liệu thật.


## Git worktree theo task và review-gated merge local

Mặc định `GIT_TASK_ISOLATION=true`, `GIT_TARGET_BRANCH=main`. Workspace phải là
root repository có commit, đang checkout đúng nhánh đích và sạch; runtime từ chối
repo bẩn, thư mục không phải Git hoặc nhánh không khớp, không tự stash/reset.
`GIT_TASK_ISOLATION=false` giữ chế độ shared workspace cũ cho tương thích.
Graph Execute độc lập vẫn làm việc tại workspace caller truyền; isolation được
điều phối bởi `runtime/review_network.py`.

`runtime/git_tasks.py:TaskCheckout` thực hiện chu trình:

1. Tạo worktree/nhánh riêng cho mỗi Execute từ nhánh đích sạch; merge các commit
   dependency vào worktree đó trước khi chạy để kế thừa thay đổi chưa review.
2. Execute pass: commit trong nhánh tạm, giữ kết quả provisional executed.
   Chuỗi nối tiếp tiếp tục trên worktree riêng, chưa cập nhật nhánh đích.
3. Tại điểm nối nhiều nhánh, mốc review_after hoặc cuối kế hoạch, gom task chưa
   review thuộc nhóm. Tạo candidate chung từ các commit của nhóm.
4. Giữ khóa tích hợp; merge candidate với nhánh đích mới nhất trong worktree
   review riêng. Reviewer kiểm tra toàn bộ diff/tiêu chí/tương tác của nhóm.
5. Nếu review pass, candidate nguyên vẹn và nhánh đích vẫn đúng phiên bản đã
   review, fast-forward nhánh đích một lần cho cả nhóm. Chuyển các task trong
   nhóm sang completed và lưu cùng review.task_ids/review_group/merged_commit.
6. Nếu review/conflict thất bại, cả nhóm chưa được duyệt, nhánh đích không đổi;
   giữ các worktree/branch cho Plan điều tra. Nhóm khác đã duyệt vẫn được giữ.
7. Thành công: dọn worktree Execute và candidate bằng git worktree remove,
   giữ nhánh/commit. Lỗi dọn worktree không đổi merge thành failed.

Khóa `flock` ở Git common directory phối hợp giữa các task và các tiến trình
orchestrator cùng repository. Git operations đang chạy được chờ kết thúc trước
khi nhả khóa nếu workflow bị cancel. Không chạy checkout/reset/merge thủ công
trên workspace chính trong lúc workflow hoạt động; khóa không điều phối công cụ
bên ngoài không tuân thủ khóa. Không push remote, không tạo hoặc merge GitLab MR.

`results[task_id].integration` có `branch`, `target_branch`, `commit`,
`merged_commit` và `status=merged` khi thành công. Khi chưa merge, metadata có
`worktree`, `review_worktree`, `group_worktree`, `group_branch`, `base_commit`,
`status=not_merged`. Plan nhận metadata
trong execution_history để phân tích conflict hoặc giao task phục hồi từ main
mới nhất và thay đổi của nhánh lỗi. Không tự bỏ thay đổi của task khác để hết conflict.
Task nối tiếp có thể dùng commit dependency khi còn executed. Task sau điểm
nối hoặc mốc review sớm phải chờ nhóm được duyệt và merge.

Worktree lỗi được giữ; worktree thành công được dọn, nhánh không tự xóa.
`GIT_WORKTREE_ROOT` mặc định `/tmp/orchestrator-worktrees`; với Compose cần đặt
vào thư mục mount phù hợp nếu muốn giữ file chưa commit qua recreate container.
Git branches/commits vẫn nằm trong repository được mount. `.env` và file ignored
không được sao chép sang worktree mới; cấu hình CLI được truyền qua môi trường.
Các dependency môi trường test của repository cần sẵn có ở checkout mới.

Reviewer phải kiểm tra tác động tích hợp và các regression test cần thiết.
Chưa có node kiểm thử toàn repository riêng ở cuối workflow; worktree cũng
không phải ranh giới bảo mật OS vì Git metadata và filesystem vẫn cùng user.


## Skills config riêng theo CLI

`runtime/cli_skills.py` đọc `config/cli/qwen.yaml` hoặc
`config/cli/opencode.yaml` theo CLI được role YAML chọn. Có thể override từng file
bằng `QWEN_SKILLS_CONFIG` / `OPENCODE_SKILLS_CONFIG`; đường dẫn override tương đối
được resolve từ cwd tiến trình LangGraph. Đường dẫn trong `skills.paths` luôn
resolve từ thư mục chứa YAML; hỗ trợ đường dẫn tuyệt đối và `~`.

Schema có `skills.enabled` (boolean, mặc định true), `skills.paths`
(list đường dẫn chung, mặc định rỗng) và `skills.roles` (mapping plan/execute/reviewer
sang list đường dẫn). Role không hợp lệ hoặc list sai kiểu bị từ chối. Path trỏ vào một skill hoặc collection chứa các
thư mục skill con trực tiếp; không quét sâu đệ quy. Skill cần YAML frontmatter
với name khớp tên thư mục, tối đa 64 ký tự chữ thường/số, phân cách bằng dấu gạch
ngang đơn; description là chuỗi 1–1024 ký tự. Hai nguồn khác nhau có cùng name
bị từ chối để tránh chọn nhầm skill; chọn trùng đúng một nguồn được gộp lại.
Config/path/metadata không hợp lệ làm CLI thất bại trước khi subprocess chạy.

`run_llm_cli()` nạp cấu hình mỗi invocation, rồi copy nguyên thư mục skill vào
scratch `.qwen/skills/<name>` hoặc `.opencode/skills/<name>`. Scripts/assets được
copy cùng; không dùng symlink nguồn nên container Docker có đủ nội dung qua mount
scratch sẵn có. Skills nguồn ở host cần truy cập được từ tiến trình LangGraph;
Compose không tự biến đường dẫn host ngoài mount thành đường dẫn trong container.
Copy được dọn cùng scratch kể cả CLI lỗi/timeout, không stage vào Git worktree và
không làm bẩn main. Không tự nhúng toàn bộ SKILL.md vào prompt; CLI tự discovery/load.

Đây là config lựa chọn skills của orchestrator, không phải file settings native
của Qwen/OpenCode. `enabled: false` không tắt skills user/global/bundled vốn có của
CLI. Empty paths và không có role được chọn sẽ không stage thêm skill.
Runner hợp nhất common paths với đường dẫn của role và gộp nguồn trùng nhau. Những hạn chế riêng của
phiên bản CLI đang dùng vẫn áp dụng. Initialize chỉ dùng ChatOpenAI nên không đọc
hai file skills này.

Tài liệu native: [Qwen Skills](https://qwenlm.github.io/qwen-code-docs/en/users/features/skills/),
[OpenCode Skills](https://opencode.ai/docs/skills/).


## Runtime INFO logging

`runtime/logging.py` cung cấp logger `orchestrator`, `logged_node()`, `operation()`
và `event()`. Wrapper node giữ đường gọi sync/async, RunnableConfig/callbacks và
kết quả state; không chèn thêm AIMessage hoặc nội dung vào prompt. Các node xử lý
trong subgraph role, các node điều phối root và task động trong DAG đều có log.

Các mốc chính:

| Event | Thông tin |
| --- | --- |
| `node.start`, `node.end` | Agent/node, task/thread, node_run_id, thời gian, status/assessment/số task |
| `llm.start`, `llm.end` | Purpose conversation/structured, model, endpoint origin, timeout, schema/method, kết quả |
| `cli.start`, `cli.end`, `cli.process_exit` | CLI/runtime/model, thời gian, exit code, kích thước input/output |
| `cli.prepared`, `cli.log_file` | Scratch/project được giao, số skills, đường dẫn log CLI |
| `git.operation.*`, `git.lock.*`, `git.merged` | Checkout/commit/review/merge, chờ/giữ/thả khóa và commit được merge |
| `task.reused`, `task.skipped` | Dùng lại việc đã merge hoặc bỏ qua do phụ thuộc thất bại |

Operation lỗi ghi event `.error` ở ERROR với error_type; cancel ghi `.cancelled`
ở WARNING. Lỗi đã được agent bắt và chuyển thành state vẫn có status/has_error ở
node.end. Không dump state, prompt, response body hoặc chuỗi exception. Conversation
LLM ghi usage_metadata nếu có; structured output chưa thu thập raw token usage.
Endpoint bỏ user/password/path/query; giá trị biến môi trường có tên chứa TOKEN,
API_KEY, PASSWORD, SECRET được che trong các field log.

Logger dùng handler riêng ra stderr, không sửa root logger hoặc bật debug log cho
HTTP client. `ORCHESTRATOR_LOG_LEVEL` hỗ trợ DEBUG/INFO/WARNING/ERROR/CRITICAL,
mặc định INFO. Cấu hình được đọc lại tại đầu node/CLI để nhận `.env` đã load sau
import. `ORCHESTRATOR_LOG_FILE` chọn file; nếu bỏ trống và có CLI_LOG_DIR thì dùng
`<CLI_LOG_DIR>/orchestrator.log`, đọc được từ Terminal Monitor. Nếu không có cả hai
thì chỉ stderr. File ngoài CLI_LOG_DIR không tự xuất hiện trong viewer.

ContextVar giữ riêng agent/node/task/thread khi chạy song song và được truyền qua
worker thread. thread_id lấy từ config configurable/metadata; node_run_id duy nhất
cho mỗi lần vào node. File runtime append, không tự rotate; stderr có thể xem qua
`docker compose logs -f team`. Không đổi cơ chế no_stream hoặc filter artifact.

Các async node gọi configure_logging qua asyncio.to_thread. QueueHandler chỉ đưa
record vào queue; QueueListener ghi stderr/file trên thread riêng và drain khi
shutdown. Constructor ChatOpenAI/structured model, build execution DAG và thao tác
Git/kiểm tra ancestor/file lock đều được chạy ngoài event loop. Khóa dùng flock
nonblocking với async sleep khi bận, không giữ event loop để chờ I/O. Test hồi quy
bật Blockbuster cho Initialize, logging file, DAG root, merge local và approved reuse.


## Bộ skills theo role đã cài

`two_phase.py:coding()` truyền `role=agent_name` vào `run_llm_cli()`. Runner gọi
`configured_skills(cli, role=role)` trên worker thread, stage riêng vào scratch
của invocation và ghi tên skills ở event `cli.prepared`. Không nhúng SKILL.md
vào prompt và không stage skill của role khác. `config/cli/qwen.yaml` và
`config/cli/opencode.yaml` đều có mapping mặc định:

- Plan: repository-discovery, dependency-planning, replan-recovery.
- Execute: scoped-implementation, systematic-debugging, verification-before-completion, ponytail.
- Reviewer: evidence-based-review, integration-regression, verification-before-completion.

Các skills scope/discovery/planning/review do dự án viết, phù hợp natural plan →
structured DAG và review-gated local merge. Skill `systematic-debugging` và
`verification-before-completion` được tải từ obra/superpowers; `ponytail` từ
DietrichGebert/ponytail. Thư mục `skills/vendor` giữ tài nguyên đi kèm và MIT LICENSE.
`skills/sources.json` ghi repository, commit pinned, upstream SKILL.md SHA-256,
local SHA-256 và ghi chú điều chỉnh. Không chạy installer/plugin/hooks của các
repo upstream; chỉ sử dụng các skill folders đã chọn.

Điều chỉnh bản vendored: Ponytail dùng full mode trong một Execute invocation;
không tự thay scope theo một chế độ hội thoại. Debugging không phụ thuộc skill
TDD chưa cài và trả architectural blocker về Plan. Verification tôn trọng review
chỉ đọc và không tự commit/push/merge. Cả ba vẫn cần tuân theo hợp đồng role và
hướng dẫn repository, không tạo workflow điều phối thứ hai.

Initialize không có CLI invocation. Caller trực tiếp có thể truyền role vào
run_llm_cli; không có role thì chỉ common paths. Khi cần đổi bộ skill theo backend,
sửa mapping roles trong đúng file config CLI hoặc override file qua biến *_SKILLS_CONFIG.


## Hợp đồng prompt và skills

Prompt role ở `config/*.yaml` xác định quyền và đầu ra; skills hỗ trợ thực hiện
trong giới hạn đó, không tự cấp quyền commit/merge/dispatch. Các ví dụ upstream
là kỹ thuật tham khảo, không phải chỉ dẫn chạy nếu vi phạm vai trò được giao.

| Role | Quyền và kết quả |
| --- | --- |
| Initialize | Hội thoại làm rõ; readiness quyết định bàn giao, không CLI/skills/code |
| Plan | Khảo sát chỉ đọc, natural plan/replan; không ghi plan file hoặc tự chạy agent |
| Execute | Sửa đúng checkout/task, Ponytail full và kiểm chứng; không tự commit/merge |
| Reviewer | Xác minh code/candidate, chạy checks; không sửa/revert source hoặc tạo test |
| Structured pass | Chuyển đổi/đánh giá báo cáo; không tự thực thi hoặc bịa bằng chứng |

Khi Git isolation bật, runtime commit sau Execute pass, tạo candidate trên nhánh
đích mới nhất, gọi Reviewer rồi mới merge local nếu review pass. Prompt phân biệt
standalone/shared mode, nơi không mặc định có candidate hoặc merge. Tên main trong
skills là cách gọi nhánh chính, không thay thế target_branch trong metadata.
Reviewer không áp dụng ví dụ red/green bằng cách revert code hoặc update snapshot;
kiểm tra không được làm bẩn candidate. Thiếu bằng chứng bắt buộc là blocker; thay
đổi tài liệu/cấu hình đơn giản không phải tạo test hình thức. CLI không có vòng
chờ tương tác: Execute/Reviewer báo blocker để Plan/runtime xử lý.

`PlanGraph` cho phép deserialize `nodes=[], edges=[]` đúng như structured prompt
khi kế hoạch bị chặn/không có task hợp lệ. `validate_plan_graph()` vẫn từ chối
nodes rỗng trước khi tạo artifact hoặc mạng Execute. Đây là tín hiệu dừng với
status failed, không phải kế hoạch thành công hoặc completed. Model không cần
bịa một node để thỏa minItems. Graph có task vẫn phải thỏa mọi ràng buộc DAG.


## ChatOpenAI viết phản hồi cho người dùng

`runtime/responses.py` là lớp diễn đạt gọi từ wrapper node trong logging.py.
Initialize, Plan, Execute và Reviewer tắt lớp này. Initialize trả lời trực tiếp;
Plan hiển thị tiến độ bằng code và một thông báo kèm artifact DAG đã validate.
Execute/Reviewer hiển thị các mốc bằng code, trạng thái cùng explanation từ
assessment; mỗi role còn một lượt CLI và một lần structured pass/fail. Báo cáo
CLI giữ trong state/log, không đưa vào chat. Execute pass chuyển Reviewer;
Reviewer pass chưa đồng nghĩa đã merge. Các wrapper task/network/execute_network
cũng tắt viết lại, kể cả thông báo tích hợp Git. Team announce_execution dùng
code; tổng kết và chuẩn bị technical replan của team vẫn dùng lớp diễn đạt.
Chỉ xử lý các AIMessage mới, không phải artifact-only, chưa có marker
`orchestrator_response.rewritten=true`. Mỗi message được thêm một call ChatOpenAI
plain text. Các node ở graph cha không viết lại message đã xử lý từ graph con.
Hợp đồng ID/name giữ nguyên; marker được lọc khỏi lịch sử gửi model.

`config/response.yaml` có prompt, timeout (60 giây), history_messages (16),
source_chars (16000) và thinking=false. Model/endpoint/key kế thừa options role,
DECISION_* hoặc QWEN_*. Constructor/config đọc trên worker thread. Phản hồi sinh
bằng plain text với streaming=False/disable_streaming=True; không cần thêm JSON
schema. Cả invoke/ainvoke đều gắn no_stream để không lộ báo cáo trước khi viết lại.

Input gồm lịch sử đã lọc artifact, nhãn role, draft và facts thật (status, báo cáo,
assessment, lỗi, clarification, kết quả task). Lịch sử được serialize thành dữ
liệu dưới một system prompt diễn đạt, không đưa SystemMessage trong lịch sử thành
quyền mới. Lấy lịch sử sau preprocessing để artifact không chiếm số lượt. Draft và
report bị giới hạn source_chars, có cờ source_truncated; credentials được che.
Prompt yêu cầu dùng ngôn ngữ/mức độ kỹ thuật phù hợp, không bịa nguyên nhân,
không coi một bước pass là đã merge hoặc toàn workflow hoàn tất.

Field output/natural_plan/assessment/results giữ báo cáo/quyết định kỹ thuật gốc.
Initialize không dùng lớp viết lại; structured payload có natural_response là
câu trả lời hiển thị trực tiếp để đánh giá readiness cùng lịch sử hội thoại. Lớp phản hồi không đổi status, gọi retry/replan hay sửa
kết quả merge. Khi lỗi, chỉ bỏ phần lời chưa viết được, ghi response_error và log;
không thay bằng raw report hoặc câu mẫu. Artifact tất định vẫn được giữ.

Plan phát phần lời kế hoạch và artifact ở cùng update; lớp phản hồi tạo phần lời,
rồi attach_artifacts() ghép payload vào AIMessage. Artifact-only bàn giao không
cần gọi model. Đầu vào model phản hồi lẫn các agent tiếp theo đều bỏ payload này,
nhưng người dùng vẫn thấy đầy đủ phần lời và artifact trong lịch sử state.


## Duyệt kế hoạch trước Execute

Trước lần chấp nhận đầu tiên, mỗi Plan thành công dẫn tới
`request_plan_feedback` gọi `interrupt()` với câu hỏi
và DAG hiện tại. Node này không chạy CLI/model, nên resume không lặp tác vụ.
Caller gửi `Command(resume="phản hồi người dùng")` trên cùng thread/checkpointer.
`assess_plan_approval` dùng cấu hình `config/plan_approval.yaml` tạo
`PlanApprovalDecision`: `short_explanation` trước rồi `ready_for_execute`.
Đồng ý thì chuyển Execute; yêu cầu sửa/chưa rõ thì tăng `plan_revision`, gửi
kế hoạch trước, phản hồi và lý do đánh giá cho Plan rồi hỏi duyệt lại.
Lỗi structured approval dừng failed và không chạy Execute. Chỉnh theo góp ý
không dùng ngân sách `max_replans`. Sau khi `plan_approval.ready_for_execute`
là true, technical replan do lỗi chạy thẳng tới Execute, không interrupt hoặc
gọi lại model đánh giá duyệt. Chỉ sửa trong phạm vi đã chấp nhận; hoàn tất hoặc
hết `max_replans` thì tổng kết và END, không tự mở vòng mới. `dispatch_execute=False` giữ chế độ chỉ lập kế hoạch, không interrupt.
Chạy Python trực tiếp truyền `checkpointer` vào `build_graph()`; graph mặc định
trên LangGraph Server dùng checkpoint do server quản lý. Xem ví dụ resume trong README.


Execute/Reviewer dùng `compact_output=True`, `rewrite_responses=False` trong
`build_assessed_agent()`. Lỗi CLI/blocker vẫn không thể được assessment nâng
thành pass; lỗi structured output vẫn dừng failed. Cơ chế review, merge và
replan không thay đổi khi bỏ các lượt viết lại thông báo.


## Mốc review theo DAG và streaming

`review_schedule()` gán mỗi task cho đúng một mốc: ưu tiên mốc reachable đầu
trong thứ tự topo, rồi đến review cuối. Nhánh có ancestor chung không review
ancestor đó hai lần: mốc sau chờ mốc đang sở hữu ancestor. Không tạo barrier
chung cho các nhánh không liên quan; scheduler async chỉ chờ prerequisite của
từng job, tôn trọng max_concurrency nếu caller cấu hình.

Reviewer nhận `task.context` có tasks/execution_results và execution_result có
task_results của toàn nhóm; integration chứa diff candidate tích hợp. Một lỗi
review làm cả nhóm chưa được duyệt. Task đã duyệt có thể được tái dùng khi replan;
khi isolation bật, merged_commit phải còn ở nhánh đích. Group thất bại không
được thêm vào approved_work. Với shared mode, các Execute vẫn dùng cùng workspace;
chỉ isolation cung cấp candidate Git riêng và kiểm soát merge.

Network bao gồm node điều phối run_review_network. Custom stream phát
`{"node": "task_id hoặc mốc review", "update": {...}}`; update chứa results và
messages theo cùng hợp đồng ID. Ví dụ nối tiếp A→B→C chỉ có một Reviewer;
A/B song song→J có một Reviewer cho A/B trước J, rồi Reviewer cuối cho J.

Trên Python 3.11+, scheduler dùng LangGraph task cho từng Execute và mốc review,
đăng ký theo thứ tự topo cố định. Khi có checkpointer, resume dùng lại kết quả
job đã lưu, không chạy lại CLI/review đã hoàn tất; kết quả cached cũng được áp
dụng theo dependency. Python 3.10 chạy scheduler async nhưng không có checkpoint
từng job vì LangGraph không hỗ trợ async functional task trên phiên bản đó.
