import os
import argparse
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
"""
# 傳入網址
python main.py https://www.mvffm.net/movies/its-my-time/

# 檢查全部來源
python main.py https://www.mvffm.net/movies/its-my-time/ -n 0

# m3u8 精確計算大小
python main.py https://www.mvffm.net/movies/its-my-time/ --exact

# 沒傳網址 -> 顯示說明
python main.py
"""

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})
REFERER = ""          # 執行時依傳入網址自動設定

TIMEOUT = (3, 6)      # (連線, 讀取) 秒
SAMPLE = 3            # m3u8 抽樣分片數
DEFAULT_MAX_SOURCES = 10
FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")
OUTPUT_DIR = "videos"   # 下載存放的子目錄

def set_referer(page_url):
    """依頁面網址設定 Referer，例如 https://www.example.com/"""
    global REFERER
    p = urlparse(page_url)
    REFERER = f"{p.scheme}://{p.netloc}/"
    SESSION.headers.update({"Referer": REFERER})


# ---------- 找影片網址 ----------
def find_video_urls(page_url):
    response = SESSION.get(page_url, timeout=20)
    response.raise_for_status()
    html_clean = response.text.replace("\\/", "/").replace("\\u002F", "/")

    pattern = re.compile(
        r'''(?:https?:)?//[^"'\\\s<>]+?\.(?:m3u8|mp4|m4v|mpd|webm)(?:\?[^"'\\\s<>]*)?''',
        re.I,
    )
    urls = set()

    soup = BeautifulSoup(html_clean, "html.parser")
    for tag in soup.find_all(["video", "source"]):
        src = tag.get("src")
        if src:
            urls.add(urljoin(response.url, src))

    for m in pattern.finditer(html_clean):
        url = m.group(0)
        if url.startswith("//"):
            url = "https:" + url
        urls.add(url)

    return sorted(urls)


