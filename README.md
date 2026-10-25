# Multi-Agent Qwen Code Orchestrator

MVP điều phối sáu loại agent bằng LangGraph Agent Server và Qwen Code, chạy trong **một container**. Dispatcher là bên duy nhất cập nhật task chính thức; Dashboard được gắn vào cùng Agent Server, không có PostgreSQL, Redis hay backend điều phối riêng.

Yêu cầu gốc: [docs/requirements.md](docs/requirements.md). Thiết kế và giới hạn: [docs/architecture.md](docs/architecture.md).

## Chạy bằng Docker

Cần Docker/Compose và một endpoint OpenAI-compatible mà container truy cập được.

```bash
cp .env.example .env
# Sửa QWEN_BASE_URL, QWEN_MODEL, QWEN_API_KEY và ORCHESTRATOR_TOKEN trong .env.
# DASHBOARD_TOKEN chỉ cần nếu sử dụng dashboard; phải khác ORCHESTRATOR_TOKEN.
docker compose up --build -d
docker compose logs -f
```

Chat trực tiếp với graph `communicator` qua LangGraph SDK, dùng **ORCHESTRATOR_TOKEN**. Agent Server nghe port 2024 bên trong container; dùng host/port được map trong `docker-compose.yml` khi kết nối từ máy host. `DASHBOARD_TOKEN` có thể để trống khi chỉ dùng SDK; các API dashboard khi đó bị khóa.

Ở đầu hội thoại, Communicator liệt kê các project để chọn bằng số thứ tự, tên hoặc ID. Nếu chưa có project, hoặc bạn nói **tạo project mới**, hệ thống tự sinh ID, tạo thư mục `WORKSPACE_ROOT/<project_id>` (mặc định `/workspace/<project_id>`) và khởi tạo Git cùng commit ban đầu. Lựa chọn được lưu vào `state.project_id` của thread. Execute workers tạo worktree riêng; Verify dùng worktree integration. API đăng ký repository có sẵn vẫn được giữ cho tích hợp.

Sau khi dependencies/image/model đã được chuẩn bị, runtime không cần Internet công cộng; `QWEN_BASE_URL` và endpoint Decision (nếu cấu hình riêng) phải tiếp tục truy cập được. Image build vẫn cần các registry hệ điều hành, Python và npm. API key không được lưu vào image, prompt hay repository.

## Phát triển ngoài container

Python 3.12+, Node.js 22+, Git và `uv`:

```bash
uv sync --frozen
npm install --global @qwen-code/qwen-code@0.26.1
cp .env.example .env
# Cấu hình .env; dùng DATA_ROOT và WORKSPACE_ROOT có quyền ghi.
set -a
. ./.env
set +a
uv run python -m orchestrator.supervisor
```

Supervisor khởi động `langgraph dev --host 0.0.0.0 --port 2024 --no-browser --no-reload --n-jobs-per-worker 10`. Dispatcher chạy trong lifespan của Agent Server với Store từ Runtime; sau readiness, Supervisor khởi động từng worker process. Dừng bằng SIGTERM/Ctrl-C để lưu trạng thái phát triển và kết thúc theo thứ tự.

```bash
uv run pytest -q
curl --fail http://127.0.0.1:2024/ok
```

Các kiểm thử tự động dùng Qwen subprocess, phản hồi HTTP LLM giả lập và Git repository tạm để xác minh protocol, quyền, DAG và cơ chế runtime. Chúng không xác minh chất lượng đầu ra mô hình nội bộ của bạn. Hãy chạy một yêu cầu thực với endpoint đã cấu hình trước khi coi luồng LLM end-to-end được nghiệm thu.

## Communicator và hội thoại

`langgraph.json` chỉ expose graph `communicator`. Gửi tin nhắn trực tiếp vào graph:

```json
{"messages": [{"role": "user", "content": "Tôi muốn xây dựng portal cho khách hàng"}]}
```

