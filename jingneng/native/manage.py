"""Per-user Windows runtime for the Odoo Community customer demonstration."""

import _thread
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import psutil

RUNTIME = Path(__file__).resolve().parent
ROOT = RUNTIME.parent
PG_BIN = RUNTIME / "pgsql" / "bin"
SERVER = RUNTIME / "server"
ODOO = SERVER / "odoo-bin"
LOGS = ROOT / "日志"
MANAGER_LOG = LOGS / "初始化.log"
SETTINGS = ROOT / "设置.json"
PG_DATA = ROOT / "pgdata"
ODOO_CONFIG = ROOT / "odoo.conf"
ODOO_DATA = ROOT / "odoo-data"
STATE = ROOT / "odoo进程.json"
STOP = ROOT / "停止请求"
READY = ROOT / ".demo-ready"
LOGIN = "demo-admin@odoo-preview.example"
DATABASE = "odoo_customer_demo"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def report(text):
    print(text, flush=True)


def write(path, text):
    temp = path.with_name(path.name + ".new")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def free_port(first, count):
    for port in range(first, first + count):
        with socket.socket() as candidate:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                candidate.bind(("127.0.0.1", port))
                return port
            except OSError:
                pass
    raise RuntimeError("本机没有可用的演示端口。请关闭占用端口的程序后重试。")


def settings():
    if SETTINGS.exists():
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        assert set(data) == {"http_port", "pg_port", "db_password", "admin_password", "master_password"}
        assert 1024 <= data["http_port"] <= 65535 and 1024 <= data["pg_port"] <= 65535
        assert data["http_port"] != data["pg_port"]
        for key in ("db_password", "admin_password", "master_password"):
            assert re.fullmatch(r"[a-zA-Z0-9!_-]{24,80}", data[key])
        return data
    data = {
        "http_port": free_port(8069, 29),
        "pg_port": free_port(15432, 30),
        "db_password": secrets.token_hex(18),
        "admin_password": "Odoo!" + secrets.token_hex(18),
        "master_password": secrets.token_hex(18),
    }
    write(SETTINGS, json.dumps(data, ensure_ascii=False, indent=2))
    return data


def url(data):
    return "http://127.0.0.1:" + str(data["http_port"])


def child_environment(data):
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PATH"] = os.pathsep.join([
        str(Path(sys.executable).parent), str(PG_BIN), str(RUNTIME / "thirdparty"), environment.get("PATH", ""),
    ])
    environment.update({
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1",
        "PGHOST": "127.0.0.1", "PGPORT": str(data["pg_port"]), "PGUSER": "odoo",
        "PGPASSWORD": data["db_password"], "PGCLIENTENCODING": "UTF8", "ODOO_RC": str(ODOO_CONFIG),
        "ODOO_CUSTOMER_ADMIN_PASSWORD": data["admin_password"], "ODOO_CUSTOMER_ADDRESS": url(data),
    })
    return environment


def last_log(path, data):
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 4700))
            content = handle.read()
    except OSError:
        return ""
    try:
        detail = content.decode("utf-8")
    except UnicodeDecodeError:
        detail = content.decode("mbcs", errors="replace")
    for key in ("admin_password", "db_password", "master_password"):
        detail = detail.replace(data[key], "[密码]")
    return "\n".join(detail.strip().splitlines()[-12:])


