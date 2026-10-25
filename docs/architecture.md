# Kiến trúc MVP

## Process và dữ liệu

Supervisor tạo `multiprocessing.Queue` và khởi động các worker bằng
`multiprocessing.Process` với start method spawn. Mỗi worker có queue nhận việc
riêng; mọi worker gửi heartbeat, kết quả và lỗi vào một queue chung. Dispatcher
là một async task trong lifespan của Agent Server, dùng `runtime.store` của
LangGraph, không còn process Dispatcher hoặc SDK Store client riêng.

Dispatcher là bên duy nhất ghi Store. Nó đọc task, kiểm tra
DAG/dependencies và concurrency, claim độc quyền rồi dựng snapshot `task`,
`project`, `context`, `predecessors`, `thread_id` cho worker. Worker chờ
`queue.get()` và invoke graph local, không dùng Store/SDK hay polling receipt.
Communicator gọi ChatOpenAI trực tiếp; năm role kỹ thuật dùng Work qua Qwen Code
và Decision qua ChatOpenAI. Graph kết thúc sau một lượt, worker quay lại chờ.

`langgraph.json` chỉ expose graph Communicator (`START → communicate → END`).
Agent Server phục vụ chat và Dashboard. Custom lifespan lấy Runtime bằng
`get_runtime()` sau khi runtime nền khởi tạo Store và truyền cho Dispatcher.
Một manager bridge loopback cấp proxy của inbox/event queue cho Dispatcher;
bridge không truy cập Store. Worker giữ queue trực tiếp và invoke graph local;
việc chạy graph kỹ thuật không đi qua Agent Server.

Giới hạn Qwen toàn hệ thống nằm ở Dispatcher; Communicator không chiếm slot.
Semaphore trong QwenRunner chỉ có phạm vi một worker process. Task và journal
vẫn lưu trong Store; queue không lưu bền. Dispatcher phục hồi task sau lease hết
hạn hoặc restart. Mọi heartbeat/result/failure từ worker có task_attempt; kết quả
của lượt đã hết hạn bị từ chối ngay cả khi worker ID được dùng lại.

Shutdown gửi sentinel cho workers, đợi kết quả flush về Dispatcher, rồi dừng
Dispatcher, Agent Server và manager bridge. Worker process chết được restart có
giới hạn. Dispatcher lỗi thì Supervisor dừng các worker và server; lỗi khởi động
Dispatcher làm Agent Server không ready. Task vẫn được bảo vệ bằng owner, lease
và attempt. File lock giữ đúng một Dispatcher cho cùng data directory.

Communicator chat nhận `Runtime` của LangGraph và đọc task/approval theo execution
chain bằng `runtime.store.aget/asearch`. Store được Agent Server inject, hoặc được
truyền qua `builder.compile(store=store)` khi chạy local; subgraph kế thừa Store
của graph cha. Không tạo SDK client hoặc Store riêng trong node. Dispatcher cũng
nhận `runtime.store`; `StoreRecords` chỉ unwrap native Store Items cho engine,
không mở kết nối HTTP hoặc tạo backend riêng.
Communicator chỉ gọi ChatOpenAI.ainvoke khi snapshot trạng thái đổi, stream
token mà không return messages. Theo dõi dừng khi chain CLOSED, chờ human hoặc
không còn task active sau nhiều lần đọc ổn định. Worker kỹ thuật vẫn không đọc
Store. Hủy task/CANCEL chưa triển khai; khi thêm phải gửi yêu cầu qua Dispatcher.

Communicator dùng `backward_count` chung khi tự giao hoặc hỏi human, không có bộ đếm hỏi riêng. Approval của Communicator có deadline (mặc định 1800 giây). Trả lời hoặc timeout tạo task Communicator trong cùng chain qua journal/receipt hiện có; timeout ghi SystemMessage, không giả tạo HumanMessage hay phê duyệt. Khi timeout hoặc hết ngân sách, Communicator bàn giao Analyze với giả định công khai. Từ chối không tiếp tục. Task hỏi đã DONE/SUCCESS vì lượt giao tiếp hoàn tất; không giữ lease khi chờ human.

`CommunicatorState.messages` là danh sách `AnyMessage` dùng reducer `add_messages`. Lịch sử được journal theo chain ở `("coding", project_id, "conversations")`, không nằm trong shared context/task description. Dispatcher dùng scoped adapter để cấp lịch sử cho Communicator, Analyze, Finalize. Finalize dùng nó đối chiếu nghiệm thu với mong muốn người dùng; các role còn lại chỉ nhận brief/contract. Dashboard có projection riêng của tin nhắn human/ai.

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

Agent Server phục vụ cả `/dashboard` và API giao diện `/api/*`. Dashboard là khung
chat, không còn bảng DAG/workers/artifacts. `/api/chat/stream` dùng LangGraph SDK
gọi graph communicator với stream_subgraphs=True và modes messages-tuple/custom; root
graph chứa subgraph communicate để token mang namespace nguồn. API proxy nhận
messages/project/thread đã kiểm tra, không nhận task/context tùy ý. Khi resume,
thread phải có metadata dashboard_chat và khớp project. Built-in Store/thread/run
API dùng ORCHESTRATOR_TOKEN; browser chỉ dùng DASHBOARD_TOKEN khác qua proxy.
Không có endpoint giao diện nào cho browser ghi task/plan authoritative. Đóng
stream hủy lượt theo dõi qua on_disconnect=cancel; task nền vẫn do Dispatcher quản lý.

Không bật custom-route auth mặc định của Agent Server cho `/api/*`, vì các route này kiểm tra dashboard token riêng. CORS mặc định cùng origin. Không đưa token vào URL hoặc gửi private scratchpad/raw Qwen logs lên browser. Dashboard stream tiến độ công khai, không stream scratchpad hay raw subprocess output. Patch/report content chỉ hiển thị qua shared artifact references đã được Dispatcher chấp nhận và kiểm tra scope; file trên disk không tự động thành tài liệu public. Shared promotion là đề xuất phải được Dispatcher kiểm tra.

Private namespace là phân vùng logic, không phải ACL. Dispatcher dùng ScopedContext dựng shared context và private đúng owner; không đưa SDK Store credentials vào Qwen environment. Một container không bảo vệ được dữ liệu khỏi code độc hại cùng quyền user.

## Git

Execute workers có worktree riêng theo project/worker; integration có worktree riêng. Giữ patch/commit trước khi giải phóng worker. Không xoá worktree bẩn, không force-push. Verify cherry-pick/merge đầu ra để chuẩn bị baseline đã tích hợp trước khi task downstream cần code được chạy. Conflict phải xuất hiện như lỗi/rework có thể quan sát.

## Persistence

Package/runtime và toàn bộ dependency được khóa bằng `uv.lock`; Qwen CLI được pin trong Dockerfile. Runtime `langgraph dev` sử dụng `.langgraph_api` dưới thư mục chạy, được Supervisor nối tới `DATA_ROOT` để snapshot local có thể sống qua việc tạo lại container với cùng volume. Worktrees/artifacts ở volume `WORKSPACE_ROOT`; journal Dispatcher ở `DATA_ROOT`.

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
