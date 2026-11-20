"""Replace the original locally installed Odoo database with the Jingneng demo."""

import argparse
from contextlib import closing
from datetime import datetime
from hashlib import file_digest
from html import unescape
import json
import os
from pathlib import Path
import re
from runpy import run_path
import shutil
import subprocess
import sys
from time import monotonic, sleep
from types import SimpleNamespace

import psycopg2
from psycopg2 import sql


DEMO = Path(__file__).resolve().parents[1]
OLD_ROOT = Path(r"C:\Users\Administrator\AppData\Local\OdooNativePreview")
SOURCE_ROOT = DEMO / "隔离验收"
ORIGINAL_DB = "odoo_customer_demo"
SOURCE_DB = "odoo_jingneng_demo"
SOURCE_DATA = DEMO / "私有导入" / "现行已报价单.json"
BACKUPS = DEMO / "私有导入" / "备份"


def checked(condition, message):
    if not condition:
        raise RuntimeError(message)


class Installation:
    def __init__(self, root, database, ports):
        self.root = root
        self.database = database
        self.runtime = root / "runtime"
        self.python = self.runtime / "python" / "python.exe"
        self.manage = self.runtime / "manage.py"
        self.modules = run_path(str(self.manage))
        self.settings = json.loads((root / "设置.json").read_text(encoding="utf-8"))
        self.filestore = root / "odoo-data" / "filestore"
        self.pg_bin = self.runtime / "pgsql" / "bin"
        checked(self.modules["DATABASE"] == database and (root / ".demo-ready").is_file()
                and (self.settings["http_port"], self.settings["pg_port"]) == ports,
                "The local installation or its database identity changed")
        self.environment = self.modules["child_environment"](self.settings)
        self.environment["PATH"] = os.pathsep.join((str(self.python.parent), self.environment["PATH"]))
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def command(self, program, args, code=None, timeout=180):
        result = subprocess.run([str(program), *map(str, args)], cwd=self.root, env=self.environment,
                                input=code, capture_output=True, timeout=timeout, check=False)
        if result.returncode:
            details = result.stderr.decode("utf-8", errors="replace")[-3500:]
            for key in ("db_password", "admin_password", "master_password"):
                details = details.replace(self.settings[key], "[redacted]")
            raise RuntimeError(f"{Path(program).name} failed ({result.returncode}): {details}")
        return result.stdout.decode("utf-8", errors="replace")

    def pg(self, tool, *args):
        return self.command(self.pg_bin / (tool + ".exe"), args)

    def connect(self, database):
        return psycopg2.connect(host="127.0.0.1", port=self.settings["pg_port"], user="odoo",
                                password=self.settings["db_password"], dbname=database, connect_timeout=8)

    def dump(self, path):
        self.pg("pg_dump", "--no-password", "-Fc", "-f", path, self.database)
        self.check_dump(path)

    def check_dump(self, path):
        catalog = self.pg("pg_restore", "--list", path)
        checked(path.stat().st_size > 0 and all(f"TABLE DATA public {table} " in catalog
                for table in ("sale_order", "res_partner", "ir_attachment")), "Database backup is incomplete")

    def restart_web(self):
        result = self.command(self.python, (self.manage, "open"), timeout=95)
        checked(f"READY http://127.0.0.1:{self.settings['http_port']}" in result,
                "The existing launcher did not report readiness")

    def stop_web(self):
        code = """import json
import socket
from pathlib import Path
from runpy import run_path
import psutil
import psycopg2

manager = run_path(str(Path.cwd() / 'runtime' / 'manage.py'))
settings = manager['settings']()
active = manager['running_odoo']()
if active:
    process, token = active
    manager['write'](manager['STOP'], token)
    try:
        process.wait(timeout=8)
    except psutil.TimeoutExpired:
        with psycopg2.connect(host='127.0.0.1', port=settings['pg_port'], user='odoo',
                              password=settings['db_password'], dbname='postgres') as connection, connection.cursor() as query:
            query.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND state IS DISTINCT FROM 'idle'",
                          (manager['DATABASE'],))
            if query.fetchone()[0]:
                raise RuntimeError('The old database still has active work')
        if process.is_running():
            if Path(process.exe()).resolve() != Path(manager['sys'].executable).resolve() or process.cmdline()[-2:] != ['serve', token]:
                raise RuntimeError('The process identity changed during shutdown')
            process.terminate()
            process.wait(timeout=10)
    if manager['STATE'].exists() and json.loads(manager['STATE'].read_text(encoding='utf-8')).get('token') == token:
        manager['STATE'].unlink(missing_ok=True)
    manager['STOP'].unlink(missing_ok=True)
with socket.socket() as probe:
    probe.settimeout(2)
    if probe.connect_ex(('127.0.0.1', settings['http_port'])) == 0:
        raise RuntimeError('The Odoo HTTP port remains active after shutdown')
"""
        self.command(self.python, ("-",), code.encode("utf-8"), timeout=35)

    def shell(self, database, setup=False):
        script = str(Path(__file__).resolve())
        invoke = f"from runpy import run_path\nrun_path({script!r})['odoo_check'](env, {database!r}, {setup!r})\n"
        output = self.command(self.python, (self.runtime / "server" / "odoo-bin", "shell", "-c",
                              self.root / "odoo.conf", "-d", database, "--no-http", "--log-level=warn"),
                              invoke.encode("utf-8"))
        result = next(line.removeprefix("CUTOVER_ORM=") for line in output.splitlines()
                      if line.startswith("CUTOVER_ORM="))
        return json.loads(result)


