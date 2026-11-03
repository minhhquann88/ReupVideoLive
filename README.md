# Nimo Tool – tải & chỉnh sửa video Nimo TV

## Cài đặt
```
pip install -r requirements.txt
winget install Gyan.FFmpeg
```
Chạy: `python main.py` (hoặc nhấp đúp `run.bat`).

## Tính năng
- **Tải video**: dán link `.m3u8` (VOD hoặc LIVE). Tải song song nhiều luồng, chỉ tải 1 đoạn (từ–đến),
  ghi live đến khi bấm Dừng hoặc đủ thời lượng. Xuất MP4 không nén lại (giữ nguyên chất lượng).
- **Chỉnh sửa**: xem trước, đặt điểm đầu/cuối, cắt nhanh (không nén) hoặc chính xác, cắt khung 9:16 / 1:1 / 16:9,
  đổi độ phân giải, tốc độ 0.25–4x, âm lượng/tắt tiếng, chữ watermark, xuất MP3.
- **Ghép video**: nối nhiều file theo thứ tự.

## Lấy link m3u8
Mở video trên nimo.tv → F12 → tab Network → lọc `m3u8` → copy link `playlist...m3u8`.
Link có chữ ký (`wsSecret`, `wsTime`) và **hết hạn nhanh** – nếu báo 403 thì lấy link mới.

## Dòng lệnh
```
python hls_downloader.py "<link m3u8>" -o video.mp4 --start 01:00:00 --end 01:05:00
```
