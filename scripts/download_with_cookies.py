"""
批量下载抖音视频（双模式）
优先 yt-dlp（速度快），失败自动切换 greenvideo.cc 解析下载
文件夹名用数字序号
按视频 ID 恢复，校验媒体文件，结构化报告逐项结果；依赖更新不在下载时执行
"""
import json
import subprocess
import sys
import os
import re
import time
import zipfile
import urllib.request
from pathlib import Path

# skill 根目录
SKILL_DIR = Path(__file__).parent.parent
PARSER_DIR = SKILL_DIR / "scripts" / "video_parser"
# 便携版 Node.js 存放目录
NODE_DIR = SKILL_DIR / "bin" / "node"

# 直链下载必须带浏览器 UA，否则抖音/B站 CDN 返回 403
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

# 文件名清理规则（与整理脚本共用）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naming import safe_name, clean_title  # noqa: E402
from command_runner import run_command
from task_state import (atomic_text, emit_event, find_video, load_state,
                        local_index, read_video_id, save_state, valid_video)


def ensure_dependencies():
    """自动检查并安装所需依赖"""
    required_packages = {
        "yt-dlp": "yt_dlp",
    }
    
    for package_name, import_name in required_packages.items():
        try:
            __import__(import_name)
        except ImportError:
            print(f"📦 正在安装依赖: {package_name}...")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", package_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print(f"✅ {package_name} 安装完成")


def update_ytdlp(max_age_hours: int = 24):
    """自动更新 yt-dlp 到最新版本（同一台机器 24 小时内只检查一次，避免每次启动都等 pip）"""
    stamp = SKILL_DIR / "work" / ".ytdlp_update_stamp"
    try:
        if stamp.exists() and (time.time() - stamp.stat().st_mtime) < max_age_hours * 3600:
            print("✅ yt-dlp 最近已检查过，跳过更新")
            return
    except Exception:
        pass

    print("🔄 检查 yt-dlp 更新...")
    try:
        result = run_command(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True,
            text=True,
            timeout=180
        )
        if "Successfully installed" in result.stdout:
            print("✅ yt-dlp 已更新到最新版")
        else:
            print("✅ yt-dlp 已是最新版")
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass
    except Exception as e:
        print(f"⚠️ yt-dlp 更新失败，继续使用当前版本: {e}")