# ---------- 解析度 ----------
def probe_resolution(url):
    """用 ffprobe 讀取影像寬高，回傳 (w, h) 或 None"""
    if not FFPROBE:
        return None
    cmd = [
        FFPROBE, "-v", "error",
        "-user_agent", UA,
        "-headers", f"Referer: {REFERER}\r\n",
        "-rw_timeout", "8000000",          # 8 秒（微秒）
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        url,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        m = re.search(r"(\d+)x(\d+)", out.stdout)
        if m:
            return int(m.group(1)), int(m.group(2))
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def fmt_res(res):
    if not res:
        return "未知"
    w, h = res
    return f"{w}x{h} ({min(w, h)}p)"


# ---------- 大小 ----------
def head_size(url):
    try:
        r = SESSION.head(url, timeout=TIMEOUT, allow_redirects=True)
        size = r.headers.get("Content-Length")
        if r.ok and size and size.isdigit():
            return int(size)
        r = SESSION.get(url, headers={"Range": "bytes=0-0"},
                        timeout=TIMEOUT, stream=True)
        cr = r.headers.get("Content-Range", "")
        r.close()
        if "/" in cr and cr.split("/")[-1].isdigit():
            return int(cr.split("/")[-1])
    except requests.RequestException:
        pass
    return None


def parse_media_playlist(text, base_url):
    """回傳 [(分片url, 秒數, byterange長度或None), ...]"""
    segments, duration, byterange = [], None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            duration = float(line.split(":")[1].split(",")[0])
        elif line.startswith("#EXT-X-BYTERANGE:"):
            byterange = int(line.split(":")[1].split("@")[0])
        elif line and not line.startswith("#"):
            segments.append((urljoin(base_url, line), duration or 0, byterange))
            duration, byterange = None, None
    return segments


def m3u8_info(url, exact=False, sample=SAMPLE, depth=0, res_hint=None):
    """回傳 (大小bytes或None, (w,h)或None)"""
    if depth > 3:
        return None, res_hint
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    text, base = r.text, r.url

    # 主清單 -> 選頻寬最高的子清單，並讀取它標示的解析度
    if "#EXT-X-STREAM-INF" in text:
        best, best_bw, best_res = None, -1, None
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(m.group(1)) if m else 0
                rm = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                res = (int(rm.group(1)), int(rm.group(2))) if rm else None
                for nxt in lines[i + 1:]:
                    if nxt.strip() and not nxt.startswith("#"):
                        if bw > best_bw:
                            best, best_bw, best_res = urljoin(base, nxt.strip()), bw, res
                        break
        if not best:
            return None, None
        return m3u8_info(best, exact, sample, depth + 1, best_res)

    segments = parse_media_playlist(text, base)
    if not segments:
        return None, res_hint

    # 沒有解析度標示 -> 用 ffprobe 讀這份媒體清單
    res = res_hint or probe_resolution(url)

    if all(s[2] for s in segments):
        return sum(s[2] for s in segments), res

    total_duration = sum(s[1] for s in segments)

    if exact:
        with ThreadPoolExecutor(max_workers=16) as ex:
            sizes = list(ex.map(lambda s: head_size(s[0]), segments))
        return (None if any(s is None for s in sizes) else sum(sizes)), res

    step = max(1, len(segments) // sample)
    picks = segments[::step][:sample]
    with ThreadPoolExecutor(max_workers=sample) as ex:
        sizes = list(ex.map(lambda s: head_size(s[0]), picks))
    pairs = [(sz, s[1]) for sz, s in zip(sizes, picks) if sz and s[1]]
    if not pairs:
        return None, res
    bps = sum(p[0] for p in pairs) / sum(p[1] for p in pairs)
    return int(bps * total_duration), res


def get_info(url, exact=False):
    try:
        if url.split("?")[0].lower().endswith(".m3u8"):
            return m3u8_info(url, exact)
        return head_size(url), probe_resolution(url)
    except requests.RequestException:
        return None, None


def human(n):
    if n is None:
        return "未知"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------- 下載 ----------
def safe_name(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "video"


def default_filename(page_url, res):
    slug = safe_name(urlparse(page_url).path.rstrip("/").split("/")[-1])
    tag = f"_{min(res)}p" if res else ""
    return f"{slug}{tag}.mp4"


def download(url, filename):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, filename)

    is_hls = url.split("?")[0].lower().endswith(".m3u8")
    ytdlp = shutil.which("yt-dlp")

    if ytdlp and is_hls:
        base = os.path.splitext(filepath)[0]
        cmd = [
            ytdlp, "-N", "16",
            "--referer", REFERER,
            "--user-agent", UA,
            "--merge-output-format", "mp4",
            "-o", f"{base}.%(ext)s",
            url,
        ]
    elif FFMPEG:
        cmd = [FFMPEG, "-user_agent", UA,
               "-headers", f"Referer: {REFERER}\r\n"]
        if is_hls:
            cmd += ["-allowed_extensions", "ALL"]
        cmd += ["-i", url, "-c", "copy"]
        if is_hls:
            cmd += ["-bsf:a", "aac_adtstoasc"]
        cmd += [filepath]
    else:
        print("找不到 yt-dlp 或 ffmpeg，請先安裝：brew install yt-dlp ffmpeg")
        return

    print("\n執行：", " ".join(f'"{c}"' if " " in c else c for c in cmd), "\n")
    try:
        ret = subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        print("\n下載已中斷")
        return
    print(f"\n完成：{filepath}" if ret == 0 else f"\n下載失敗（代碼 {ret}）")

def choose_and_download(results, page_url):
    # 依解析度、大小由大到小排序
    results.sort(key=lambda r: (min(r[2]) if r[2] else 0, r[1] or 0),
                 reverse=True)

    print("\n===== 結果 =====")
    for i, (url, size, res) in enumerate(results, 1):
        print(f"[{i:>2}] {human(size):>10}  {fmt_res(res):<18}  {url}")

    while True:
        try:
            choice = input(f"\n請輸入要下載第幾個 (1-{len(results)}，q 離開)：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice.lower() in ("q", "quit", "exit", ""):
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(results):
            print("輸入無效，請重新輸入")
            continue

        url, size, res = results[int(choice) - 1]
        default = default_filename(page_url, res)
        name = input(f"檔名（Enter 使用 {default}）：").strip() or default
        if not name.lower().endswith((".mp4", ".mkv", ".ts")):
            name += ".mp4"
        download(url, name)


# ---------- 命令列 ----------
def parse_args():
    parser = argparse.ArgumentParser(
        description="查詢頁面內影片網址的檔案大小與解析度，並可選擇下載"
    )
    parser.add_argument("url", nargs="?", help="要分析的頁面網址")
    parser.add_argument("-n", "--max-sources", type=int,
                        default=DEFAULT_MAX_SOURCES,
                        help=f"最多檢查幾個來源（預設 {DEFAULT_MAX_SOURCES}，0 表示全部）")
    parser.add_argument("--exact", action="store_true",
                        help="m3u8 精確計算大小（較慢）")
    return parser, parser.parse_args()


if __name__ == "__main__":
    parser, args = parse_args()

    # 沒傳網址 -> 顯示輸入參數說明
    if not args.url:
        parser.print_help()
        raise SystemExit(1)

    page_url = args.url
    if not page_url.startswith(("http://", "https://")):
        print(f"網址格式錯誤：{page_url}（需以 http:// 或 https:// 開頭）")
        raise SystemExit(1)

    set_referer(page_url)

    if not FFPROBE:
        print("提示：找不到 ffprobe，沒標示解析度的來源會顯示「未知」"
              "（brew install ffmpeg）\n")
    print("正在讀取頁面...", flush=True)
    try:
        urls = find_video_urls(page_url)
    except requests.RequestException as e:
        print("讀取失敗：", e)
        raise SystemExit(1)

    if args.max_sources:
        urls = urls[:args.max_sources]
    if not urls:
        print("沒有找到任何影片網址")
        raise SystemExit(1)
    print(f"找到 {len(urls)} 個網址，開始查詢大小與解析度...\n", flush=True)

    results = []
    ex = ThreadPoolExecutor(max_workers=6)
    try:
        futures = {ex.submit(get_info, u, args.exact): u for u in urls}
        for done, fut in enumerate(as_completed(futures), 1):
            size, res = fut.result()
            results.append((futures[fut], size, res))
            print(f"  查詢進度 {done}/{len(urls)}  {human(size):>10}  "
                  f"{fmt_res(res)}", flush=True)
    except KeyboardInterrupt:
        print("\n已中斷查詢，使用目前已完成的結果")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    if results:
        choose_and_download(results, page_url)