Client chỉ cần gửi **tin nhắn mới** trong `messages` trên cùng thread; không cần
truyền `project_id`, `task`, `cwd` hoặc gửi lại assistant messages. Graph lưu cả
câu trả lời vào state và phát stream khi có câu trả lời mới. Ví dụ với async SDK:

```python
import os
from langgraph_sdk import get_client

client = get_client(url=os.environ["LANGGRAPH_URL"], api_key=os.environ["ORCHESTRATOR_TOKEN"])
thread = await client.threads.create()

async def chat(text):
    state = await client.runs.wait(thread["thread_id"], "communicator", input={
        "messages": [{"role": "user", "content": text}]
    })
    print(state["messages"][-1]["content"])
    return state

await chat("Xin chào")        # Liệt kê project; nếu danh sách trống thì tự tạo.
await chat("1")               # Chọn project số 1 khi vừa nhận danh sách.
await chat("Thêm đăng nhập")  # Yêu cầu cho project đã chọn.
```

Nói **danh sách dự án** để chọn lại, hoặc **tạo project mới** để chuyển sang project
mới trong cùng thread. Sau khi chọn, Communicator thông báo project và câu hỏi
đang chờ (nếu có); tin nhắn tiếp theo tiếp tục quy trình. Lịch sử trao đổi của
project được chuyển cho worker để không mất yêu cầu khi bắt đầu execution chain mới.

Graph đọc catalog/task/approval qua `runtime.store` được LangGraph inject; mọi ghi
project, task và conversation chính thức vẫn đi qua Dispatcher. Khi chạy local,
truyền Store vào `builder.compile(store=store)` và dùng checkpointer để giữ state
giữa các lượt. Agent Server tự cung cấp Store/checkpoint khi dùng cùng thread.
Theo dõi kết thúc khi chain CLOSED, cần người dùng trả lời, hoặc bị chặn. Nói
**tiến độ** hoặc **status** để xem chain hiện tại mà không tạo task mới. Trả lời
câu hỏi làm rõ tiếp tục cùng chain qua `human_reply`; với quyết định kỹ thuật,
trả lời rõ **đồng ý** hoặc **từ chối**. Đóng kết nối không hủy task nền.

Communicator worker chạy hai lượt model: tạo câu trả lời hội thoại, rồi tạo
`TaskGraphOutput` từ lịch sử và bản trả lời đó. Khi đủ rõ, Dispatcher kiểm tra và
tạo task Analyze; nếu cần hỏi thêm, worker được nhả để chờ người dùng. Khi hết
thời gian chờ, hệ thống vẫn tiếp tục sang Analyze và lưu thông báo timeout cùng
các giả định. Khi lượt chat đã kết thúc chờ trả lời, có thể nhắn **tiến độ** để đọc
cập nhật và các thông báo timeout/giả định đã lưu.

Supervisor tạo một queue kết quả chung và một queue nhận việc cho mỗi worker
bằng `multiprocessing.Queue`, rồi khởi động các worker bằng
`multiprocessing.Process` (spawn). Dispatcher là một async task trong lifespan
của Agent Server, nhận `Runtime` và dùng chung `runtime.store` với Communicator.

```text
Dispatcher đọc/ghi Store
    → cấp task + context → queue của worker
    → worker queue.get() → invoke graph local → queue kết quả chung
    → Dispatcher kiểm tra, lưu kết quả và giao task tiếp theo
```

**Chỉ Dispatcher ghi Store; Communicator chat đọc `runtime.store` để báo tiến độ.**
Dispatcher dựng context, kiểm tra dependencies,
claim task và cấp lease trước khi giao việc. Worker không poll Store, không chờ
receipt và không gọi LangGraph SDK. Worker chờ bằng `queue.get()`, log bắt đầu/kết
thúc/lỗi, rồi chạy graph ngay trong process của mình. Mỗi graph run xử lý một task
và kết thúc; vòng chờ tiếp tục cho tới khi nhận sentinel shutdown.

