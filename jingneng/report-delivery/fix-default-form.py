from pathlib import Path
from runpy import run_path
from datetime import datetime
from lxml import etree

stage=Path(__file__).parent
root=Path('C:/Users/Administrator/AppData/Local/OdooNativePreview')
source=root/'runtime/server/odoo/addons/sale/views/sale_order_views.xml'
target=run_path('G:/京能/Odoo/京能板件演示/私有导入/切换原安装版演示库.py')['Installation'](root,'odoo_customer_demo',(8069,15432))
before=source.read_bytes()
text=before.decode('utf-8')
start=text.index('    <record id="sale_order_customer_feedback_form"')
end=text.index('    </record>',start)+len('    </record>')
record=text[start:end]
assert '<field name="priority">' not in record
record=record.replace('<field name="arch" type="xml">','<field name="priority">10000</field>\n        <field name="arch" type="xml">',1)
record=record.replace('<form string="客户反馈">','<form string="客户反馈" create="false" delete="false">',1)
updated=text[:start]+record+text[end:]
etree.fromstring(updated.encode('utf-8'))
backup=Path('G:/京能/Odoo/核对材料')/('修复新建报价默认表单前-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
backup.mkdir()
(backup/'sale_order_views.xml').write_bytes(before)
target.dump(backup/'原演示库.dump')
code='''
from pathlib import Path
from io import BytesIO
from lxml import etree
from odoo.tools.convert import convert_xml_import
env.cr.execute('SELECT id,state,amount_total,partner_id,quotation_result,quotation_first_sent_at FROM sale_order ORDER BY id')
before=env.cr.fetchall()
source=Path('C:/Users/Administrator/AppData/Local/OdooNativePreview/runtime/server/odoo/addons/sale/views/sale_order_views.xml')
tree=etree.parse(str(source));minimal=etree.Element('odoo')
minimal.append(next(n for n in tree.getroot() if n.get('id')=='sale_order_customer_feedback_form'))
data=BytesIO(etree.tostring(minimal));data.name='sale/views/sale_order_views.xml'
convert_xml_import(env,'sale',data,{},mode='update',noupdate=False)
main=env.ref('sale.view_order_form')
feedback=env.ref('sale.sale_order_customer_feedback_form')
assert env['ir.ui.view'].default_view('sale.order','form')==main.id,'Normal form was not restored'
assert feedback.priority>main.priority
env.cr.execute('SELECT id,state,amount_total,partner_id,quotation_result,quotation_first_sent_at FROM sale_order ORDER BY id')
assert env.cr.fetchall()==before,'Order data changed'
env.cr.commit()
print('DEFAULT_FORM_FIXED',main.id,feedback.id,len(before),flush=True)
'''
target.stop_web()
try:
    assert source.read_bytes()==before,'Concurrent source edit'
    source.write_bytes(updated.encode('utf-8'))
    output=target.command(target.python,[target.runtime/'server/odoo-bin','shell','-c',root/'odoo.conf','-d','odoo_customer_demo','--no-http','--log-level=warn'],code.encode('utf-8'),timeout=120)
    lines=[line for line in output.splitlines() if line.startswith('DEFAULT_FORM_FIXED')]
    if not lines:raise RuntimeError(output[-1800:])
    print('\n'.join(lines),flush=True)
    target.restart_web()
except Exception:
    source.write_bytes(before)
    target.restart_web()
    raise
# Keep the existing local staging template aligned to prevent a later re-import regression.
builder=stage/'build.py'
builder_text=builder.read_text(encoding='utf-8')
needle='<field name="name">sale.order.customer.feedback</field><field name="model">sale.order</field>'
if needle in builder_text:
    builder_text=builder_text.replace(needle,needle+'\n        <field name="priority">10000</field>',1)
    builder_text=builder_text.replace('<form string="客户反馈">','<form string="客户反馈" create="false" delete="false">',1)
    builder.write_text(builder_text,encoding='utf-8')
(stage/'views/sale_order_views.xml').write_bytes(source.read_bytes())
print('Original quotation form ready on 8069',flush=True)
