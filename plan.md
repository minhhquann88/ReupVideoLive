

# Hàng đợi render + máy worker

## 1. Vấn đề

Mọi video đang render trên một máy. Trần render là file khoá trên đĩa của máy đó, tối đa 24 lượt, nên không chia việc sang máy khác được. Đông người dùng thì video phải xếp hàng chờ CPU của máy này.

## 2. Hướng giải quyết

Tách phần render nặng CPU sang các máy worker. Máy chính chỉ nhận việc, xếp hàng và lưu kết quả.

```
                    MÁY CHÍNH                                MÁY WORKER (1..N)
        ┌───────────────────────────────┐
Người   │  Web + Gateway                │   xin việc    ┌──────────────┐
dùng ──▶│  Chuẩn bị AI (dịch, TTS...)   │◀──────────────│  Worker A    │
        │  Hàng đợi (Postgres)          │   tải file    ├──────────────┤
        │  Lưu kết quả, xuất Drive/ZIP  │◀──────────────│  Worker B    │
        └───────────────────────────────┘   gửi MP4     ├──────────────┤
                                            ◀───────────│  Worker C    │
                                                        └──────────────┘
```

Ba nguyên tắc:

1. **Worker tự gọi về máy chính để xin việc.** Máy chính không cần biết IP của worker. Worker không cần IP tĩnh hay mở cổng.
2. **Mỗi video là một job.** Batch, thứ tự và xuất file vẫn do máy chính quản.
3. **Worker chỉ cầm file của job đang làm.** Worker không vào được Postgres, không có key AI, không có tài khoản Antigravity.

## 3. Việc nào chạy ở đâu

| Việc | Máy chính | Worker |
|---|---|---|
| Web, đăng nhập, cào, ghép Shopee | ✓ | |
| Whisper, dịch, TTS, viết lời | ✓ | |
| Hàng đợi, giao job | ✓ | |
| Cắt đầu/đuôi, OCR che sub, ghép voice/nhạc/logo, FFmpeg | | ✓ |
| Lưu kết quả, thống kê, xuất ZIP / Drive / Excel | ✓ | |

Riêng chế độ dịch theo subtitle vẫn render ở máy chính, vì OCR và AI của nó xen kẽ nhau.

## 4. Luồng chính

```
 1. Người dùng bấm Render (hoặc Chạy tự động đẩy video sang)
 2. Máy chính chạy phần AI nếu mẫu cần: Whisper, dịch, TTS
 3. Máy chính tạo 1 job cho video và đưa vào hàng đợi
    → Trên web: "Chờ worker rảnh"
 4. Worker còn chỗ trống gọi về xin việc
 5. Máy chính giao job cho đúng 1 worker
 6. Worker tải file nguồn, logo, nhạc, voice về ổ tạm
 7. Worker render, liên tục báo tiến độ
    → Trên web: % chạy như hiện nay
 8. Worker gửi MP4 về. Máy chính kiểm file hợp lệ rồi mới nhận
 9. Worker xoá file tạm và quay lại xin việc tiếp
10. Máy chính ghi nhận kết quả như hiện nay
    → Trên web: video hiện ở Kết quả & Xuất, tải ZIP hoặc đẩy Drive được
```

Người dùng vẫn thao tác như cũ. Khác biệt duy nhất họ thấy là dòng trạng thái "Chờ worker rảnh".

## 5. Worker biết có việc bằng cách nào

Máy chính không báo cho worker. Worker **tự hỏi** bằng long polling: gửi một request xin việc, máy chính giữ request đó tới 20 giây.

```
Worker                                Máy chính
  │  "Có việc không?"                     │
  │ ────────────────────────────────────▶ │  Giữ request, 1 giây kiểm tra hàng đợi một lần
  │                                       │
  │ ◀──────────────────────────────────── │  Có job → giao ngay
  │  Render, còn slot thì hỏi tiếp        │
  │                                       │
  │ ◀──────────────────────────────────── │  Hết 20 giây không có job → "chưa có việc"
  │  Hỏi lại ngay                         │
```

Job mới vào hàng đợi được giao trong khoảng 1 giây nếu đang có worker rảnh. Worker chỉ hỏi khi còn slot trống và RAM dưới 88 %.

## 6. Chia job khi nhiều worker rảnh

**Máy chính không chọn máy.** Worker nào hỏi trước thì nhận job xếp đầu hàng. Máy chính chỉ quyết định *job nào* được giao tiếp theo, còn *máy nào* làm thì do lúc máy đó còn slot và đến hỏi.

**Mỗi lần hỏi nhận đúng một job.** Máy còn slot thì hỏi tiếp ngay. Postgres khoá dòng job khi giao (`FOR UPDATE SKIP LOCKED`), nên hai máy hỏi cùng lúc sẽ nhận hai job khác nhau.

