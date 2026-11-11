# Kế hoạch: Lấy video phát lại Nimo TV qua API

Nov 4, 2026 · @quân

## Mục tiêu & phạm vi

Cho một URL video phát lại Nimo (vd. `https://www.nimo.tv/v/v-1880869346605400724`), trả về đúng video đó kèm metadata và link phát m3u8 còn hiệu lực.

- **Đầu vào:** URL video hoặc `id` video, cùng `roomId` của kênh (Thầy Giáo Ba = `2272219`).
- **Đầu ra:** `id`, `title`, `playDuration`, `createdTime`, `liveId`, link m3u8 theo từng độ phân giải.
- **Ngoài phạm vi:** tải và ghép file video, giải mã body mã hoá `keyType=2` của trang.

## API sử dụng

Chỉ cần một endpoint: danh sách video của một room, có phân trang.

`POST https://video.nimo.tv/v1/liveVideo/videoListForAgg-{page}-{size}/{lang}/{roomId}`

| Thành phần | Giá trị | Ghi chú |
| --- | --- | --- |
| `page` | 1, 2, 3… | Trang vượt quá số trang trả mảng rỗng, không lỗi |
| `size` | tối đa đã thử: 100 | 1616 video → 17 trang với size 100 |
| `lang` | `1066` | Tiếng Việt (mặc định của trang là `1033`) |
| `roomId` | `2272219` | Room của kênh |
| FormData `keyType` | `0` | 0 = body JSON thường; trang thật gửi 2 (mã hoá) |
| FormData `body` | `{"roomId":2272219}` | Không thêm `tabId`, `orderColumn` để giữ thứ tự mới nhất trước |

Response cần đọc:

- `data.result.liveVideoViewList[]`: mỗi phần tử là một video (`id`, `title`, `dotType`, `playDuration`, `createdTime`, `liveId`, `videoUrl`, `multiResolutionVideoUrl[]`).
- `data.result.pageControlView`: `count`, `pageCount`, `pageIndex`, `pageSize`.

Nguồn gốc: hàm `fetchVideoListWithRoomId` và bảng `{ CLEAR_TEXT: 0, CIPHER_TEXT: 2 }` trong JS của trang Nimo; đã gọi thử thành công (`code: 200`) ngày 4/11/2026.

## Luồng xử lý

Mỗi lần gọi lấy 100 video; video cần tìm thường nằm ở trang đầu, tệ nhất 17 lần gọi cho kênh này.

&#91;embedded content: luồng tìm video · 5 bước, 2 điểm rẽ\]

"Còn trang?" là sai khi đã qua `pageCount`, khi trang trả về rỗng, hoặc (nếu bật dừng sớm) khi `id` cuối trang đã nhỏ hơn `id` cần tìm.

## Lọc và đối chiếu

Chỉ khớp bằng `id`; tiêu đề không dùng được vì gần như mọi buổi live đều tên "Tối vui vẻ".

- **Khớp:** `v.id === targetId`, so sánh dạng chuỗi. `id` dài 19 chữ số, vượt giới hạn `Number`, nên khi cần so lớn/nhỏ thì dùng `BigInt`.
- **Loại video:** `dotType: 1` là video phát lại buổi live. `dotType: 4` là clip người khác cắt (vd. "Phật Sống Baroibeo" của Hải Đông). Khi liệt kê thì lọc theo loại cần lấy.
- **Thứ tự:** body chỉ có `roomId` trả mới nhất trước. Danh sách gợi ý trên trang thì xếp theo lượt xem (`playNum` giảm dần), nên video mới ít view bị đẩy xuống xa.
- **Dừng sớm:** khi xếp theo thời gian, `id` giảm dần. Nếu `id` cuối trang đã nhỏ hơn `targetId` mà chưa thấy thì video không có trong danh sách. Quan sát trên khoảng 160 video, Nimo không ghi tài liệu; tắt được bằng một cờ.
- **Giới hạn vòng lặp:** tối đa `pageCount` trang, lấy từ `pageControlView` của lần gọi đầu.

## Link phát (m3u8)

API trả sẵn link m3u8 đã ký, dùng được trong khoảng 1 giờ (`loopTime: 3600`), nên không lưu link lâu dài mà lưu `id` rồi lấy link mới khi cần.

- Link không kèm tham số ký (`wsSecret`, `wsTime`, `sn`) bị **403**.
- `videoUrl` là bản mặc định (thường 480P). `multiResolutionVideoUrl[]` có 480P / 720P / 1080P, chọn theo `iresolution`.
- Cache link theo `id` + độ phân giải, hết hạn sau khoảng 50 phút để còn dư thời gian cho người xem.
- Không chia sẻ công khai link đã ký: nó gắn với phiên (`cu`, `srckey`) của người gọi.

