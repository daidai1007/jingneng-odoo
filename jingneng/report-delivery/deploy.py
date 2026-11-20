from pathlib import Path
from runpy import run_path
from datetime import datetime
import json
import zipfile

stage=Path(__file__).parent
root=Path('C:/Users/Administrator/AppData/Local/OdooNativePreview')
source=root/'runtime/server/odoo/addons/sale'
files=[str(p.relative_to(stage/'before')).replace('\\','/') for p in (stage/'before').rglob('*') if p.is_file()]
target=run_path('G:/京能/Odoo/京能板件演示/私有导入/切换原安装版演示库.py')['Installation'](root,'odoo_customer_demo',(8069,15432))
old={name:(stage/'before'/name).read_bytes() for name in files}
for name in files: assert (source/name).read_bytes()==old[name], 'Concurrent edit: '+name
backup=Path('G:/京能/Odoo/核对材料')/('报表接入前-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
backup.mkdir()
target.dump(backup/'原演示库.dump')
with zipfile.ZipFile(backup/'原有相关代码.zip','x',compression=zipfile.ZIP_DEFLATED) as archive:
    for name,data in old.items():archive.writestr(name,data)
code='''
import json
from pathlib import Path
from io import BytesIO
from lxml import etree
from odoo.tools.convert import convert_xml_import

root=Path('C:/Users/Administrator/AppData/Local/OdooNativePreview/runtime/server/odoo/addons/sale')
env.cr.execute('SELECT id,state,amount_total,partner_id,date_order FROM sale_order ORDER BY id')
fingerprint=env.cr.fetchall()
env.registry.init_models(env.cr,['sale.order'],{'module':'sale'},install=False)
# Backfill only an existing recorded sent date; never infer outcomes from workflow.
env.cr.execute("UPDATE sale_order SET quotation_first_sent_at=quotation_sent_at WHERE quotation_first_sent_at IS NULL AND quotation_sent_at IS NOT NULL")
env['sale.order'].invalidate_model(['quotation_first_sent_at'])
for filename,ids in [
    ('views/sale_order_views.xml',['sale_order_customer_feedback_form','view_order_form']),
    ('report/sale_report_views.xml',['action_jingneng_report_sales','action_jingneng_report_people','action_jingneng_report_products','action_jingneng_report_customers']),
]:
    tree=etree.parse(str(root/filename));minimal=etree.Element('odoo')
    nodes={n.get('id'):n for n in tree.getroot() if n.tag=='record'}
    for name in ids:
        if name not in nodes:raise RuntimeError('Missing XML record: '+name)
        minimal.append(nodes[name])
    data=BytesIO(etree.tostring(minimal));data.name='sale/'+filename
    convert_xml_import(env,'sale',data,{},mode='update',noupdate=False)
for name,dimension in [('menu_reporting_sales','sales'),('menu_reporting_salespeople','people'),('menu_reporting_product','products'),('menu_reporting_customer','customers')]:
    env.ref('sale.'+name).action=env.ref('sale.action_jingneng_report_'+dimension)
env.cr.execute('SELECT id,state,amount_total,partner_id,date_order FROM sale_order ORDER BY id')
after=env.cr.fetchall()
assert fingerprint==after, 'Order invariant changed'
env.cr.commit()
print('REPORT_SCHEMA_AND_VIEWS_READY',len(after),flush=True)
'''
target.stop_web()
try:
    for name in files:assert (source/name).read_bytes()==old[name], 'Concurrent edit: '+name
    for name in files:(source/name).write_bytes((stage/name).read_bytes())
    output=target.command(target.python,[target.runtime/'server/odoo-bin','shell','-c',root/'odoo.conf','-d','odoo_customer_demo','--no-http','--log-level=warn'],code.encode('utf-8'),timeout=150)
    relevant=[x for x in output.splitlines() if x.startswith('REPORT_')]
    if not relevant:raise RuntimeError('Schema deployment did not complete: '+output[-2000:])
    print('\n'.join(relevant),flush=True)
    target.restart_web()
except Exception:
    for name,data in old.items():(source/name).write_bytes(data)
    target.restart_web()
    raise
(stage/'deployment.json').write_text(json.dumps({'backup':str(backup),'files':files,'url':'http://127.0.0.1:8069'},ensure_ascii=False,indent=2),encoding='utf-8')
print('Original 8069 report deployment ready',flush=True)
