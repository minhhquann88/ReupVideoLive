# Nimo Tool – tải & chỉnh sửa video Nimo TV

## Cài đặt
```
pip install -r requirements.txt
python setup_ffmpeg.py
```
`setup_ffmpeg.py` tải ffmpeg + ffprobe (bản essentials của gyan.dev, có kiểm SHA-256) vào thư mục `bin/`
của tool, không cài global. Tool luôn ưu tiên `bin/`, không có mới tìm trong PATH.
Chạy: `python main.py` (hoặc nhấp đúp `run.bat`).

## Tính năng
- **Tải video**: dán link `.m3u8` (VOD hoặc LIVE). Tải song song nhiều luồng, chỉ tải 1 đoạn (từ–đến),
  ghi live đến khi bấm Dừng hoặc đủ thời lượng. Xuất MP4 không nén lại (giữ nguyên chất lượng).
- **Chỉnh sửa (ghép bố cục)**: kéo chuột trên video để chọn nhiều vùng cắt (vd. facecam + màn hình game),
  rồi kéo thả / phóng to từng vùng trên khung xuất (9:16, 16:9, 1:1, 4:5 hoặc tự nhập), chọn màu nền,
  xếp nhanh Dọc / Ngang / Lưới, lưu & mở lại bố cục (.json), chỉ xuất 1 đoạn (từ–đến).

## Lấy link từ link video Nimo (API)
Dán link `https://www.nimo.tv/v/v-...` vào ô link, nhập kênh (alias như `thaygiaoba`, URL kênh hoặc roomId),
chọn chất lượng rồi bấm **🎬 Lấy link từ video Nimo** → tool tự điền link m3u8 và tên file.
Dòng lệnh:
```
python nimo_api.py https://www.nimo.tv/v/v-1880869346605400724 -c thaygiaoba
python nimo_api.py https://www.nimo.tv/v/v-1880869346605400724 -c thaygiaoba --res 720 -o video.mp4
python nimo_api.py --list -c thaygiaoba --type replay --limit 10
```

## Lấy link m3u8 thủ công
Mở video trên nimo.tv → F12 → tab Network → lọc `m3u8` → copy link `playlist...m3u8`.
Link có chữ ký (`wsSecret`, `wsTime`) và **hết hạn nhanh** – nếu báo 403 thì lấy link mới.

## Dòng lệnh
```
python hls_downloader.py "<link m3u8>" -o video.mp4 --start 01:00:00 --end 01:05:00
```
