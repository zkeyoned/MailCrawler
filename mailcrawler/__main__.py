# -*- coding: utf-8 -*-
"""桌面入口：单实例锁 + 本地服务 + pywebview 窗口。

运行：python -m mailcrawler
打包后由 PyInstaller 直接调用此模块。
"""
import os
import sys


def acquire_lock():
    """单实例锁：第二个实例直接退出（旧版多窗口会抢同一个分类队列）。"""
    from mailcrawler.paths import lock_path   # 绝对导入：PyInstaller 把本文件当顶层脚本跑，相对导入会崩
    p = lock_path()
    if p.exists():
        try:
            pid = int(p.read_text().strip())
            os.kill(pid, 0)          # 进程还活着吗
            return None
        except (ValueError, OSError, ProcessLookupError):
            try:
                p.unlink()            # 残留锁文件（上次异常退出留下的），删掉重来
            except OSError:
                pass
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return p
    except FileExistsError:
        return None


def main():
    lock = acquire_lock()
    if lock is None:
        try:
            import webview
            webview.create_window("邮件爬虫", html="<h3 style='font-family:sans-serif'>邮件爬虫已经在运行，"
                                                  "请用原来的窗口。</h3>", width=360, height=140)
            webview.start()
        except Exception:
            print("邮件爬虫已经在运行。", file=sys.stderr)
        return 1

    from mailcrawler import server
    srv, port = server.create_server()
    server.start_auto_sync()
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()

    try:
        import webview
        window = webview.create_window("邮件爬虫", "http://127.0.0.1:%d/" % port,
                                       width=1280, height=820, min_size=(960, 600))
        webview.start(debug=False)
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            lock.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
