"""Initialize a fresh Odoo Community demonstration database using official modules."""

import os
from pathlib import Path
import re
import subprocess
import sys

import psycopg2

DATABASE = "odoo_customer_demo"
ADMIN_LOGIN = "demo-admin@odoo-preview.example"
MODULES = ("sale_management", "purchase", "stock", "mrp", "contacts")
COMPANY_NAMES = {
    "My Company (San Francisco)": "演示公司（旧金山）",
    "My Company (Chicago)": "演示公司（芝加哥）",
    "CN ASSBE Company": "小企业会计准则演示公司",
    "CN ASBE Company": "企业会计准则演示公司",
}
PARTNER_NAMES = {
    "OdooBot": "Odoo 助手", "Mitchell Admin": "演示管理员",
    "Public user": "访客", "Portal User Template": "门户用户模板",
    "Marc Demo": "马克（演示用户）", "Monica Light": "莫妮卡·莱特",
    "Joel Willis": "乔尔·威利斯", "Wood Corner": "木作坊",
    "Acme Corporation": "阿克米公司", "Gemini Furniture": "双子家具",
    "Ready Mat": "瑞迪地垫", "OpenWood": "开放木业",
    "LightsUp": "点亮照明", "The Jackson Group": "杰克逊集团",
    "Azure Interior": "蔚蓝室内设计", "Lumber Inc": "林木公司",
    "Willie Burke": "威利·伯克", "Ron Gibson": "罗恩·吉布森",
    "Douglas Fletcher": "道格拉斯·弗莱彻", "Floyd Steward": "弗洛伊德·斯图尔特",
    "Edwin Hansen": "埃德温·汉森", "Billy Fox": "比利·福克斯",
    "Jesse Brown": "杰西·布朗", "Soham Palmer": "索哈姆·帕尔默",
    "Kim Snyder": "金·斯奈德", "Edith Sanchez": "伊迪丝·桑切斯",
    "Brandon Freeman": "布兰登·弗里曼", "Nicole Ford": "妮可·福特",
    "Toni Rhodes": "托妮·罗兹", "Gordon Owens": "戈登·欧文斯",
    "Theodore Gardner": "西奥多·加德纳", "Oscar Morgan": "奥斯卡·摩根",
    "Tom Ruiz": "汤姆·鲁伊斯", "Colleen Diaz": "科琳·迪亚斯",
    "Lorraine Douglas": "洛琳·道格拉斯", "Addison Olson": "艾迪生·奥尔森",
    "Sandra Neal": "桑德拉·尼尔", "Julie Richards": "朱莉·理查兹",
    "Travis Mendoza": "特拉维斯·门多萨", "Chester Reed": "切斯特·里德",
    "Dwayne Newman": "德韦恩·纽曼", "Jeff Lawson": "杰夫·劳森",
}
CITIES = {
    "Bayonne": "巴约讷", "Brisbane": "布里斯班", "Chicago": "芝加哥",
    "Eugene": "尤金", "Fremont": "弗里蒙特", "Namur": "那慕尔",
    "Pleasant Hill": "普莱森特希尔", "Scranton": "斯克兰顿",
    "Serravalle": "塞拉瓦莱", "Stockton": "斯托克顿", "Tracy": "特雷西",
    "Turlock": "特洛克", "Uuearu": "乌埃阿鲁", "Wiltz": "维尔茨",
}
STREETS = {
    "118 Rue de l'Ange": "天使街 118 号", "1337 N San Joaquin St": "北圣华金街 1337 号",
    "1611 Peony Dr": "牡丹路 1611 号", "1839 Arbor Way": "阿伯路 1839 号",
    "215 Vine St": "藤蔓街 215 号", "3575  Buena Vista Avenue": "布埃纳维斯塔大道 3575 号",
    "4557 De Silva St": "德席尔瓦街 4557 号", "7500 W Linne Road": "西林恩路 7500 号",
    "77 Santa Barbara Rd": "圣巴巴拉路 77 号", "8000 Marina Blvd, Suite 300": "滨海大道 8000 号 300 室",
    "858 Lynn Street": "林恩街 858 号", "90 Streets Avenue": "斯特里茨大道 90 号",
    "Eignaa tee 12": "埃格纳路 12 号", "Via Industria 21": "工业路 21 号",
}
JOBS = {
    "Analyst": "分析师", "Business Executive": "商务主管", "Chief Executive Officer (CEO)": "首席执行官",
    "Chief Operations Officer (COO)": "首席运营官", "Creative Director": "创意总监",
    "Director": "总监", "Financial Manager": "财务经理", "Functional Consultant": "业务顾问",
    "Knowledge Manager": "知识管理经理", "Managing Partner": "管理合伙人", "Marketing Manager": "市场经理",
    "Order Clerk": "订单文员", "Production Supervisor": "生产主管", "Sales Manager": "销售经理",
    "Sales Representative": "销售代表", "Senior Associate": "高级专员", "Senior Consultant": "高级顾问",
    "Service Manager": "服务经理", "Software Developer": "软件开发人员", "Store Manager": "门店经理",
    "System Analyst": "系统分析师",
}
ACTIVITIES = {
    "Send Email to Alfred": "给阿尔弗雷德发邮件", "Eat cookies": "吃点饼干", "Follow-up on payment": "跟进付款",
    "Follow up on missed call": "回拨未接来电", "Include upsell": "建议增购", "Update address": "更新地址",
    "Send specifications": "发送规格说明", "Get approval": "取得审批", "Check optional products": "检查可选产品",
    "Check competitors": "了解竞争对手", "Answer questions": "解答问题", "Upsell": "建议增购",
    "Follow up on previous email": "跟进上一封邮件", "Check delivery requirements": "核对交货要求",
    "Confirm Delivery": "确认交货", "Confirm order via email": "邮件确认订单", "Get quote confirmation": "确认报价",
}
LOCATIONS = {
    "Vendors": "供应商", "Customers": "客户", "Inter-company transit": "公司间中转",
    "Inventory adjustment": "库存调整", "Production": "生产", "Scrap": "报废",
    "Shelf 2": "2 号货架", "Shelf 1": "1 号货架", "Order Processing": "订单处理区",
    "Dispatch Zone": "发运区", "Gate A": "A 门", "Gate B": "B 门", "Small Refrigerator": "小型冷柜",
}