def odoo_check(env, database, setup):
    from odoo import SUPERUSER_ID, api

    checked(env.cr.dbname == database and (database == ORIGINAL_DB or re.fullmatch(r"jingneng_stage_\d{8}_\d{6}", database)),
            "Odoo shell connected to the wrong database")
    verifier = run_path(str(DEMO / "私有导入" / "替换演示报价.py"))
    env = api.Environment(env.cr, SUPERUSER_ID, {
        "lang": "zh_CN", "tracking_disable": True, "mail_notrack": True, "send_email": False,
    })
    checked(env["ir.config_parameter"].get_bool(verifier["MARKER"]), "The replacement quote import is missing")
    settings = json.loads((OLD_ROOT / "设置.json").read_text(encoding="utf-8"))
    administrator = env.ref("base.user_admin")
    checked(administrator.login == "demo-admin@odoo-preview.example", "Administrator login differs")
    cases = json.loads(SOURCE_DATA.read_text(encoding="utf-8"))["quotes"]
    expected = {verifier["ref_for"](case): case for case in cases}
    checked(len(expected) == len(cases) == 36, "Source quotation list differs from the accepted data")
    orders = env["sale.order"].search([], order="id")
    quotes = orders.filtered(lambda order: (order.client_order_ref or "").startswith("AIQUOTE:"))
    checked(len(quotes) == len(expected) and set(quotes.mapped("client_order_ref")) == expected.keys(),
            "Quotation references differ from the Jingneng source")
    confirmed = orders - quotes
    checked(len(confirmed) == 1 and confirmed.name == "S00003" and confirmed.state == "sale"
            and confirmed.client_order_ref == "JN-DEMO-SO-READY" and confirmed.picking_ids,
            "The Jingneng confirmed order or its delivery is missing")
    units = {line["unit"] for case in cases for line in case["lines"]}
    uom = {unit: env["uom.uom"].search([("name", "=", unit)]) for unit in units}
    checked(all(len(record) == 1 for record in uom.values()), "A quotation unit does not resolve uniquely")

    def note_text(markup):
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", str(markup))).split())

    rows = []
    for order in quotes:
        case = expected[order.client_order_ref]
        rows.append(verifier["verify_order"](order, case, uom))
        checked(note_text(order.note) == note_text(verifier["order_note"](case)), "Formula quote note differs")
    lookup = run_path(str(DEMO / "私有导入" / "核对并清理默认客户.py"))["fixture_records"]
    checked(not lookup(env, OLD_ROOT / "runtime" / "server" / "odoo" / "addons"),
            "Official demo customers are present in the replacement database")
    config = env["ir.config_parameter"]
    origin = f"http://127.0.0.1:{settings['http_port']}"
    if setup:
        administrator.write({"password": settings["admin_password"]})
        config.set_str("web.base.url", origin)
        config.set_bool("web.base.url.freeze", True)
        env.cr.commit()
    else:
        checked(config.get_str("web.base.url") == origin, "The database advertises the wrong origin")
    print("CUTOVER_ORM=" + json.dumps({"rows": rows, "confirmed_order": confirmed.name,
          "companies": len(set(quotes.mapped("partner_id").ids)), "lines": len(quotes.order_line)}), flush=True)