**Thứ tự chọn job:**

1. Mức ưu tiên: render tay và render lại trước, Chạy tự động sau.
2. Cùng mức thì chọn người đang có ít job chạy nhất.
3. Vẫn bằng nhau thì chọn job cũ nhất.

**Ví dụ:** người X đẩy 20 video, sau đó người Y đẩy 5 video, cùng mức ưu tiên. Worker A có 2 slot, worker B có 1 slot, tất cả đang rảnh.

| Lượt | Máy hỏi | Đang chạy trước lượt | Nhận | Lý do |
|---|---|---|---|---|
| 1 | A | X: 0, Y: 0 | Video của X | Bằng nhau, job của X cũ hơn |
| 2 | B | X: 1, Y: 0 | Video của Y | Y đang chạy ít hơn |
| 3 | A | X: 1, Y: 1 | Video của X | Bằng nhau, job của X cũ hơn |

Sau lượt 3, cả 3 slot đều bận và không máy nào hỏi nữa. Máy nào xong một video thì hỏi lại và nhận job tiếp theo theo đúng thứ tự trên.

**Kết quả của cách chia này:**

| Tình huống | Điều xảy ra |
|---|---|
| Nhiều job hơn slot | Mọi slot đều bận. Máy nhiều slot nhận nhiều job hơn, máy nhanh xong sớm nên hỏi sớm và nhận thêm |
| Ít job hơn slot | Không bảo đảm chia đều. Job có thể dồn vào một máy trong khi máy khác rảnh, nhưng không vượt số slot của máy đó |
| Một người đẩy rất nhiều video | Người vào sau vẫn được xen vào ngay khi có slot trống, không phải chờ người trước làm hết |
| Render tay liên tục | Chạy tự động phải chờ, vì mức ưu tiên được xét trước |

Video đang chạy không bị ngắt để nhường cho job ưu tiên hơn. Job ưu tiên chỉ được xếp trước ở lần giao kế tiếp.

## 7. Giao tiếp giữa các thành phần

```
Trình duyệt ◀── lệnh + WebSocket ──▶ Gateway ◀── HTTP + token ── Worker
                                       │
                                  IPC  │  đọc/ghi DB
                                       ▼
                               Process workspace
                                       │
                                  đọc/ghi DB
                                       ▼
                                   Postgres
```

Bốn quy tắc:

1. **Worker luôn là bên gọi.** Máy chính không bao giờ gọi vào worker.
2. **Worker chỉ nói chuyện với gateway**, không vào thẳng Postgres.
3. **Web không nói chuyện với worker.** Process workspace ghi job vào Postgres và đọc tiến độ từ đó, rồi đẩy lên trình duyệt qua WebSocket như hiện nay.
4. **Lệnh huỷ đi theo câu trả lời của heartbeat**, vì máy chính không gọi được vào worker.

**Worker và gateway trao đổi 6 loại request:**

| # | Khi nào | Worker gửi | Gateway trả về |
|---|---|---|---|
| 1. Đăng ký | Lúc khởi động | Tên máy, số slot, phiên bản, CPU, RAM | Đồng ý, hoặc từ chối nếu lệch phiên bản hay trùng tên |
| 2. Xin việc | Còn slot và RAM dưới 88 % | Tên máy | Một job, hoặc "chưa có việc" sau 20 giây |
| 3. Tải file | Ngay sau khi nhận job, bỏ qua file đã có trong cache | Mã job, tên file | Nội dung file |
| 4. Heartbeat | Mỗi 2 giây, kể cả khi rảnh | Tiến độ và log của mọi job đang chạy trên máy | Danh sách job phải dừng, job không còn thuộc máy này, cờ cho máy nghỉ |
| 5. Gửi kết quả | Render xong | File MP4, số lần thử | Đã nhận, hoặc từ chối kèm lý do |
| 6. Báo kết thúc | Sau khi gửi xong, hoặc khi lỗi, bị huỷ, phải tắt máy | Trạng thái, thông tin encode, lỗi nếu có | Đã nhận, hoặc từ chối nếu job đã sang lượt thử khác |

Mọi request đều kèm token của worker.

**Giao tiếp trong một video thành công:**