Các queue chỉ nằm trong RAM. Sau restart, Dispatcher đọc task còn lưu trong Store,
phục hồi lease hết hạn và giao lại việc còn hợp lệ. Mã lượt chạy (`task_attempt`)
giúp từ chối heartbeat/kết quả cũ của lượt đã hết lease. Giới hạn Qwen toàn hệ thống
được Dispatcher kiểm tra khi cấp việc; Communicator không chiếm slot Qwen.

Agent Server chỉ expose Communicator và phục vụ Dashboard. Supervisor mở một
`multiprocessing.Manager` bridge trên loopback để Dispatcher trong Agent Server
nhận kết quả và gửi việc qua các queue worker. Dashboard gửi yêu cầu tới cùng
Dispatcher qua bridge; không mở Store client.
Chạy qua Supervisor để có bridge; graph chat `messages` vẫn dùng được riêng.

Dispatcher và Communicator đều dùng Store từ Runtime, không gọi HTTP Store API.
`StoreRecords` chỉ chuyển native Store Items thành dictionary cho engine hiện
có, không tạo client hay Store mới. Khi shutdown, Supervisor đợi worker flush
kết quả, gửi sentinel dừng Dispatcher rồi mới dừng Agent Server và bridge.

Chưa triển khai hủy task hoặc trạng thái `CANCEL`.

Yêu cầu từ Dashboard tạo task Communicator thực sự. Agent này gọi LLM với
`prompts/communicator.md`, đứng về phía người dùng để làm rõ mục tiêu, phạm vi và
kết quả mong muốn. Task Communicator chỉ gọi ChatOpenAI một lần với lịch sử
`messages` và metadata của task để trả `TaskGraphOutput` cho Dispatcher.
Communicator chọn giao cho chính nó, hỏi human bằng `human_request`/`HUMAN_APPROVAL`,
hoặc bàn giao bản tóm tắt yêu cầu cho Analyze.

Tự giao và hỏi human đều dùng `backward_count` chung của execution chain, giới hạn
bởi `MAX_BACKWARD_DELEGATIONS`; không có bộ đếm hỏi riêng. Trả lời không reset
chain/budget. Khi chờ human, worker được giải phóng. Sau 30 phút mặc định, approval
chuyển `TIMED_OUT` và Dispatcher tạo task Communicator tiếp tục trong cùng chain.
Communicator chuyển sang Analyze với giả định nêu rõ khi hết thời gian hoặc hết
ngân sách; không coi im lặng là phê duyệt. Người dùng từ chối thì không tự tiếp tục.

State Communicator có `messages: Annotated[list[AnyMessage], add_messages]`.
Dispatcher journal lịch sử theo chain ở namespace `conversations`; scoped context
do Dispatcher dùng chỉ cấp lịch sử cho Communicator, Analyze và Finalize. Các agent còn lại nhận brief
và shared contracts. Dashboard hiển thị hội thoại cho người dùng đã xác thực.
Đây là giới hạn cấp context, không phải sandbox filesystem giữa các process.

Analyze chỉ được giao cho Communicator hoặc Plan & Delegate. JSON Schema của
Decision pass và Dispatcher cùng áp dụng quy tắc này; câu hỏi cho human phải qua
task Communicator. Nếu LLM chọn sai nơi nhận, runtime yêu cầu sửa lại Decision
pass với cùng kết quả Work pass. Số lượt quyết định tối đa theo `max_attempts` của
task (mặc định 3, tính cả lượt đầu); các đề xuất bị loại không tạo task hoặc tiêu
tốn backward budget. Nếu vẫn sai, lượt chạy báo lỗi để xử lý theo retry kỹ thuật.

## Cấu hình

Với HTTPS bị `unable to get local issuer certificate`, kiểm tra server đã gửi
đủ intermediate chain chưa. Có thể bổ sung intermediate đã xác minh cùng các CA
gốc đang tin cậy vào `.local/certs/ca-bundle.pem`, rồi cấu hình cả hai runtime:

