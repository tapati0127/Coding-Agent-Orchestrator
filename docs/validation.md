# Bằng chứng kiểm chứng bản MVP

Tài liệu này phân biệt kiểm thử runtime thực với mô hình được giả lập. Kết quả phản ánh môi trường phát triển hiện tại; không suy ra độ bền sản xuất của `langgraph dev` hoặc chất lượng mô hình nội bộ.

## Khởi chạy trực tiếp bằng LangGraph CLI

- `RUN_SERVER_TESTS=1 .venv/bin/pytest tests/test_e2e.py tests/test_server.py -q`:
  **2 passed**. Smoke gọi trực tiếp `langgraph dev`, nạp token từ `.env` thông qua
  `langgraph.json`, xử lý bảy task CLOSED và kiểm tra graceful restart giữ dữ liệu.
- Docker và live smoke script dùng cùng đường khởi chạy; chưa build lại Docker
  hoặc chạy smoke với model thật cho thay đổi này.

## Kiến trúc queue/process trong graph.py

- `RUN_SERVER_TESTS=1 .venv/bin/pytest -q`: **234 passed, 1 skipped**.
  Bài kiểm thử CLI Qwen thật tùy chọn chưa bật.

- `graph.py` khai báo queue stdlib và topology; Communicator chạy ở main process,
  Dispatcher và năm role kỹ thuật chạy ở child processes.
- Test runtime dùng process thật, kiểm tra PID riêng của Dispatcher/technical
  workers, PID Communicator trùng main process, request từ human được claim và ghi
  vào native Runtime Store qua queue RPC, rồi tất cả child process thoát sạch.
- Smoke Agent Server với model/CLI fixture đã chạy qua: bảy task CLOSED, hai
  Execute song song, Git integration, graceful shutdown và restart giữ nguyên
  task/checkpoint/patch. Chưa kiểm chứng chất lượng LLM thật hoặc build Docker
  cho topology mới.

## Sau khi gỡ giao diện web

- `PYTHONPATH=src .venv/bin/python -m pytest -q`: **233 passed, 2 skipped**.
- `PYTHONPATH=src RUN_SERVER_TESTS=1 .venv/bin/python -m pytest tests/test_e2e.py -q`:
  **1 passed**. Agent Server thật xử lý SDK chat, auth, bảy task CLOSED, Execute
  song song, Git integration và graceful restart; các route giao diện cũ trả 404.
- Model và Qwen CLI trong smoke test dùng fixture xác định. Kết quả không xác minh
  chất lượng mô hình nội bộ. Các lỗi Communicator đã tái hiện được ghi riêng ở
  [communicator-review.md](communicator-review.md).

## Bằng chứng trước thay đổi Communicator LLM

Các kết quả và số tests dưới đây là bằng chứng lịch sử của bản Communicator Python; không suy ra đã kiểm chứng lại bản Communicator LLM. Kiểm thử hiện tại có thêm intake LLM, shared backward budget, timeout và quyền đọc Messages.

## Đã kiểm chứng

- `uv sync --frozen` cài dependencies theo lockfile; `uv lock --check` qua.
- Wheel build thành công, chứa các role prompts.
- Docker image build thành công, cài Git, Qwen CLI `0.26.1`, Python dependencies đã khóa. Môi trường build hiện tại cần proxy DNS mapping và CA tin cậy; không tắt TLS hoặc kiểm tra chữ ký package.
- Container khởi động Agent Server, Dispatcher và các worker trong cùng container; healthcheck báo healthy.
- Built-in Store API và Communicator graph thực hoạt động qua Agent Server.
- Container graceful restart thành công; Store records, worktree, patch và workspace-manager metadata khôi phục được khi dùng lại volumes.
- Auth tests kiểm tra runtime token hợp lệ, từ chối token sai/trống và settings giới hạn concurrency hợp lệ.
- Bộ kiểm thử bật cả hai nhóm tùy chọn đã chạy đạt **130 tests**, không có skip (17,28 giây):

  ```bash
  RUN_SERVER_TESTS=1 RUN_QWEN_CLI_TESTS=1 \
    QWEN_CODE_BIN=/workspace/.orchestrator-tools/node_modules/.bin/qwen \
    .venv/bin/python -m pytest -q
  ```

- Smoke Agent Server thực kiểm tra cả sáu task `CLOSED`, hai Execute worker có khoảng thực thi chồng nhau, Git integration, đủ hai pass mỗi agent, Store/checkpoint/worktree/patch sau graceful restart. CLI Qwen `0.26.1` thực được kiểm tra riêng với endpoint OpenAI-compatible xác định, bao gồm chỉ expose tool `structured_output` trong pass quyết định.
- Image Docker đã chạy cùng luồng fixture: sáu task đóng, hai Execute được tích hợp, ba patch công khai; sau `docker restart`, giữ nguyên task IDs, trạng thái, hai file đã tích hợp và ba patch.
- Luồng clarification có kiểm thử riêng: Analyze kết thúc với `NEED_ANALYSIS`, không giữ worker khi approval chờ người, câu trả lời hợp lệ mở Analyze mới và không chấp nhận trả lời trùng.
- Giới hạn Qwen được kiểm tra qua tám thread có event loop riêng, hủy waiter/holder không làm mất slot; mọi role trong cùng runtime dùng chung semaphore theo process và DATA_ROOT.

## Giới hạn bằng chứng

Kiểm thử Qwen dùng deterministic subprocess/model fixture chỉ xác minh adapter hai pass, structured output, điều phối và cơ chế runtime. Nó không thay thế end-to-end với LLM nội bộ thật. Cần cấu hình endpoint, API key và model hợp lệ để nghiệm thu luồng này.

Graceful restart xác minh khả năng khôi phục một trường hợp đã chạy của phiên bản pin. Không bảo đảm dữ liệu luôn sống qua SIGKILL, host failure, corrupted volumes hoặc nâng cấp định dạng persistence. Store/checkpoints và Dispatcher journal không cung cấp multi-record transactions hoặc exactly-once trên mọi crash.

Chạy lại kiểm thử trên checkout hiện tại bằng `uv run pytest -q`. Số tests và smoke evidence cuối cùng được ghi trong báo cáo thực thi; không xem bài kiểm thử bị skip hoặc chưa chạy là đã qua.