MAIL_TRANSLATIONS = {
    "Hello,": "您好：", "Hello": "您好，", "Dear": "尊敬的",
    "Your quotation": "您的报价单", "Your order": "您的订单",
    "(with reference:": "（参考号：", "CUSTOMER REF": "客户参考号",
    "amounting in": "金额为", "is ready for review.": "，现已可查阅。",
    "Here are some additional documents that may interest you:": "以下是供您参考的补充文件：",
    "Here is an additional document that may interest you:": "以下是供您参考的补充文件：",
    "Do not hesitate to contact us if you have any questions.": "如有任何疑问，请联系我们。",
    "Mitchell Admin": "演示管理员", "Johnny Tester": "约翰尼（测试客户）",
    "has been confirmed and will be ready on": "已确认，预计备妥日期为",
    "has been confirmed and will be ready soon.": "已确认，预计近期备妥。",
    "has been confirmed.": "已确认。", "Thank you for your trust.": "感谢您的信任。",
    "is pending. It will be confirmed when the payment is received.": "正在等待付款，收到款项后将予以确认。",
    "Your payment reference is": "您的付款参考号为",
    "Taking care of Trees Course": "树木养护课程", "Delivery": "交货", "Delivery with": "配送方式",
    "Untaxed Amount": "未税金额", "Total": "总计", "Billing": "账单地址",
    "1201 S Figueroa St": "南菲格罗亚街 1201 号", "Los Angeles": "洛杉矶",
    "California": "加利福尼亚州", "United States": "美国",
    "Brandon Freeman": "布兰登·弗里曼", "Azure Interior": "蔚蓝室内设计",
    "Here is in attachment a request for quotation": "附件是采购询价单",
    "with reference:": "，参考号：", "REF_XXX": "演示参考号", "from": "，发自", "YourCompany": "演示公司",
    "If you have any questions, please do not hesitate to contact us.": "如有疑问，欢迎联系我们。",
    "Best regards,": "此致，", "Here is in attachment a purchase order": "附件是采购订单",
    "The receipt is expected for": "预计到货日期为",
    "Could you please acknowledge the receipt of this order?": "请确认您已收到此订单。",
    "Acknowledge": "确认收到",
    "Here is a reminder that the delivery of the purchase order": "提醒您，采购订单",
    "is expected for": "的预计交付日期为", "undefined": "待定",
    "Could you please confirm it will be delivered on time?": "请确认能否按期交货。",
    "Your order has been collected.": "您的订单已完成取货。",
    "We are glad to inform you that your order has been shipped.": "您的订单已发货。",
    "Your tracking reference is": "您的物流单号为",
    "Please find your delivery order attached for more details.": "交货详情见附件。",
    "Thank you,": "谢谢，", "Marc Demo": "马克（演示用户）",
    "We would like to issue a product recall regarding the following items from your delivery": "我们将召回以下交货单中的商品",
    "Product(s):": "产品：", "[EXAMPLE] Drawer": "[示例] 抽屉", "Lot / Serial Number(s):": "批次号或序列号：",
    "We apologize for the inconvenience caused.": "对于给您带来的不便，我们深表歉意。",
    "Best wishes,": "顺祝安好，",
    "Your Pro forma invoice for quotation": "报价单对应的形式发票",
    "Your Pro forma invoice for order": "订单对应的形式发票", "is available.": "已生成，可供查阅。",
    "A payment with reference": "付款参考号", "amounting": "金额为", "for your order": "，对应您的订单",
    "is pending.": "，正在等待确认。",
    "Your order will be confirmed once the payment is confirmed.": "付款确认后，我们会确认您的订单。",
    "Once confirmed,": "付款确认后，仍有", "will remain to be paid.": "待支付。",
    "remains to be paid.": "仍待支付。", "Thank you for your trust!": "感谢您的信任！",
    "(": "（", ")": "）", ",": "，", ".": "。", ":": "：",
}
MAIL_SUBJECTS = {
    "sale.email_template_edi_sale": "{{ object.company_id.name }} {{ object.state in ('draft', 'sent') and '报价单' or '订单' }} {{ object.name }} {{ object.client_order_ref and ('（' + object.client_order_ref + '）') or '' }}",
    "sale.mail_template_sale_confirmation": "{{ object.company_id.name }} 订单确认 {{ object.name }}",
    "sale.email_template_proforma": "{{ object.company_id.name }} 形式发票（{{ object.name }}）",
    "sale.mail_template_sale_payment_executed": "{{ object.company_id.name }} {{ (object.get_portal_last_transaction().state == 'pending') and '待付款订单' or '订单' }} {{ object.name }}",
    "purchase.email_template_edi_purchase": "{{ object.company_id.name }} 采购询价单 {{ object.name }}",
    "purchase.email_template_edi_purchase_done": "{{ object.company_id.name }} 采购订单 {{ object.name }}",
    "purchase.email_template_edi_purchase_reminder": "{{ object.company_id.name }} 采购订单交付提醒 {{ object.name }}",
    "stock.mail_template_data_delivery_confirmation": "{{ object.company_id.name }} 交货单 {{ object.name }}",
    "stock.mail_template_data_stock_picking_recall": "交货单 {{ object.name }} 的产品召回通知",
}