def run(label, arguments, data, duration):
    with MANAGER_LOG.open("ab") as output:
        output.write(("\n" + label + "\n").encode("utf-8"))
        output.flush()
        try:
            outcome = subprocess.run(arguments, cwd=ROOT, env=child_environment(data), stdout=output,
                                     stderr=subprocess.STDOUT, creationflags=NO_WINDOW, timeout=duration, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError(label + "超时。详情见 " + str(MANAGER_LOG)) from None
    if outcome.returncode:
        raise RuntimeError(label + "失败（" + str(outcome.returncode) + "）。\n" + last_log(MANAGER_LOG, data))


def create_postgres(data):
    if (PG_DATA / "PG_VERSION").is_file():
        return
    if PG_DATA.exists():
        raise RuntimeError("数据库目录存在但结构不完整。请保留该目录并联系提供安装包的人员。")
    report("初始化本机 PostgreSQL 数据目录…")
    pending = ROOT / (".pg-init-" + secrets.token_hex(5))
    # initdb puts its native Windows share-directory path into bootstrap SQL.
    # Use ASCII for the system templates if that path has non-ASCII characters.
    # Odoo explicitly creates and checks its demonstration database as UTF-8.
    template_encoding = "UTF8" if str(PG_BIN).isascii() else "SQL_ASCII"
    password_file = ROOT / ".pg-init-password"
    write(password_file, data["db_password"] + "\n")
    try:
        run("初始化 PostgreSQL", [
            str(PG_BIN / "initdb.exe"), "-D", pending.name, "--username=odoo",
            "--encoding=" + template_encoding, "--locale=C", "--auth=scram-sha-256", "--pwfile=" + password_file.name,
        ], data, 90)
        with (pending / "postgresql.conf").open("a", encoding="utf-8") as handle:
            handle.write("\nlisten_addresses = '127.0.0.1'\nport = " + str(data["pg_port"]) + "\n"
                         "password_encryption = 'scram-sha-256'\n")
        pending.rename(PG_DATA)
    finally:
        password_file.unlink(missing_ok=True)


def postgres_status(data):
    return subprocess.run([str(PG_BIN / "pg_ctl.exe"), "-D", PG_DATA.name, "status"],
                          cwd=ROOT, env=child_environment(data), stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=10, check=False).returncode


def start_postgres(data):
    if postgres_status(data) == 0:
        return
    report("启动本机 PostgreSQL…")
    run("启动 PostgreSQL", [
        str(PG_BIN / "pg_ctl.exe"), "-D", PG_DATA.name, "-l", str(LOGS / "postgresql.log"),
        "-w", "-t", "55", "start",
    ], data, 65)


def write_odoo_config(data):
    ODOO_DATA.mkdir(exist_ok=True)
    write(ODOO_CONFIG,
          "[options]\n" + "admin_passwd = " + data["master_password"] + "\n"
          "http_interface = 127.0.0.1\nhttp_port = " + str(data["http_port"]) + "\n"
          "db_host = 127.0.0.1\ndb_port = " + str(data["pg_port"]) + "\ndb_user = odoo\n"
          "db_password = " + data["db_password"] + "\n"
          "db_name = " + DATABASE + "\ndbfilter = ^" + DATABASE + "$\nlist_db = False\n"
          "data_dir = " + ODOO_DATA.as_posix() + "\npg_path = " + PG_BIN.as_posix() + "\n"
          "logfile = " + (LOGS / "odoo.log").as_posix() + "\nworkers = 0\n"
          "smtp_server = 127.0.0.1\nsmtp_port = 1025\n")


def running_odoo():
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        process = psutil.Process(state["pid"])
        token = state["token"]
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            return None
        if Path(process.exe()).resolve() == Path(sys.executable).resolve() and process.cmdline()[-2:] == ["serve", token]:
            return process, token
    except (OSError, KeyError, ValueError, psutil.Error):
        pass
    return None


def browser_ready(data):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    login_url = url(data) + "/web/login?db=" + DATABASE
    try:
        with opener.open(login_url, timeout=3) as response:
            return response.status == 200 and "/web/login" in response.url and b"odoo" in response.read(24000).lower()
    except (OSError, urllib.error.URLError, ValueError):
        return False


def start_odoo(data):
    active = running_odoo()
    if active and browser_ready(data):
        return
    process = None
    if not active:
        with socket.socket() as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("127.0.0.1", data["http_port"]))
            except OSError:
                raise RuntimeError("Odoo 的本机端口已被其他程序占用。请释放端口后再打开。") from None
        report("启动本机 Odoo 网页服务…")
        token = secrets.token_hex(16)
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        with (LOGS / "odoo启动.log").open("ab") as output:
            process = subprocess.Popen([sys.executable, str(Path(__file__)), "serve", token],
                                       cwd=ROOT, env=child_environment(data), stdout=output, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    end = time.monotonic() + 75
    while time.monotonic() < end:
        if running_odoo() and browser_ready(data):
            return
        if process is not None and process.poll() is not None:
            break
        time.sleep(0.65)
    raise RuntimeError("Odoo 登录页未就绪。\n" + last_log(LOGS / "odoo启动.log", data))


def serve(token):
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise RuntimeError("启动参数无效。")
    write(STATE, json.dumps({"pid": os.getpid(), "token": token}))

    def watch_stop():
        while True:
            try:
                requested = STOP.read_text(encoding="utf-8").strip() == token
            except OSError:
                requested = False
            if requested:
                _thread.interrupt_main(signal.SIGINT)
                return
            time.sleep(0.5)

    threading.Thread(target=watch_stop, daemon=True).start()
    sys.path.insert(0, str(SERVER))
    sys.argv = [str(ODOO), "server", "-c", str(ODOO_CONFIG)]
    try:
        from odoo.cli import main as odoo_main
        odoo_main()
    finally:
        with contextlib.suppress(OSError, ValueError):
            if json.loads(STATE.read_text(encoding="utf-8")).get("token") == token:
                STATE.unlink(missing_ok=True)


def stop(data):
    active = running_odoo()
    if active:
        process, token = active
        report("正在等待 Odoo 正常退出，最长约 1 分钟…")
        write(STOP, token)
        try:
            process.wait(timeout=75)
        except psutil.TimeoutExpired:
            raise RuntimeError("Odoo 尚未正常退出，PostgreSQL 仍在运行。请稍后重试停止。") from None
        STOP.unlink(missing_ok=True)
    if (PG_DATA / "PG_VERSION").is_file() and postgres_status(data) == 0:
        run("停止 PostgreSQL", [str(PG_BIN / "pg_ctl.exe"), "-D", PG_DATA.name, "-m", "fast",
                                "-w", "-t", "35", "stop"], data, 45)
    report("Odoo 本机演示已停止。")


def instructions(data):
    note = ROOT / "使用说明.txt"
    if note.exists():
        return
    lines = [
        "Odoo 社区版原生 Windows 演示", "", "本机地址  " + url(data),
        "管理员账号  " + LOGIN, "本机测试密码  " + data["admin_password"],
        "演示数据库  " + DATABASE, "版本  Odoo 20.0-20261112，PostgreSQL 16.16", "",
        "在桌面双击「打开 Odoo 原生演示」即可使用。销售里有「客户演示订单」和「客户演示报价」。",
        "关闭浏览器后，演示仍在后台运行。双击本目录的「停止 Odoo 演示.cmd」可停止后台。",
        "如需移除这套演示，先停止后台并关闭打开的文件，再删除本目录及桌面快捷方式。",
        "Odoo 和 PostgreSQL 的监听地址均为本机 127.0.0.1。",
        "报价邮件可在界面预览。实际投递需本机邮件接收服务。",
        "第三方许可证和下载来源见 runtime 目录中的「组件来源.txt」。", "",
    ]
    write(note, "\n".join(lines))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "open"
    if mode == "serve" and len(sys.argv) == 3:
        serve(sys.argv[2])
        return
    if mode not in ("open", "install", "stop"):
        raise RuntimeError("启动模式无效。")
    if mode == "open" and not READY.is_file():
        raise RuntimeError("演示尚未安装完成，请运行原生安装包。")
    if mode == "stop" and not SETTINGS.is_file():
        report("当前没有运行中的本机演示。")
        return
    LOGS.mkdir(exist_ok=True)
    data = settings()
    if mode == "stop":
        stop(data)
        return
    write_odoo_config(data)
    create_postgres(data)
    start_postgres(data)
    if not READY.is_file():
        report("加载中文官方演示数据并安装五个应用，通常需要几分钟…")
        run("初始化 Odoo 中文演示库", [sys.executable, str(RUNTIME / "bootstrap.py")], data, 1100)
    start_odoo(data)
    instructions(data)
    if not READY.is_file():
        write(READY, "1\n")
    report("READY " + url(data))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        sys.exit(1)
