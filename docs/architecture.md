# Kiến trúc MVP

## Process và dữ liệu

Một Supervisor chạy Agent Server, Dispatcher và các worker độc lập trong một container. Sáu graph không có cạnh giao việc với nhau. Graph 1–5 gọi Qwen hai pass: thực thi công việc rồi quyết định JSON theo cùng `TaskGraphOutput`. Communicator sinh protocol bằng Python, không gọi Qwen.

Worker gửi claim, heartbeat và kết quả vào request inbox. Dispatcher kiểm tra quyền sở hữu task/lease, schema, DAG, ALL-join, outcome, chuyển trạng thái, tích hợp Git và backward budget, rồi ghi tasks/plans/events/receipts. Worker không ghi namespace task chính thức. Store là dữ liệu liên graph; checkpoint không thay thế Store.

Số Execute workers có thể tới 5, nhưng mọi graph Qwen dùng chung giới hạn 5 subprocess. Limiter dùng semaphore theo process và phạm vi `DATA_ROOT`, với chờ bất đồng bộ không block event loop; giới hạn vẫn áp dụng khi graph chạy trên nhiều event loop trong Agent Server. Supervisor truyền rõ `--n-jobs-per-worker 10`: CLI đã pin mặc định chỉ một background run và ghi đè cấu hình concurrency chỉ đặt bằng biến môi trường. Chỉ graph Verify được `IN_REVIEW`. Chỉ Finalize được đề nghị đóng các task hợp lệ; Dispatcher thực hiện đóng. Rework tạo task mới để giữ lịch sử, không mở lại task `DONE`. Node rework có thể khai báo `replaces_task_id` để thay một task `DONE` thất bại cùng vai trò. Dispatcher giữ nguyên status/outcome cũ, kiểm tra replacement và dùng alias tới task thay thế để các task downstream đang chờ theo dõi kết quả rework đã được chấp nhận; không âm thầm sửa dependency của task đang chạy.

## HTTP và quyền

Agent Server phục vụ cả `/dashboard` và API giao diện `/api/*`. Built-in Store/thread/run API dùng ORCHESTRATOR_TOKEN. API giao diện dùng DASHBOARD_TOKEN khác, chỉ cung cấp projection công khai và enqueue yêu cầu người dùng. Không có endpoint giao diện nào cho browser ghi task/plan authoritative.

Không bật custom-route auth mặc định của Agent Server cho `/api/*`, vì các route này kiểm tra dashboard token riêng. CORS mặc định cùng origin. Không đưa token vào URL hoặc gửi private scratchpad/raw Qwen logs lên browser. Dashboard stream tiến độ công khai, không stream scratchpad hay raw subprocess output. Patch/report content chỉ hiển thị qua shared artifact references đã được Dispatcher chấp nhận và kiểm tra scope; file trên disk không tự động thành tài liệu public. Shared promotion là đề xuất phải được Dispatcher kiểm tra.

Private namespace là phân vùng logic, không phải ACL. ScopedContext chỉ dựng shared context và private đúng owner; không đưa SDK Store credentials vào Qwen environment. Một container không bảo vệ được dữ liệu khỏi code độc hại cùng quyền user.

## Git

Execute workers có worktree riêng theo project/worker; integration có worktree riêng. Giữ patch/commit trước khi giải phóng worker. Không xoá worktree bẩn, không force-push. Verify cherry-pick/merge đầu ra để chuẩn bị baseline đã tích hợp trước khi task downstream cần code được chạy. Conflict phải xuất hiện như lỗi/rework có thể quan sát.

## Persistence

Package/runtime và toàn bộ dependency được khóa bằng `uv.lock`; Qwen CLI được pin trong Dockerfile. Runtime `langgraph dev` sử dụng `.langgraph_api` dưới thư mục chạy, được Supervisor nối tới `DATA_ROOT` để snapshot local có thể sống qua việc tạo lại container với cùng volume. Worktrees/artifacts ở volume `WORKSPACE_ROOT`; journal Dispatcher ở `DATA_ROOT`.

Store `put/get/search` không phải multi-record transaction. Journal ghi ý định và replay khi khởi động lại giúp reconcile trong MVP, nhưng không bảo đảm exactly-once hoặc durability như hệ quản trị dữ liệu sản xuất. Không thay đổi expected artifact checksums hay tắt TLS để cài dependencies.

## Kiểm chứng

Chạy `uv run pytest -q` để kiểm tra schema, vòng đời, idempotency/recovery, claim, dependency ALL-join, concurrency, privacy, Qwen hai pass và Git worktree/integration. Smoke test Agent Server phải gọi một graph và xác nhận Store thật, tách với tests in-memory.

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