```dotenv
SSL_CERT_FILE=.local/certs/ca-bundle.pem
NODE_EXTRA_CA_CERTS=.local/certs/ca-bundle.pem
```

HTTPX/ChatOpenAI đọc `SSL_CERT_FILE`; Qwen Code dùng `NODE_EXTRA_CA_CERTS`.
QwenRunner đổi đường dẫn CA tương đối thành tuyệt đối trước khi chạy trong
worktree khác. Compose mount `.local/certs` vào `/app/.local/certs` ở chế độ chỉ
đọc. Bundle là cấu hình local, đã được gitignore; cần tạo file trên máy chạy.
Khởi động lại Supervisor/container sau khi đổi biến CA.

Thinking được tắt cho cả hai pass. ChatOpenAI gửi `enable_thinking=false` và
`chat_template_kwargs.enable_thinking=false` qua `extra_body`. Qwen Code nhận
cùng các tham số trong `model.generationConfig.extra_body`, cùng `reasoning=false`,
qua file `~/.qwen/settings.json` trong HOME riêng của từng Work session.
Endpoint/model cần hỗ trợ tham số tương ứng để áp dụng chế độ không thinking.

Với các agent kỹ thuật, Work pass chạy Qwen Code CLI. Decision pass gửi role prompt, task/context được cấp
và kết quả Work pass sang `langchain_openai.ChatOpenAI`, gọi bất đồng bộ bằng
`ainvoke()`. Decision đặt `streaming=False` và `disable_streaming=True` để tắt stream,
kể cả khi LangGraph có callback streaming. Tag `nostream` ngăn phản hồi Decision
phát ra qua luồng `messages` của LangGraph. Riêng chat/báo tiến độ Communicator bật
stream token và không dùng tag `nostream`; thinking vẫn tắt. Decision dùng Chat Completions với
`response_format=json_schema` (`strict=False`); endpoint nội bộ cần hỗ trợ chế độ
này. Runtime vẫn kiểm tra `TaskGraphOutput`, danh tính source và quyền giao task.

Đặt `DECISION_BASE_URL`, `DECISION_API_KEY`, `DECISION_MODEL` để dùng LLM nội bộ
cho Communicator và Decision; từng biến bỏ trống dùng giá trị `QWEN_*` tương ứng. Timeout mặc
định 120 giây qua `DECISION_TIMEOUT_SECONDS`. SDK không tự retry; lỗi đi qua retry
kỹ thuật hiện có. Dispatcher giới hạn số task kỹ thuật đang hoạt động trên toàn hệ thống.

Skills của từng Work pass được khai báo trong [config/work_pass_skills.json](config/work_pass_skills.json).
Mặc định `ANALYZE` dùng `gitlab-cli`; các role khác có danh sách rỗng. Thêm skill
bằng cách khai báo đường dẫn `SKILL.md` trong `skills` rồi gán tên đó vào `agents`:

```json
{
  "version": 1,
  "skills": {"gitlab-cli": "../skills/gitlab-cli/SKILL.md"},
  "agents": {"ANALYZE": ["gitlab-cli"], "EXECUTE": []}
}
```

Đường dẫn skill tương đối được tính từ thư mục chứa config; role không được khai
báo tương đương danh sách rỗng. Có thể đặt `WORK_PASS_SKILLS_CONFIG` tới config riêng
(mount cả config và các skill vào container nếu dùng Docker). Runtime đọc config
cho mỗi lượt chạy; skill thiếu/sai cấu hình báo lỗi thay vì bỏ qua âm thầm.

