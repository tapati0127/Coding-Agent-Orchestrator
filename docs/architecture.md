# Kiến trúc MVP

## Process và dữ liệu

Entry point là `graph.py` ở root. `langgraph.json` expose `./graph.py:graph`,
chính là graph Communicator nhận message human. Chạy trực tiếp `langgraph dev` từ root; cấu hình tự nạp `.env`, không cần
launcher riêng. Lifecycle trong
`orchestrator.server` lấy Runtime và gọi `graph_lifespan` của `graph.py`.

`GraphRuntime` khai báo toàn bộ queue bằng `multiprocessing` của Python với start
method `spawn`: `dispatcher_queue`, `agent_queues`, `store_requests`, `store_responses`.
Import module không tạo queue/process. Communicator xử lý task bằng vòng `agent_loop`
trong process chính cùng endpoint SDK. Analyze, Plan & Delegate, Execute,
Verify & Integrate và Finalize dùng cùng vòng `while True` ở process riêng.
Dispatcher có StateGraph riêng; node dispatch chạy `DispatcherLoop.run`, đọc
queue liên tục, xử lý event rồi kiểm tra task sẵn sàng để giao.

```text
Main process: Human → Communicator → dispatcher_queue
                                    ↓
Dispatcher process: validate → claim → agent_queues[worker_id]
                                    ↓
Technical processes: graph run → heartbeat/result/failure → dispatcher_queue
```

Dispatcher là bên duy nhất quyết định ghi Store: kiểm tra DAG/dependencies,
owner/attempt, lease, giới hạn concurrency và kết quả rồi ghi journal/receipt.
Worker nhận snapshot `task`, `project`, `context`, `predecessors`, `thread_id`;
không mở SDK hay poll Store. Communicator gọi ChatOpenAI trực tiếp; năm role
kỹ thuật chạy Work qua Qwen Code và Decision qua ChatOpenAI.

Native Store do Runtime inject ở main process không được pickle hoặc tạo bản sao
trong process Dispatcher. `QueueStoreRecords` gửi RPC qua queue tới `serve_store`
ở main process; service gọi `StoreRecords` để thao tác trên đúng native Store.
RPC chỉ hỗ trợ get/put/delete/search, tuần tự trong Dispatcher, kiểm tra request ID
và có timeout. Exception trả về loại lỗi thay vì nội dung có thể chứa dữ liệu riêng.
Communicator đọc cùng Store qua `runtime.store.aget/asearch` để theo dõi chain.
Không có Manager bridge hoặc HTTP Store client trong runtime này.

Giới hạn Qwen toàn hệ thống nằm ở Dispatcher; Communicator không chiếm slot.
Semaphore trong QwenRunner có phạm vi một process. Queue không lưu bền; task,
journal và lease vẫn được phục hồi từ Store. Kết quả attempt cũ bị từ chối.
File lock giữ đúng một Dispatcher cho cùng data directory.

Shutdown gửi sentinel cho agent, đợi kết quả flush, dừng Dispatcher rồi mới dừng
Store service. Worker chết được restart theo budget; Dispatcher/Store service/
vòng Communicator chết làm server dừng. Khởi động Dispatcher lỗi làm server không
ready. Các thao tác process/queue và filesystem chạy ngoài event loop khi cần.

Communicator chỉ gọi model báo tiến độ khi snapshot đổi. Lượt theo dõi kết thúc
khi chain CLOSED, chờ human hoặc không còn task active sau nhiều lần đọc ổn định.
Hủy stream không hủy task; CANCEL chưa triển khai.

Communicator dùng `backward_count` chung khi tự giao hoặc hỏi human, không có bộ đếm hỏi riêng. Approval của Communicator có deadline (mặc định 1800 giây). Trả lời hoặc timeout tạo task Communicator trong cùng chain qua journal/receipt hiện có; timeout ghi SystemMessage, không giả tạo HumanMessage hay phê duyệt. Khi timeout hoặc hết ngân sách, Communicator bàn giao Analyze với giả định công khai. Từ chối không tiếp tục. Task hỏi đã DONE/SUCCESS vì lượt giao tiếp hoàn tất; không giữ lease khi chờ human.

`CommunicatorState.messages` là danh sách `AnyMessage` dùng reducer `add_messages`. Lịch sử được journal theo chain ở `("coding", project_id, "conversations")`, không nằm trong shared context/task description. Dispatcher dùng scoped adapter để cấp lịch sử cho Communicator, Analyze, Finalize. Finalize dùng nó đối chiếu nghiệm thu với mong muốn người dùng; các role còn lại chỉ nhận brief/contract. SDK đọc câu trả lời từ thread state hoặc stream.

## HTTP và quyền