def materialize_filestore(source, target, stage, resume):
    with target.connect(stage) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT DISTINCT store_fname, checksum FROM ir_attachment WHERE store_fname IS NOT NULL")
        attachments = cursor.fetchall()
    src = source.filestore / source.database
    dst = target.filestore / stage
    checked(src.is_dir() and dst.exists() == resume, "Attachment directories do not match the migration plan")
    if not resume:
        dst.mkdir()
        (dst / "checklist").mkdir()
    for name, checksum in attachments:
        checked(bool(re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{40}", name)) and checksum == name.split("/")[-1],
                "Unexpected Odoo attachment path or checksum")
        origin = src / name
        checked(origin.resolve().is_relative_to(src.resolve()), "Attachment leaves source filestore")
        with origin.open("rb") as stream:
            checked(file_digest(stream, "sha1").hexdigest() == checksum, "Source attachment checksum differs")
        destination = dst / name
        if not resume:
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(origin, destination)
        with destination.open("rb") as stream:
            checked(file_digest(stream, "sha1").hexdigest() == checksum, "Copied attachment checksum differs")
    return len(attachments)


def swap_names(target, first, first_destination, second, second_destination):
    def allow(cursor, database, enabled):
        setting = sql.SQL("true" if enabled else "false")
        cursor.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS {}").format(sql.Identifier(database), setting))

    pairs = ((first, first_destination), (second, second_destination))
    fenced = []
    with closing(target.connect("postgres")) as maintenance:
        maintenance.autocommit = True
        with maintenance.cursor() as admin:
            try:
                for name, _ in pairs:
                    allow(admin, name, False)
                    fenced.append(name)
                end = monotonic() + 15
                while True:
                    admin.execute("SELECT pid, state, backend_type FROM pg_stat_activity WHERE datname IN (%s, %s)",
                                  (first, second))
                    connections = admin.fetchall()
                    if not connections:
                        break
                    for pid, state, kind in connections:
                        if kind == "client backend" and state == "idle":
                            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
                    checked(monotonic() < end, "Local databases still have active transactions")
                    sleep(0.25)
                with target.connect("postgres") as connection, connection.cursor() as cursor:
                    for name, destination in pairs:
                        cursor.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}")
                                       .format(sql.Identifier(name), sql.Identifier(destination)))
                    for _, destination in pairs:
                        allow(cursor, destination, True)
            except BaseException:
                for name in fenced:
                    allow(admin, name, True)
                raise


def main(resume):
    source = Installation(SOURCE_ROOT, SOURCE_DB, (8070, 15433))
    target = Installation(OLD_ROOT, ORIGINAL_DB, (8069, 15432))
    release = Path("server/odoo/release.py")
    checked((source.runtime / release).read_bytes() == (target.runtime / release).read_bytes(),
            "The two Odoo installations have different releases")
    if resume:
        checked(re.fullmatch(r"\d{8}_\d{6}", resume), "Invalid staging timestamp")
    stamp = resume or datetime.now().strftime("%Y%m%d_%H%M%S")
    stage = f"jingneng_stage_{stamp}"
    previous = f"original_before_jingneng_{stamp}"
    original_filestore = target.filestore / ORIGINAL_DB
    previous_filestore = target.filestore / previous
    stage_filestore = target.filestore / stage
    checked(original_filestore.is_dir() and not previous_filestore.exists() and stage_filestore.exists() == bool(resume),
            "The original or staging filestore paths are unexpected")
    old_backup = BACKUPS / f"原安装版切换前_{stamp}.dump"
    source_backup = BACKUPS / f"京能演示迁移源_{stamp}.dump"
    if resume:
        target.check_dump(old_backup)
        source.check_dump(source_backup)
    else:
        target.dump(old_backup)
        source.dump(source_backup)
        target.pg("createdb", "--no-password", "--template=template0", "--encoding=UTF8", stage)
        target.pg("pg_restore", "--no-password", "--exit-on-error", "--no-owner", "--no-privileges", "-d", stage, source_backup)
    print("Both database backups verified", flush=True)
    attachment_count = materialize_filestore(source, target, stage, bool(resume))
    staged = target.shell(stage, setup=True)
    print(f"Replacement staged: {len(staged['rows'])} quotations, {attachment_count} verified attachment files", flush=True)

    database_swapped = False
    original_files_moved = False
    replacement_files_moved = False
    try:
        target.stop_web()
        original_filestore.rename(previous_filestore)
        original_files_moved = True
        stage_filestore.rename(original_filestore)
        replacement_files_moved = True
        swap_names(target, ORIGINAL_DB, previous, stage, ORIGINAL_DB)
        database_swapped = True
        target.restart_web()
        current = target.shell(ORIGINAL_DB)
        checked(staged == current, "Quotation evidence differs after the cutover")
        verify_web = run_path(str(DEMO / "初始化源码" / "verify_demo.py"))["verify_web"]
        web = verify_web(SimpleNamespace(**target.modules), target.settings, current["rows"])
    except BaseException:
        if database_swapped:
            target.stop_web()
            swap_names(target, ORIGINAL_DB, stage, previous, ORIGINAL_DB)
        if replacement_files_moved:
            original_filestore.rename(stage_filestore)
        if original_files_moved:
            previous_filestore.rename(original_filestore)
        target.restart_web()
        raise

    result = {"database": ORIGINAL_DB, "quotation_count": len(current["rows"]),
              "company_count": current["companies"], "line_count": current["lines"],
              "confirmed_order": current["confirmed_order"], "attachment_file_count": attachment_count,
              "web": web, "original_database_backup": str(old_backup), "source_database_backup": str(source_backup),
              "previous_database": previous, "previous_filestore": str(previous_filestore)}
    report = DEMO / "原安装版切换验收.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", help="Reuse a fully restored staging database and filestore by timestamp")
    main(parser.parse_args().resume)
