# Bằng chứng kiểm chứng bản MVP

Tài liệu này phân biệt kiểm thử runtime thực với mô hình được giả lập. Kết quả phản ánh môi trường phát triển hiện tại; không suy ra độ bền sản xuất của `langgraph dev` hoặc chất lượng mô hình nội bộ.

## Bằng chứng trước thay đổi Communicator LLM

Các kết quả và số tests dưới đây là bằng chứng lịch sử của bản Communicator Python; không suy ra đã kiểm chứng lại bản Communicator LLM. Kiểm thử hiện tại có thêm intake LLM, shared backward budget, timeout và quyền đọc Messages.

## Đã kiểm chứng

- `uv sync --frozen` cài dependencies theo lockfile; `uv lock --check` qua.
- Wheel build thành công, chứa các role prompts và assets dashboard.
- Docker image build thành công, cài Git, Qwen CLI `0.26.1`, Python dependencies đã khóa. Môi trường build hiện tại cần proxy DNS mapping và CA tin cậy; không tắt TLS hoặc kiểm tra chữ ký package.
- Container khởi động Agent Server, Dispatcher và các worker trong cùng container; healthcheck báo healthy.
- Built-in Store API và Communicator graph thực hoạt động qua Agent Server.
- Container graceful restart thành công; Store records, worktree, patch và workspace-manager metadata khôi phục được khi dùng lại volumes.
- Auth tests kiểm tra dashboard token không dùng được với runtime API, cấu hình token trống/giống nhau fail closed, và settings giới hạn concurrency hợp lệ.
- Bộ kiểm thử bật cả hai nhóm tùy chọn đã chạy đạt **130 tests**, không có skip (17,28 giây):

  ```bash
  RUN_SERVER_TESTS=1 RUN_QWEN_CLI_TESTS=1 \
    QWEN_CODE_BIN=/workspace/.orchestrator-tools/node_modules/.bin/qwen \
    .venv/bin/python -m pytest -q
  ```

- Smoke Agent Server thực kiểm tra cả sáu task `CLOSED`, hai Execute worker có khoảng thực thi chồng nhau, Git integration, đủ hai pass mỗi agent, Store/checkpoint/worktree/patch sau graceful restart. CLI Qwen `0.26.1` thực được kiểm tra riêng với endpoint OpenAI-compatible xác định, bao gồm chỉ expose tool `structured_output` trong pass quyết định.
- Image Docker đã chạy cùng luồng fixture: sáu task đóng, hai Execute được tích hợp, ba patch công khai; sau `docker restart`, giữ nguyên task IDs, trạng thái, hai file đã tích hợp và ba patch. Dashboard trả HTML và dashboard token bị từ chối ở built-in Store API.
- Luồng clarification có kiểm thử riêng: Analyze kết thúc với `NEED_ANALYSIS`, không giữ worker khi approval chờ người, câu trả lời hợp lệ mở Analyze mới và không chấp nhận trả lời trùng.
- Giới hạn Qwen được kiểm tra qua tám thread có event loop riêng, hủy waiter/holder không làm mất slot; mọi role trong cùng runtime dùng chung semaphore theo process và DATA_ROOT.

## Giới hạn bằng chứng

Kiểm thử Qwen dùng deterministic subprocess/model fixture chỉ xác minh adapter hai pass, structured output, điều phối và cơ chế runtime. Nó không thay thế end-to-end với LLM nội bộ thật. Cần cấu hình endpoint, API key và model hợp lệ để nghiệm thu luồng này.

Graceful restart xác minh khả năng khôi phục một trường hợp đã chạy của phiên bản pin. Không bảo đảm dữ liệu luôn sống qua SIGKILL, host failure, corrupted volumes hoặc nâng cấp định dạng persistence. Store/checkpoints và Dispatcher journal không cung cấp multi-record transactions hoặc exactly-once trên mọi crash.

Chạy lại kiểm thử trên checkout hiện tại bằng `uv run pytest -q`. Số tests và smoke evidence cuối cùng được ghi trong báo cáo thực thi; không xem bài kiểm thử bị skip hoặc chưa chạy là đã qua.