```
Worker                           Gateway                 Postgres            Process workspace
  │                                 │                       │                       │
  │                                 │                       │ ◀── ghi job (queued) ─│
  │── 2. Xin việc ────────────────▶ │── lấy job đầu hàng ─▶ │                       │
  │ ◀── job, số lần thử ─────────── │ ◀── running ───────── │                       │
  │── 3. Tải file ────────────────▶ │                       │                       │
  │ ◀── file ────────────────────── │                       │                       │
  │── 4. Heartbeat (mỗi 2 giây) ──▶ │── ghi tiến độ ──────▶ │ ◀── đọc mỗi 1 giây ── │──▶ % lên web
  │ ◀── không có lệnh dừng ──────── │                       │                       │
  │── 5. Gửi MP4 ─────────────────▶ │  kiểm file, lưu đĩa   │                       │
  │── 6. Báo xong ────────────────▶ │── done ─────────────▶ │ ◀── đọc kết quả ───── │──▶ Kết quả & Xuất
```

**Giao tiếp khi có sự kiện khác:**

| Sự kiện | Diễn ra thế nào |
|---|---|
| Người dùng bấm Dừng | Process workspace đặt cờ huỷ trong Postgres. Lần heartbeat kế tiếp trả job đó trong danh sách phải dừng. Worker giết FFmpeg, gửi "Báo kết thúc" với trạng thái đã huỷ |
| Worker mất mạng | Heartbeat không tới. Sau 60 giây gateway đưa job về hàng đợi, tăng lượt thử khi máy khác nhận |
| Worker cũ nối lại | Heartbeat trả job đó trong danh sách không còn thuộc máy này. Worker giết FFmpeg, xoá file, không báo gì thêm |
| Worker gửi kết quả muộn | Gateway so số lần thử, không khớp thì từ chối và không ghi đè file |
| Tắt worker | Worker làm nốt trong thời gian ngắn. Không kịp thì "Báo kết thúc" với trạng thái trả job, không tính lượt thử |
| Admin cho máy nghỉ | Heartbeat trả cờ cho máy nghỉ. Worker ngừng xin việc, làm xong job đang có |

Các mốc 1, 2, 20 và 60 giây là giá trị thiết kế, chưa đo trên hệ thống thật. Định dạng request nằm ở [phase 2](phase-02-job-queue-and-worker-api.md).

## 8. Khi có sự cố

| Tình huống | Hệ thống xử lý |
|---|---|
| Worker chết hoặc mất mạng lâu | Job về hàng đợi và máy khác làm lại, tối đa 2 lần thử |
| Mất mạng ngắn | Worker vẫn render và gửi lại kết quả sau |
| Tắt worker chủ động | Worker ngừng nhận việc, làm nốt trong thời gian ngắn, không kịp thì trả job |
| Người dùng bấm Dừng / Xoá | Worker dừng đúng video đó, video khác chạy tiếp |
| Không có worker online | Tuỳ cấu hình: render tại máy chính, hoặc chờ và báo trên web |
| Máy chính khởi động lại | Video đang chạy bị huỷ, giống hiện nay. Bản này chưa nối lại được |
| Bản mới có lỗi | Đổi cấu hình về `local` là render lại tại máy chính như cũ |

## 9. Thêm một máy worker

Máy worker chỉ cần Linux có Docker. Điền địa chỉ máy chính và token vào `.env.worker`, rồi chạy:

```bash
docker compose -f docker-compose.worker.yml up -d
```

Worker tự đăng ký và hiện trong trang quản trị. Máy chính không phải sửa gì. Muốn tăng công suất thì lặp lại trên máy khác.

## 10. Công cụ

| Việc | Chọn | Lý do |
|---|---|---|
| Hàng đợi | Bảng trong Postgres sẵn có | Không phải cài thêm dịch vụ. 1000 video/ngày chỉ khoảng 0,7 job/phút |
| Chuyển file | HTTP qua máy chính | File không lớn, không cần ổ đĩa chia sẻ |
| Đóng gói worker | Docker, cùng mã nguồn | Cùng ffmpeg, font, model nên kết quả giống nhau |

Redis, Celery và Kubernetes chưa cần ở quy mô này.

## 11. Các phase

| # | Phase | Xong thì có gì |
|---|---|---|
| 1 | [Tách phần render](phase-01-headless-render.md) | Render được 1 video mà không cần giao diện và DB |
| 2 | [Hàng đợi và API](phase-02-job-queue-and-worker-api.md) | Máy chính giao được job và nhận được kết quả |
| 3 | [Chương trình worker](phase-03-worker-runtime-and-image.md) | Container worker tự nhận và làm job |
| 4 | [Nối vào web](phase-04-connect-web-to-queue.md) | Bấm Render là chạy trên worker, cho ghép nhạc, giữ voice, che sub |
| 5 | [Dịch voice, Voice AI](phase-05-dub-and-ai-voice.md) | Hai chế độ này cũng chạy trên worker |
| 6 | [Vận hành và triển khai](phase-06-admin-docs-rollout.md) | Trang quản trị, tài liệu cài đặt, lên production |

Thứ tự làm từ 1 đến 4. Sau phase 4, phase 5 và 6 làm độc lập được.


