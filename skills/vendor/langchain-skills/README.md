# Skill LangGraph offline

Bản chụp từ [langchain-ai/langchain-skills](https://github.com/langchain-ai/langchain-skills).

- Commit upstream: `126c384d8552dcb476b226c244172186cc3f4328`.
- Phạm vi: toàn bộ 7 skill `langgraph-*` ở commit này, kèm `ecosystem-primer`
  và `langchain-dependencies` mà bộ skill dùng làm kiến thức nền.
- Giữ nguyên nội dung upstream, gồm toàn bộ thư mục `references/` đi kèm.
- Giấy phép upstream: [LICENSE](LICENSE). Mục lục gốc: [UPSTREAM_README.md](UPSTREAM_README.md).
- Nguồn và SHA-256 từng file: [snapshot.json](snapshot.json) và [../../sources.json](../../sources.json).

| Skill | Nhóm |
| --- | --- |
| [ecosystem-primer](ecosystem-primer/SKILL.md) | Tài liệu nền |
| [langchain-dependencies](langchain-dependencies/SKILL.md) | Tài liệu nền |
| [langgraph-cli](langgraph-cli/SKILL.md) | LangGraph |
| [langgraph-decision-models](langgraph-decision-models/SKILL.md) | LangGraph |
| [langgraph-fundamentals](langgraph-fundamentals/SKILL.md) | LangGraph |
| [langgraph-human-in-the-loop](langgraph-human-in-the-loop/SKILL.md) | LangGraph |
| [langgraph-persistence](langgraph-persistence/SKILL.md) | LangGraph |
| [langgraph-python-quickstart](langgraph-python-quickstart/SKILL.md) | LangGraph |
| [langgraph-typescript-quickstart](langgraph-typescript-quickstart/SKILL.md) | LangGraph |

## Đọc khi không có mạng

Mở `SKILL.md` và các file tương đối trong `references/` trực tiếp từ thư mục này.
Quickstart upstream yêu cầu đọc trang live; khi dùng offline, thay bằng bản lưu:

| Trang gốc | Bản offline |
| --- | --- |
| Python LangGraph quickstart | [docs/python-quickstart.md](docs/python-quickstart.md) |
| TypeScript LangGraph quickstart | [docs/typescript-quickstart.md](docs/typescript-quickstart.md) |
| Chỉ mục tài liệu LangChain | [docs/llms.txt](docs/llms.txt) |

Chỉ mục llms.txt có liên kết tới nhiều trang khác; những trang không được liệt kê
trong snapshot.json không nằm trong bản lưu. Cài package hoặc gọi dịch vụ model
vẫn cần môi trường/dependency phù hợp; bản chụp này lưu nội dung skill/tài liệu.

## Dùng trong repository

Đọc skill bằng đường dẫn local, hoặc thêm đường dẫn skill cần thiết vào
`skills.roles` trong `config/cli/qwen.yaml` hoặc `config/cli/opencode.yaml`, ví dụ:

```yaml
skills:
  roles:
    execute:
      - ../../skills/vendor/langchain-skills/langgraph-fundamentals
      - ../../skills/vendor/langchain-skills/langgraph-persistence
```

Đây là bản vendor theo dự án, không tự đăng ký skill toàn cục và không tự đổi
các lựa chọn skill theo role hiện tại. Không cần tải GitHub để đọc bản offline.
