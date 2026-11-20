from pathlib import Path
from runpy import run_path
from datetime import datetime
import zipfile,json
stage=Path(__file__).parent
root=Path('C:/Users/Administrator/AppData/Local/OdooNativePreview')
source=root/'runtime/server/odoo/addons/sale'
target=run_path('G:/京能/Odoo/京能板件演示/私有导入/切换原安装版演示库.py')['Installation'](root,'odoo_customer_demo',(8069,15432))
files=['models/sale_order.py','views/sale_order_views.xml','static/src/js/dashboard/dashboard.js','static/src/js/dashboard/dashboard.xml','static/src/js/dashboard/dashboard.scss']
old={name:(source/name).read_bytes() for name in files}
backup=Path('G:/京能/Odoo/核对材料')/('订单报价结果接入前-'+datetime.now().strftime('%Y%m%d-%H%M%S'));backup.mkdir()
target.dump(backup/'原演示库.dump')
with zipfile.ZipFile(backup/'原有相关代码.zip','x',compression=zipfile.ZIP_DEFLATED) as archive:
    for name,data in old.items():archive.writestr(name,data)
code='''
from pathlib import Path
from io import BytesIO
from lxml import etree
from odoo.tools.convert import convert_xml_import
env.cr.execute('SELECT id,state,amount_total,quotation_result,quotation_first_sent_at FROM sale_order ORDER BY id')
before=env.cr.fetchall()
source=Path('C:/Users/Administrator/AppData/Local/OdooNativePreview/runtime/server/odoo/addons/sale/views/sale_order_views.xml')
tree=etree.parse(str(source));nodes={n.get('id'):n for n in tree.getroot() if n.tag=='record'}
minimal=etree.Element('odoo')
for name in ['sale_order_customer_feedback_form','view_order_form','sale_order_tree','view_sales_order_filter']:minimal.append(nodes[name])
data=BytesIO(etree.tostring(minimal));data.name='sale/views/sale_order_views.xml'
convert_xml_import(env,'sale',data,{},mode='update',noupdate=False)
env.cr.execute('SELECT id,state,amount_total,quotation_result,quotation_first_sent_at FROM sale_order ORDER BY id')
assert env.cr.fetchall()==before,'Existing order results changed'
env.cr.commit()
print('ORDER_RESULT_VIEWS_READY',len(before),flush=True)
'''
target.stop_web()
try:
    for name in files:assert (source/name).read_bytes()==old[name],'Concurrent edit: '+name
    for name in files:(source/name).write_bytes((stage/name).read_bytes())
    output=target.command(target.python,[target.runtime/'server/odoo-bin','shell','-c',root/'odoo.conf','-d','odoo_customer_demo','--no-http','--log-level=warn'],code.encode('utf-8'),timeout=120)
    lines=[x for x in output.splitlines() if x.startswith('ORDER_')]
    if not lines:raise RuntimeError(output[-2000:])
    print('\n'.join(lines),flush=True)
    target.restart_web()
except Exception:
    for name,data in old.items():(source/name).write_bytes(data)
    target.restart_web()
    raise
(stage/'installed.json').write_text(json.dumps({name:(source/name).read_text(encoding='utf-8') for name in files if name.startswith('static/')},ensure_ascii=False),encoding='utf-8')
print('Original order result UI ready',flush=True)