def translate_body(html):
    parts = re.split(r"(<!--[\s\S]*?-->|<[^>]+>)", html)
    for index in range(0, len(parts), 2):
        value = parts[index]
        key = re.sub(r"\s+", " ", value).strip()
        if key in MAIL_TRANSLATIONS:
            before = value[:len(value) - len(value.lstrip())]
            after = value[len(value.rstrip()):]
            parts[index] = before + MAIL_TRANSLATIONS[key] + after
    return "".join(parts)


def localize(runtime):
    from odoo import Command, SUPERUSER_ID, api

    runtime = api.Environment(
        runtime.cr, SUPERUSER_ID, {"lang": "zh_CN", "tracking_disable": True, "mail_notrack": True}
    )
    marker = runtime["ir.config_parameter"]
    if marker.get_bool("odoo_customer_preview.initialized"):
        return

    company = runtime.ref("base.main_company")
    customer = runtime.ref("base.res_partner_1")
    product = runtime["product.product"].search([("default_code", "=", "FURN_7800")], limit=1)
    if not product or customer.name not in ("Wood Corner", "木作坊"):
        raise RuntimeError("初始化缺少官方演示客户或产品。")

    if company.country_id.code != "CN":
        company.country_id = runtime.ref("base.cn")
    for rec in runtime["res.company"].search([]):
        if rec.name in COMPANY_NAMES:
            rec.name = COMPANY_NAMES[rec.name]
    for rec in runtime["res.partner"].with_context(active_test=False).search([]):
        fields = {
            field: mapping[rec[field]]
            for field, mapping in (
                ("name", PARTNER_NAMES), ("city", CITIES), ("street", STREETS), ("function", JOBS)
            )
            if rec[field] in mapping
        }
        if fields:
            rec.write(fields)

    admin = runtime.ref("base.user_admin")
    admin.write({
        "name": "演示管理员", "login": ADMIN_LOGIN, "lang": "zh_CN",
        "password": os.environ["ODOO_CUSTOMER_ADMIN_PASSWORD"], "signature": "<div>演示管理员</div>",
    })
    # Give the additional built-in demo users distinct, unknown test passwords.
    import secrets
    for user in runtime["res.users"].with_context(active_test=False).search([
        ("id", "not in", [admin.id, SUPERUSER_ID]), ("active", "=", True)
    ]):
        user.password = secrets.token_urlsafe(24)

    for record in runtime["mail.activity"].search([("summary", "in", list(ACTIVITIES))]):
        record.summary = ACTIVITIES[record.summary]
    for record in runtime["stock.location"].with_context(active_test=False).search([
        ("name", "in", list(LOCATIONS))
    ]):
        record.name = LOCATIONS[record.name]
    for wh in runtime["stock.warehouse"].search([]):
        if wh.company_id.id == company.id:
            wh.name = "主仓库"
        elif "芝加哥" in wh.company_id.name:
            wh.name = "芝加哥仓库"

    for key, subject in MAIL_SUBJECTS.items():
        template = runtime.ref(key, raise_if_not_found=False)
        if template:
            template.write({"subject": subject, "body_html": translate_body(template.body_html or "")})

    quotations = runtime["sale.order"].with_company(company)
    sample = {"partner_id": customer.id, "user_id": admin.id,
              "order_line": [Command.create({"product_id": product.id, "product_uom_qty": 1})]}
    order = quotations.create(dict(sample, client_order_ref="客户演示订单"))
    order.action_confirm()
    sample["order_line"] = [Command.create({"product_id": product.id, "product_uom_qty": 1})]
    quote = quotations.create(dict(sample, client_order_ref="客户演示报价"))
    if not order.picking_ids:
        raise RuntimeError("演示销售订单未生成交货单。")
    marker.set_str("web.base.url", os.environ["ODOO_CUSTOMER_ADDRESS"])
    marker.set_bool("odoo_customer_preview.initialized", True)
    marker.set_str("odoo_customer_preview.demo_order", order.name)
    marker.set_str("odoo_customer_preview.demo_quote", quote.name)
    runtime.cr.commit()
    print("中文演示已初始化，订单 " + order.name + "，报价 " + quote.name, flush=True)