## Rủi ro & xử lý lỗi

Rủi ro lớn nhất: đây là API nội bộ không công bố, có thể đổi bất cứ lúc nào.

| Tình huống | Dấu hiệu | Xử lý |
| --- | --- | --- |
| Server bắt buộc mã hoá | `keyType=0` trả `code` khác 200 | Báo lỗi rõ ràng; tìm hàm mã hoá (biến `T` trong `transformRequest`) |
| Body sai | `code: 10401` "Param illegal" | Kiểm tra `roomId` và cách gửi FormData |
| Gọi từ server bị chặn | `code: 407` hoặc HTTP 403 | Thêm header `Origin` / `Referer` = `https://www.nimo.tv` |
| Không tìm thấy video | Hết `pageCount` hoặc dừng sớm | Trả `null`; video có thể đã bị xoá hoặc sai `roomId` |
| Link m3u8 hết hạn | HTTP 403 khi phát | Gọi lại API để lấy link mới |
| Gọi quá nhiều | Lỗi hoặc bị chặn tạm thời | Size 100, nghỉ giữa các trang, cache kết quả |

## Code mẫu

Chạy bằng Node 18+ (lưu thành file .mjs vì có top-level await) hoặc trong console trình duyệt.

```js
const LANG = 1066;
const API = 'https://video.nimo.tv/v1/liveVideo';

// Bước 1: lấy id từ URL
function parseVideoId(input) {
  const m = String(input).match(/v-(\d+)/);
  if (m) return m[1];
  if (/^\d+$/.test(input)) return input;
  throw new Error('URL/id không hợp lệ: ' + input);
}

// Bước 2: gọi một trang
async function getPage(roomId, page, size = 100) {
  const fd = new FormData();
  fd.append('keyType', '0');
  fd.append('body', JSON.stringify({ roomId }));
  const res = await fetch(`${API}/videoListForAgg-${page}-${size}/${LANG}/${roomId}`, {
    method: 'POST',
    body: fd,
  });
  const json = await res.json();
  if (json.code !== 200) throw new Error(`Nimo API ${json.code}: ${json.message}`);
  const { liveVideoViewList, pageControlView } = json.data.result;
  return { list: liveVideoViewList, pageCount: pageControlView.pageCount };
}

// Bước 3: lật trang cho tới khi gặp id
async function findVideo(roomId, input, { earlyStop = true } = {}) {
  const targetId = parseVideoId(input);
  const target = BigInt(targetId);
  let page = 1, pageCount = 1;
  do {
    const r = await getPage(roomId, page);
    pageCount = r.pageCount;
    const hit = r.list.find(v => v.id === targetId);
    if (hit) return toResult(hit);
    const last = r.list.at(-1);
    if (!last) return null;
    if (earlyStop && BigInt(last.id) < target) return null;
    page++;
  } while (page <= pageCount);
  return null;
}

// Bước 4: rút gọn kết quả
function toResult(v) {
  return {
    id: v.id,
    title: v.title,
    isReplay: v.dotType === 1,
    durationSec: v.playDuration,
    createdAt: new Date(v.createdTime),
    liveId: v.liveId,
    pageUrl: `https://www.nimo.tv/v/v-${v.id}`,
    streams: (v.multiResolutionVideoUrl || []).map(s => ({ res: s.iresolution, url: s.videoUrl })),
    defaultUrl: v.videoUrl,
    fetchedAt: Date.now(), // link hết hạn sau ~3600s
  };
}

const video = await findVideo(2272219, 'https://www.nimo.tv/v/v-1880869346605400724');
console.log(video);
```

## Việc cần làm tiếp

- [ ] Chạy thử `findVideo` từ server (không phải trình duyệt), xem có cần header `Origin` / `Referer` không
- [ ] Xác nhận `size` tối đa API chấp nhận (đã thử được 100)
- [ ] Thử một video cũ ở trang cuối để kiểm tra vòng lặp và điều kiện dừng sớm
- [ ] Thêm cache link m3u8 theo `id` + độ phân giải, hết hạn trước 1 giờ
- [ ] Lấy `roomId` tự động từ trang kênh (vd. `nimo.tv/thaygiaoba`) thay vì gán cứng
- [ ] Thêm cảnh báo khi API trả `code` khác 200, để biết sớm nếu Nimo đổi API