Orchestrator nạp nội dung skill trực tiếp vào **Work prompt**, giữ Qwen `--safe-mode`;
đây không phải native skill discovery của Qwen. Decision pass chỉ nhận role prompt,
task/context và kết quả Work pass, không được nạp bộ skill hay nhận GitLab credentials.
Theo [tài liệu Qwen](https://github.com/QwenLM/qwen-code/blob/main/docs/users/support/troubleshooting.md),
safe mode vô hiệu hóa cơ chế custom skills tự động.

[Skill GitLab CLI](skills/gitlab-cli/SKILL.md) hướng dẫn Analyze đọc issue, MR, diff
và CI evidence qua `glab`, không thực hiện thay đổi GitLab. Dockerfile lấy binary
`glab` từ image chính thức phiên bản `v1.95.0`; khi chạy local cần cài `glab` trên PATH
(ví dụ `brew install glab` trên macOS). Đặt `GITLAB_HOST` cho instance cần truy cập và
`GITLAB_TOKEN` khi cần xác thực; runtime chỉ truyền chúng cho Work pass được gán
skill `gitlab-cli`, không đọc credential config từ HOME của người chạy. Cách xác thực
bằng biến môi trường được mô tả trong [tài liệu GitLab](https://docs.gitlab.com/cli/auth/login/).
Skill là hướng dẫn sử dụng, không thay đổi `yolo` hay khóa shell thành read-only;
quyền truy cập GitLab còn phụ thuộc token được cấp.

| Biến | Ý nghĩa |
| --- | --- |
| `ORCHESTRATOR_TOKEN` | Token API nội bộ Agent Server và SDK chat proxy; không phát cho browser |
| `DASHBOARD_TOKEN` | Token riêng cho API giao diện |
| `QWEN_BASE_URL`, `QWEN_API_KEY`, `QWEN_MODEL` | Endpoint/model OpenAI-compatible cho Qwen Code |
| `DECISION_BASE_URL`, `DECISION_API_KEY`, `DECISION_MODEL` | LLM nội bộ cho Decision; từng giá trị mặc định theo `QWEN_*` |
| `DECISION_TIMEOUT_SECONDS` | Timeout Decision LLM; mặc định 120 giây |
| `WORK_PASS_SKILLS_CONFIG` | Đường dẫn config skills riêng; bỏ trống để dùng config đi kèm |
| `GITLAB_HOST`, `GITLAB_TOKEN` | Host/token GitLab cho Work pass được gán skill `gitlab-cli` |
| `MAX_EXECUTE_WORKERS` | 1–5 Execute workers |
| `MAX_QWEN_SESSIONS` | 1–5 lượt xử lý đồng thời; mỗi lượt giữ slot qua Work và Decision |
| `MAX_BACKWARD_DELEGATIONS` | Tối đa 3 lần quay ngược trên execution chain |
| `MAX_TECHNICAL_RETRIES` | Retry kỹ thuật riêng; mặc định 3 |
| `HUMAN_RESPONSE_TIMEOUT_SECONDS` | Thời gian chờ human của Communicator; mặc định 1800 giây |
| `TASK_LEASE_SECONDS` | Thời hạn lease task; heartbeat gia hạn |
| `WORKSPACE_ROOT` | Git repositories, worktrees và artifacts |
| `DATA_ROOT` | Journal Dispatcher và dữ liệu runtime phát triển |

Qwen Code được pin `0.26.1`. Adapter map biến `QWEN_*` sang provider OpenAI của CLI (`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`) trong child process; đây không phải tên biến CLI giả định hỗ trợ trực tiếp.

## Giới hạn cần biết

- `langgraph dev` là runtime phát triển. Store/checkpoint dùng file local của phiên bản đã pin; volume và orderly restart không đồng nghĩa transaction bền vững hoặc khả năng phục hồi mọi crash.
- Dispatcher có journal/replay và request idempotency; không tuyên bố exactly-once khi mọi process/máy dừng đột ngột.
- Scoped context và token tách biệt ngăn truy cập API/private ngoài luồng cho phép. Code do Qwen chạy trong cùng container có cùng quyền filesystem; worktree không phải sandbox bảo mật. Chỉ chạy repository và yêu cầu được tin cậy.
- Hiện thực có thể kiểm thử không cần key; gọi mô hình thật, E2E ứng dụng đích và nghiệm thu cuối vẫn phụ thuộc endpoint/model, repository và tiêu chí của dự án được giao.
