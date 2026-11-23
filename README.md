# Giá phân bón Việt Nam

Web app phân tích xu hướng giá phân bón tại Việt Nam, giá phân bón thế giới và cước vận tải biển (tàu hàng rời).
App chỉ dùng dữ liệu thật từ nguồn miễn phí; khi nguồn không có số liệu thì giá trị để trống (NULL), không tạo dữ liệu giả.

## Nguồn dữ liệu

| Chuỗi | Nguồn | Tần suất | Ghi chú |
|---|---|---|---|
| Giá bán lẻ trong nước (Urê, DAP, Kali, NPK, Lân), VND/bao | [VietnamBiz](https://vietnambiz.vn/gia-phan-bon.html) | Lấy 9:00 và 15:00 mỗi ngày | Nguồn chỉ có giá ngày hiện tại; lịch sử được tích lũy từ lần chạy đầu tiên |
| Giá thế giới DAP, Urê, Kali clorua, TSP, quặng phosphate, USD/tấn | [World Bank Pink Sheet](https://www.worldbank.org/en/research/commodity-markets) (CC BY 4.0) | Theo tháng, từ 2000 | Kiểm tra ngày 1–10 hằng tháng |
| Baltic Dry Index (cước tàu hàng rời), điểm | Baltic Exchange qua [Dashboard Konjunktur](https://www.dashboard-konjunktur.de) (DL-DE/BY-2.0) | Theo ngày, từ 2007 | Cước theo tuyến về Việt Nam cần nguồn trả phí, chưa có |

## Chạy app

```bash
pip install -r requirements.txt
export DATA_DIR=$HOME/fertilizer-data   # nơi lưu SQLite, nằm ngoài thư mục code
python -m app.ingest                    # lấy dữ liệu lần đầu
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Mở http://localhost:8000. Server tự lấy dữ liệu theo lịch (đặt `DISABLE_SCHEDULER=1` để tắt).

## Cấu trúc

- `app/ingest.py` — tải và phân tích dữ liệu từ từng nguồn, ghi vào SQLite.
- `app/main.py` — API FastAPI (`/api/series`, `/api/observations`, `/api/vn-latest`, `/api/fetch-log`) và lịch cập nhật.
- `static/` — giao diện (Chart.js): giá trong nước, giá thế giới, BDI, biểu đồ so sánh chỉ số và bảng tương quan BDI–giá phân bón.
