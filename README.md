# Multi-Agent Qwen Code Orchestrator

MVP điều phối sáu loại agent bằng LangGraph Agent Server và Qwen Code, chạy trong **một container**. Dispatcher là bên duy nhất cập nhật task chính thức; Dashboard được gắn vào cùng Agent Server, không có PostgreSQL, Redis hay backend điều phối riêng.

Yêu cầu gốc: [docs/requirements.md](docs/requirements.md). Thiết kế và giới hạn: [docs/architecture.md](docs/architecture.md).

## Chạy bằng Docker

Cần Docker/Compose và một endpoint OpenAI-compatible mà container truy cập được.

```bash
cp .env.example .env
# Sửa QWEN_BASE_URL, QWEN_MODEL, QWEN_API_KEY và hai token trong .env.
# ORCHESTRATOR_TOKEN và DASHBOARD_TOKEN phải khác nhau, đủ ngẫu nhiên.
docker compose up --build -d
docker compose logs -f
```

Dashboard nằm ở đường dẫn `/dashboard` trên port 2024. Compose mặc định chỉ bind port ra loopback của máy Docker; đổi binding trong môi trường mạng nội bộ có kiểm soát nếu cần. Nhập **DASHBOARD_TOKEN** vào giao diện. Không cung cấp ORCHESTRATOR_TOKEN cho trình duyệt.

Project phải trỏ tới một repository Git có commit bên trong `/workspace` của container. Đưa repository vào volume hoặc mount một thư mục Git có sẵn tại `/workspace/repositories/<project_id>`. Tạo project trên Dashboard bằng đường dẫn đó, rồi gửi yêu cầu. Execute workers tạo worktree riêng; Verify dùng worktree integration. Repository rỗng cần một commit ban đầu trước khi cấp worktree.

Sau khi dependencies/image/model đã được chuẩn bị, runtime không cần Internet công cộng; `QWEN_BASE_URL` phải tiếp tục truy cập được. Image build vẫn cần các registry hệ điều hành, Python và npm. API key không được lưu vào image, prompt hay repository.

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

Supervisor khởi động `langgraph dev --host 0.0.0.0 --port 2024 --no-browser --no-reload --n-jobs-per-worker 10`, chờ readiness, rồi chạy Dispatcher và từng worker. Dừng bằng SIGTERM/Ctrl-C để lưu trạng thái phát triển và kết thúc subprocess theo thứ tự.

```bash
uv run pytest -q
curl --fail http://127.0.0.1:2024/ok
```

Các kiểm thử tự động dùng Qwen subprocess giả lập hoặc Git repository tạm để xác minh protocol, quyền, DAG và cơ chế runtime. Chúng không xác minh chất lượng đầu ra mô hình nội bộ của bạn. Hãy chạy một yêu cầu thực với endpoint đã cấu hình trước khi coi luồng LLM end-to-end được nghiệm thu.

## Communicator và hội thoại

Yêu cầu từ Dashboard tạo task Communicator thực sự. Agent này gọi LLM với
`prompts/communicator.md`, đứng về phía người dùng để làm rõ mục tiêu, phạm vi và
kết quả mong muốn; không khám phá repository hoặc dùng công cụ sửa code.
Communicator chọn giao cho chính nó, hỏi human bằng `human_request`/`HUMAN_APPROVAL`,
hoặc bàn giao bản tóm tắt yêu cầu cho Analyze.

Tự giao và hỏi human đều dùng `backward_count` chung của execution chain, giới hạn
bởi `MAX_BACKWARD_DELEGATIONS`; không có bộ đếm hỏi riêng. Trả lời không reset
chain/budget. Khi chờ human, worker được giải phóng. Sau 30 phút mặc định, approval
chuyển `TIMED_OUT` và Dispatcher tạo task Communicator tiếp tục trong cùng chain.
Communicator chuyển sang Analyze với giả định nêu rõ khi hết thời gian hoặc hết
ngân sách; không coi im lặng là phê duyệt. Người dùng từ chối thì không tự tiếp tục.

State Communicator có `Messages: Annotated[list[AnyMessage], add_messages]`.
Dispatcher journal lịch sử theo chain ở namespace `conversations`; scoped context
chỉ cấp lịch sử cho Communicator, Analyze và Finalize. Các agent còn lại nhận brief
và shared contracts. Dashboard hiển thị hội thoại cho người dùng đã xác thực.
Đây là giới hạn cấp context, không phải sandbox filesystem giữa các process.

Analyze chỉ được giao cho Communicator hoặc Plan & Delegate. JSON Schema của
Decision pass và Dispatcher cùng áp dụng quy tắc này; câu hỏi cho human phải qua
task Communicator. Nếu Qwen chọn sai nơi nhận, runtime yêu cầu sửa lại Decision
pass với cùng kết quả Work pass. Số lượt quyết định tối đa theo `max_attempts` của
task (mặc định 3, tính cả lượt đầu); các đề xuất bị loại không tạo task hoặc tiêu
tốn backward budget. Nếu vẫn sai, lượt chạy báo lỗi để xử lý theo retry kỹ thuật.

## Cấu hình

| Biến | Ý nghĩa |
| --- | --- |
| `ORCHESTRATOR_TOKEN` | Token nội bộ cho Store/graph SDK; không phát cho browser |
| `DASHBOARD_TOKEN` | Token riêng cho API giao diện |
| `QWEN_BASE_URL`, `QWEN_API_KEY`, `QWEN_MODEL` | Endpoint/model OpenAI-compatible cho Qwen Code |
| `MAX_EXECUTE_WORKERS` | 1–5 Execute workers |
| `MAX_QWEN_SESSIONS` | 1–5 phiên Qwen đồng thời trên Agent Server |
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