def ensure_nodejs():
    """
    检查 Node.js 是否可用：
    1. 先找系统安装的 node
    2. 没有就下载便携版 Node.js 到 skill/bin/node 目录
    """
    # 1. 先检查系统有没有 node
    try:
        result = run_command(["where", "node"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            node_path = result.stdout.strip().split('\n')[0]
            # 验证能运行
            v = run_command([node_path, "--version"], capture_output=True, text=True, timeout=5)
            if v.returncode == 0:
                print(f"✅ 检测到系统 Node.js: {v.stdout.strip()}")
                return
    except Exception:
        pass

    # 2. 检查便携版是否已存在
    portable_node = NODE_DIR / "node.exe"
    if portable_node.exists():
        v = run_command([str(portable_node), "--version"], capture_output=True, text=True, timeout=5)
        if v.returncode == 0:
            print(f"✅ 使用便携版 Node.js: {v.stdout.strip()}")
            return

    # 3. 下载便携版 Node.js
    print("📦 未检测到 Node.js，正在下载便携版...")
    NODE_DIR.parent.mkdir(parents=True, exist_ok=True)

    # Node.js LTS 版本（Windows x64 便携版 zip）
    node_version = "v20.17.0"
    node_zip_name = f"node-{node_version}-win-x64"
    node_url = f"https://nodejs.org/dist/{node_version}/{node_zip_name}.zip"
    zip_path = SKILL_DIR / "bin" / f"{node_zip_name}.zip"

    try:
        # 下载
        print(f"  ⬇️ 下载 Node.js {node_version}...")
        urllib.request.urlretrieve(node_url, zip_path)

        # 解压
        print(f"  📂 解压中...")
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(SKILL_DIR / "bin")

        # 移动 node 目录到 NODE_DIR
        extracted_dir = SKILL_DIR / "bin" / node_zip_name
        if NODE_DIR.exists():
            import shutil
            shutil.rmtree(NODE_DIR)
        extracted_dir.rename(NODE_DIR)

        # 清理 zip
        zip_path.unlink()

        # 验证
        v = run_command([str(portable_node), "--version"], capture_output=True, text=True, timeout=5)
        if v.returncode == 0:
            print(f"✅ 便携版 Node.js 安装完成: {v.stdout.strip()}")
        else:
            print("⚠️ 便携版 Node.js 可能安装失败")

    except Exception as e:
        print(f"⚠️ 下载 Node.js 失败: {e}")
        print("   greenvideo.cc 解析下载可能不可用，仅使用 yt-dlp")


def get_node_path():
    """获取 node.exe 路径：优先系统 node，否则用便携版"""
    # 先试系统 node
    try:
        result = run_command(["where", "node"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip().split('\n')[0]
    except Exception:
        pass
    # 再试便携版
    portable_node = NODE_DIR / "node.exe"
    if portable_node.exists():
        return str(portable_node)
    # 都没有就返回 "node" 硬试
    return "node"


# Dependency setup is explicit; importing this module never installs or updates software.


def download_video_ytdlp(url: str, output_dir: Path, video_name: str, cookies_file: str = None):
    """
    使用 yt-dlp 下载单个视频
    返回: (success, video_file, error_msg)
    """
    staging = output_dir / "_download"
    staging.mkdir(exist_ok=True)
    output_template = str(staging / f"{video_name}.%(ext)s")

    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "-f", "best", "-o", output_template, "--no-playlist", "--merge-output-format", "mp4", "--remux-video", "mp4"]
    
    if cookies_file:
        cmd.extend(["--cookies", cookies_file])
    
    cmd.append(url)

    try:
        result = run_command(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8"
        )

        if result.returncode == 0:
            files = [p for p in staging.glob(f"{video_name}.mp4") if valid_video(p)]
            if not files:
                return False, None, "yt-dlp 未生成完整有效的 MP4"
            video_file = output_dir / f"{video_name}.mp4"
            files[0].replace(video_file)
            return True, video_file, None
        else:
            return False, None, result.stderr[:200]

    except FileNotFoundError:
        return False, None, "yt-dlp 未安装"
    except subprocess.TimeoutExpired:
        return False, None, "下载超时（120秒）"
    except Exception as e:
        return False, None, str(e)


def download_video_greenvideo(url: str, output_dir: Path, video_name: str, max_retries: int = 6):
    """
    使用 greenvideo.cc 解析下载视频（带重试）
    流程：先运行 video_extract.cjs --json 获取直链，再用 curl 下载
    返回: (success, video_file, error_msg)
    """
    extract_script = PARSER_DIR / "video_extract.cjs"

    if not extract_script.exists():
        return False, None, "greenvideo 解析脚本不存在"

    node_path = get_node_path()

    for attempt in range(1, max_retries + 1):
        print(f"  🔄 greenvideo.cc 解析下载（第 {attempt}/{max_retries} 次尝试）...")

        # 第一步：解析获取视频直链
        direct_url = None
        try:
            result = run_command(
                [node_path, str(extract_script), "--json", url],
                capture_output=True,
                text=True,
                timeout=35,
                encoding="utf-8"
            )
            output = result.stdout + result.stderr

            # 提取 JSON 部分（__GV_JSON_BEGIN__ 和 __GV_JSON_END__ 之间）
            import re
            json_match = re.search(r'__GV_JSON_BEGIN__(.+?)__GV_JSON_END__', output, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(1))
                if data.get("success") and data.get("data", {}).get("videoItemVoList"):
                    for item in data["data"]["videoItemVoList"]:
                        if item.get("fileType") == "video" and item.get("baseUrl"):
                            direct_url = item["baseUrl"]
                            break

            if not direct_url:
                if attempt < max_retries:
                    wait_time = 4 * attempt
                    print(f"  ⏳ 解析未获取到直链，{wait_time} 秒后重试...")
                    time.sleep(wait_time)
                    continue
                else:
                    return False, None, "解析未获取到视频直链"

        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                print(f"  ⏳ 解析超时，重试...")
                time.sleep(5)
                continue
            else:
                return False, None, "greenvideo 解析超时"
        except Exception as e:
            if attempt < max_retries:
                print(f"  ⏳ 解析出错: {e}，重试...")
                time.sleep(5)
                continue
            else:
                return False, None, f"greenvideo 解析异常: {e}"

        # 第二步：用 curl 直接下载视频直链
        print(f"  ⬇️ 获取到直链，开始下载...")
        output_file = output_dir / f"{video_name}.mp4"
        partial_file = output_file.with_suffix(".mp4.part")
        try:
            # 抖音/B站 CDN 都校验 UA 和 Referer，缺了会返回 403 或空文件
            curl_cmd = [
                "curl.exe", "-L", "--fail", "--show-error", "--silent", "-o", str(partial_file),
                "-H", f"User-Agent: {UA}",
                "-H", "Referer: https://www.douyin.com/",
                "--connect-timeout", "20",
                "--max-time", "300",
                direct_url,
            ]
            result = run_command(
                curl_cmd,
                capture_output=True,
                text=True,
                timeout=310
            )
            if result.returncode == 0 and valid_video(partial_file):
                partial_file.replace(output_file)
                size_mb = output_file.stat().st_size / 1024 / 1024
                print(f"  ✅ 下载完成: {output_file.name} ({size_mb:.1f} MB)")
                return True, output_file, None
            else:
                real = partial_file.stat().st_size if partial_file.exists() else 0
                partial_file.unlink(missing_ok=True)
                print(f"  ⚠️ curl 返回 {result.returncode}: {(result.stderr or chr(32))[:160]}")
                if attempt < max_retries:
                    print(f"  ⏳ 下载文件无效（{real} 字节），重试...")
                    time.sleep(3)
                    continue
                else:
                    return False, None, f"下载文件无效（{real} 字节）"
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                print(f"  ⏳ 下载超时，重试...")
                time.sleep(5)
                continue
            else:
                return False, None, "视频下载超时"
        except Exception as e:
            if attempt < max_retries:
                print(f"  ⏳ 下载出错: {e}，重试...")
                time.sleep(5)
                continue
            else:
                return False, None, f"视频下载异常: {e}"

    return False, None, "greenvideo 下载失败（重试次数已用完）"


def download_video(url: str, output_dir: Path, video_name: str, cookies_file: str = None):
    """
    智能下载：优先 yt-dlp，失败自动切换 greenvideo.cc
    返回: (success, video_file, method_used, error_msg)
    """
    # 第一步：尝试 yt-dlp
    print("  📥 尝试 yt-dlp 下载...")
    success, video_file, error = download_video_ytdlp(url, output_dir, video_name, cookies_file)
    
    if success:
        return True, video_file, "yt-dlp", None
    
    print(f"  ⚠️ yt-dlp 失败: {error[:100]}")
    print("  🔄 切换到 greenvideo.cc 解析下载...")
    
    # 第二步：尝试 greenvideo.cc
    success, video_file, error = download_video_greenvideo(url, output_dir, video_name)
    
    if success:
        return True, video_file, "greenvideo.cc", None
    else:
        return False, None, "both_failed", error


def download_favorites(json_file: str, cookies_file: str, base_dir: str = str(Path.home() / "抖音视频下载")):
    """Resume by canonical ID; return truthful counts for this batch only."""
    base_path = Path(base_dir)
    base_path.mkdir(parents=True, exist_ok=True)
    videos = json.loads(Path(json_file).read_text(encoding="utf-8"))
    videos = list({str(v["video_id"]): v for v in videos}.values())
    if not videos:
        return {"ok": 0, "fail": 0, "failed_ids": []}
    date_str = Path(json_file).stem.replace("douyin_favorites_", "")
    if date_str == "video_list":
        date_str = Path(json_file).parent.name
    date_dir = base_path / date_str
    date_dir.mkdir(parents=True, exist_ok=True)
    index = local_index(base_path)
    completed, failed, ready = set(), set(), []
    transcribe = os.environ.get("SKILL_SKIP_TRANSCRIBE") != "1"
    next_number = max((int(p.name.split("_")[0]) for p in date_dir.iterdir()
                       if p.is_dir() and p.name.split("_")[0].isdigit()), default=0) + 1

    for i, video in enumerate(videos, 1):
        vid = str(video["video_id"])
        raw_title = clean_title((video.get("title") or "").strip())
        directory = index.get(vid)
        if directory is None:
            short = safe_name(raw_title, 16)
            directory = date_dir / (f"{next_number:02d}" + (f"_{short}" if short else ""))
            while directory.exists():
                next_number += 1
                directory = date_dir / f"{next_number:02d}_{vid}"
            next_number += 1
            directory.mkdir(parents=True)
            index[vid] = directory
        emit_event(video_id=vid, phase="download", status="running", directory=str(directory),
                   current=i, total=len(videos))
        print(f"\n[{i}/{len(videos)}] 下载: {raw_title[:30] or vid}...")
        try:
            save_state(directory, video_id=vid, url=video["url"],
                       title=raw_title or load_state(directory).get("title", ""))
            info = directory / "视频信息.txt"
            if not info.exists():
                atomic_text(info, f"视频ID: {vid}\n链接: {video['url']}\n博主: {video.get('author', '')}\n标题:\n{video.get('title', '')}\n")
            video_file = find_video(directory)
            if video_file:
                print(f"  ⏭ 已有有效视频，跳过下载: {video_file.name}")
            else:
                save_state(directory, download_status="running", download_error="")
                success, video_file, method, error = download_video(
                    video["url"], directory, f"video_{vid}", cookies_file)
                if not success or not video_file or not valid_video(video_file):
                    raise RuntimeError(error or "下载产物校验失败")
                pretty = safe_name(raw_title, 50)
                if pretty:
                    target = directory / f"{pretty}.mp4"
                    if target != video_file and not target.exists():
                        video_file.rename(target)
                        video_file = target
                print(f"  ✅ 下载成功: {video_file.name} (方式: {method})")
            save_state(directory, download_status="done", download_error="", video_file=video_file.name)
            emit_event(video_id=vid, phase="download", status="done", directory=str(directory))
            ready.append(directory)
            if not transcribe:
                completed.add(vid)
        except Exception as exc:
            failed.add(vid)
            try:
                save_state(directory, download_status="error", download_error=str(exc))
            except Exception:
                pass
            print(f"  ❌ 下载失败: {exc}")
            emit_event(video_id=vid, phase="download", status="error", error=str(exc))

    if transcribe and ready:
        print("🎤 自动开始飞书妙记语音转写...")
        from batch_transcribe import transcribe_directories
        result = transcribe_directories(ready)
        bad = {str(Path(p).resolve()) for p in result["failed_dirs"]}
        for directory in ready:
            vid = read_video_id(directory)
            (failed if str(directory.resolve()) in bad else completed).add(vid)
    report = {"ok": len(completed), "fail": len(failed), "failed_ids": sorted(failed)}
    emit_event(phase="summary", **report)
    print(f"任务结束：完成 {report['ok']}，失败 {report['fail']}")
    return report


if __name__ == "__main__":
    # Server assigns a Windows Job Object before permitting any child processes.
    if os.environ.get("DY_START_GATE") == "1" and sys.stdin.readline().strip() != "GO":
        sys.exit(1)
    if len(sys.argv) < 2:
        print("用法: python download_with_cookies.py <视频列表JSON> <cookies.txt> [基础目录]")
        sys.exit(1)
    try:
        result = download_favorites(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None,
                                    sys.argv[3] if len(sys.argv) > 3 else str(Path.home() / "抖音视频下载"))
        sys.exit(2 if result["fail"] else 0)
    except Exception as exc:
        print(f"❌ 任务异常: {exc}", flush=True)
        sys.exit(1)