Work pass có bộ skills riêng theo `config/work_pass_skills.json` (override bằng
`WORK_PASS_SKILLS_CONFIG`). Runtime nạp các file SKILL.md đã cấu hình vào prompt
trong thread chuẩn bị, không nạp từ task/model output và không tắt Qwen safe mode.
GitLab credentials chỉ vào subprocess Work có skill `gitlab-cli`; Decision pass
và các lần sửa quyết định chỉ gửi task/context đã lọc, role prompt và kết quả Work
đến LLM, không có tool hoặc bộ skill Work. Qwen dùng HOME riêng cho Work;
Decision dùng HTTP client riêng cho mỗi invocation và đóng khi hoàn tất/lỗi/hủy.
Cả hai pass ghi log private đã che credentials; Decision chỉ phát sự kiện hoàn tất,
không stream token. Timeout và lỗi LLM đi qua cơ chế retry kỹ thuật hiện có.
Skill không thay thế quyền công cụ, sandbox hoặc quyền token GitLab.

Client dùng LangGraph SDK gọi graph communicator với `stream_subgraphs=True` và
modes `messages-tuple/custom`; root graph chứa subgraph communicate để token mang
namespace nguồn. Built-in Store/thread/run API dùng `ORCHESTRATOR_TOKEN`. Đây là
token runtime có quyền quản trị; chỉ cấp cho client được tin cậy. Client gửi tin
nhắn mới trên cùng thread để tiếp tục lịch sử. Đóng stream với `on_disconnect=cancel`
dừng lượt theo dõi; task nền vẫn do Dispatcher quản lý.

Communicator stream tiến độ công khai, không stream scratchpad hay raw subprocess
output. Patch/report chỉ được đưa vào context qua shared artifact references đã
được Dispatcher chấp nhận và kiểm tra scope; file trên disk không tự động thành
tài liệu public. Shared promotion là đề xuất phải được Dispatcher kiểm tra.

Private namespace là phân vùng logic, không phải ACL. Dispatcher dùng ScopedContext dựng shared context và private đúng owner; không đưa SDK Store credentials vào Qwen environment. Một container không bảo vệ được dữ liệu khỏi code độc hại cùng quyền user.

## Git

Execute workers có worktree riêng theo project/worker; integration có worktree riêng. Giữ patch/commit trước khi giải phóng worker. Không xoá worktree bẩn, không force-push. Verify cherry-pick/merge đầu ra để chuẩn bị baseline đã tích hợp trước khi task downstream cần code được chạy. Conflict phải xuất hiện như lỗi/rework có thể quan sát.

## Persistence

Package/runtime và toàn bộ dependency được khóa bằng `uv.lock`; Qwen CLI được pin trong Dockerfile. Runtime `langgraph dev` sử dụng `.langgraph_api` dưới thư mục chạy, ở thư mục chạy khi dùng local. Docker image nối thư mục này tới `/data/langgraph` để snapshot có thể sống qua việc tạo lại container với cùng volume. Worktrees/artifacts ở volume `WORKSPACE_ROOT`; journal Dispatcher ở `DATA_ROOT`.

Store `put/get/search` không phải multi-record transaction. Journal ghi ý định và replay khi khởi động lại giúp reconcile trong MVP, nhưng không bảo đảm exactly-once hoặc durability như hệ quản trị dữ liệu sản xuất. Không thay đổi expected artifact checksums hay tắt TLS để cài dependencies.

## Kiểm chứng

Chạy `uv run pytest -q` để kiểm tra schema, vòng đời, idempotency/recovery, claim, dependency ALL-join, concurrency, privacy, Qwen Work + LangChain Decision và Git worktree/integration. Smoke test Agent Server phải gọi một graph và xác nhận Store thật, tách với tests in-memory.

Nghiệm thu Docker cần build image, khởi động một container, chờ `/ok`, kiểm tra API riêng, ghi dữ liệu Store bằng runtime token, restart rồi đọc lại; worktree và artifact cần được đối chiếu trước/sau. Chỉ công bố phần kiểm chứng đã thực sự chạy. LLM end-to-end thật cần credentials hợp lệ và endpoint nội bộ; fake subprocess trong unit test không thay thế kiểm thử này.

## Build sau proxy doanh nghiệp

Dockerfile hỗ trợ BuildKit secret tùy chọn `proxy_ca` là chứng chỉ CA công khai của proxy được quản trị viên tin cậy. Không đưa API key vào build arguments. Khi Docker daemon không dùng được DNS bridge của môi trường hiện tại, có thể dùng host network cho build (không thay đổi network khi chạy):

```bash
docker build --network=host \
  --secret id=proxy_ca,src=/path/to/trusted-proxy-ca.crt \
  --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
  --build-arg http_proxy --build-arg https_proxy --build-arg no_proxy \
  -t qwen-orchestrator:dev .
```

CA được cài vào trust store hệ điều hành trong image để apt/Python/Node tiếp tục xác thực TLS. Giữ chữ ký apt, npm package integrity và dependency hashes của lockfile. Không dùng `--insecure`, `strict-ssl=false` hoặc bỏ kiểm chứng certificate.
