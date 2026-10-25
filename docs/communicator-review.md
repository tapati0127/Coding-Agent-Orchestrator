# Review Communicator

Đã đọc graph Communicator, chọn project, theo dõi tiến độ, DecisionRunner,
worker runner và các nhánh conversation/approval của Dispatcher. Bốn lỗi dưới
đây đã được tái hiện bằng Store in-memory và model giả lập; các nhánh quyết định
approve/reject đã được đối chiếu với Dispatcher thật. Chưa sửa các lỗi này trong
thay đổi gỡ dashboard.

## P1 — Có thể phê duyệt khác câu hỏi vừa hiển thị

`src/orchestrator/graphs/projects.py:101` hiển thị approval PENDING có
`created_at` mới nhất. `src/orchestrator/graphs/progress.py:123` lại chọn
`pending[0]`, trong khi `read_progress` sắp xếp approvals theo `approval_id`.
Nếu cùng chain có nhiều approval, câu trả lời “đồng ý” có thể phê duyệt câu hỏi
khác với câu hỏi người dùng vừa thấy.

Tái hiện: approval `a-old` được tạo trước, `z-new` được tạo sau. Chọn project
hiển thị câu hỏi của `z-new`; trả lời “đồng ý” gửi request approval cho `a-old`.
Cần lưu approval ID của câu hỏi đã hiển thị và gắn câu trả lời vào đúng ID;
khi có nhiều câu hỏi chưa rõ mục tiêu thì yêu cầu chọn câu hỏi.

## P1 — Chat treo sau khi từ chối quyết định kỹ thuật

`src/orchestrator/graphs/progress.py:134` chọn chain mới cho cả approve và
reject. `src/orchestrator/dispatcher/engine.py:235` xử lý reject bằng cách lưu
REJECTED rồi trả receipt, không tạo chain/task mới. Vòng lặp progress tại dòng
165–169 không kết thúc hoặc chuyển blocked khi danh sách task trống.

Tái hiện: trả lời “từ chối” cho approval của VERIFY_INTEGRATE. Dispatcher lưu
REJECTED thành công nhưng chat vẫn poll chain rỗng cho đến khi bị hủy.
Cần kết thúc lượt chat bằng xác nhận từ chối và giữ chain hiện tại; kiểm tra
receipt trước khi bắt đầu theo dõi một chain mới.

## P2 — Lịch sử project cũ vẫn vào prompt của project mới

`src/orchestrator/graphs/communicator.py:43` lấy toàn bộ `state.messages`
để tạo prompt. Khi đổi project, `project_message_start` được cập nhật nhưng
chỉ dùng để cắt history gửi Dispatcher ở `graphs/progress.py:144`.
Model báo tiến độ vẫn nhận yêu cầu và câu trả lời của project trước.

Tái hiện: chọn A, gửi yêu cầu riêng cho A, mở menu và chọn B, rồi gửi yêu cầu
cho B. Prompt chuyển vào `follow_progress` vẫn chứa yêu cầu của A dù nó đã nằm
ngoài `project_message_start`. Cần áp dụng ranh giới lịch sử project khi dựng
prompt cho nhánh chat theo dõi tiến độ.

## P2 — Mở lại project có thể theo dõi nhầm chain sau approve

Khi không có chain trong thread và không còn approval PENDING,
`src/orchestrator/graphs/progress.py:99` chọn conversation có `created_at`
mới nhất. Nhánh technical approve ở `dispatcher/engine.py:240` tạo task Analyze
trong chain mới nhưng không tạo conversation tương ứng. Vì vậy conversation
mới nhất vẫn trỏ vào chain cũ.

Tái hiện: Dispatcher nhận technical approve, tạo Analyze TODO trong chain mới;
thread mới gửi “status” lại đọc chain cũ và báo blocked. Cần lưu chain đang
được tiếp tục ở cấp project hoặc tìm chain từ task/receipt; đồng thời chuyển
lịch sử được phép sang chain tiếp tục.

## Kiểm chứng thay đổi gỡ dashboard

- Bộ test thường: 233 passed, 2 skipped.
- Smoke Agent Server thật với model/CLI fixture: 1 passed; kiểm tra SDK chat,
  auth, DAG, Git integration, persistence qua graceful restart và route cũ trả 404.
- Các kịch bản tái hiện review chạy riêng, không phải kiểm thử LLM nội bộ thật.