def run_odoo(arguments, script=None):
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "server" / "odoo-bin"), *arguments], input=script, text=True, check=False)
    if result.returncode:
        raise SystemExit("Odoo 初始化步骤失败，退出码 " + str(result.returncode))


def bootstrap():
    with psycopg2.connect(dbname="postgres", connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname=%s", [DATABASE])
            exists = cursor.fetchone() is not None
    if not exists:
        run_odoo([
            "db", "init", "--with-demo", "--language", "zh_CN", "--country", "CN",
            "--username", ADMIN_LOGIN, "--password", os.environ["ODOO_CUSTOMER_ADMIN_PASSWORD"], DATABASE,
        ])
    with psycopg2.connect(dbname=DATABASE, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_encoding")
            if cursor.fetchone()[0] != "UTF8":
                raise RuntimeError("中文演示数据库需要 UTF-8 编码。")
            cursor.execute("SELECT to_regclass('ir_module_module')")
            has_modules = cursor.fetchone()[0] is not None
            installed = set()
            if has_modules:
                cursor.execute("SELECT name FROM ir_module_module WHERE state='installed'")
                installed = {row[0] for row in cursor.fetchall()}
    missing = [name for name in ("base", *MODULES) if name not in installed]
    if missing:
        run_odoo([
            "server", "-d", DATABASE, "-i", ",".join(missing), "--with-demo",
            "--no-http", "--stop-after-init", "--log-level=warn",
        ])
    run_odoo(["shell", "-d", DATABASE, "--no-http", "--log-level=warn"],
             Path(__file__).read_text(encoding="utf-8"))


if "env" in globals():
    localize(globals()["env"])
elif __name__ == "__main__":
    bootstrap()

