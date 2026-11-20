import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
const work=process.argv[2];
const require=createRequire("C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/browser-check.cjs");
const {chromium}=require("playwright");
const config=JSON.parse((await fs.readFile("C:/Users/Administrator/AppData/Local/OdooNativePreview/设置.json","utf8")).replace(/^\uFEFF/,""));
const browser=await chromium.launch({executablePath:"C:/Program Files/Google/Chrome/Application/chrome.exe",headless:true});
const context=await browser.newContext({viewport:{width:1720,height:1050},deviceScaleFactor:1});
const origin="http://127.0.0.1:8069";
const login=await context.request.post(origin+"/web/session/authenticate",{data:{jsonrpc:"2.0",method:"call",params:{db:"odoo_customer_demo",login:"demo-admin@odoo-preview.example",password:config.admin_password}}});
if(!(await login.json()).result?.uid) throw new Error("Local login failed");
async function call(model,method,args=[],kwargs={}) {
 const response=await context.request.post(origin+"/web/dataset/call_kw/"+model+"/"+method,{data:{jsonrpc:"2.0",method:"call",params:{model,method,args,kwargs}}});
 const out=await response.json();
 if(out.error) throw new Error(out.error.data?.message||out.error.message);
 return out.result;
}
const assert=(value,message)=>{if(!value)throw Error(message);};
const token='REPORT-VERIFY-'+Date.now();
const ids=[];let products=[];
const before=await call('sale.order','search_read',[[]],{fields:['id','state','partner_id','amount_total','date_order']});
const normalize=rows=>JSON.stringify(rows.toSorted((a,b)=>a.id-b.id));
const page=await context.newPage();page.setDefaultTimeout(30000);const errors=[];page.on('pageerror',e=>errors.push(e.message));
const kpi=async(key,value)=>page.waitForFunction(({key,value})=>document.querySelector('.jn-report-card[data-result="'+key+'"] strong')?.textContent===value,{key,value});
const check=[];const pass=name=>{check.push(name);console.log('PASS',name);};
try {
 const partner=(await call('sale.order','search_read',[[]],{fields:['partner_id'],limit:1}))[0].partner_id;
 const category=(await call('product.category','search_read',[[['name','=','机箱机柜']]],{fields:['id'],limit:1}))[0];
 const unit=(await call('uom.uom','search_read',[[]],{fields:['id','name'],limit:1}))[0];
 const p=await call('product.product','create',[{name:token+'机柜',type:'service',categ_id:category.id,uom_id:unit.id,taxes_id:[[5]],supplier_taxes_id:[[5]]}]);products.push(Array.isArray(p)?p[0]:p);
 const create=async(label,extra={},price=100)=>{
  const val={name:token+'-'+label,partner_id:partner[0],quotation_workflow_step:'quotation',state:'sent',
   quotation_first_sent_at:'2026-11-03 01:00:00',quotation_sent_at:'2026-11-03 01:00:00',
   order_line:[[0,0,{product_id:products[0],product_uom_qty:1,price_unit:price,tax_ids:[[5]]}]],...extra};
  const result=await call('sale.order','create',[val]);const id=Array.isArray(result)?result[0]:result;ids.push(id);return id;
 };
 const root=await create('V1');
 const revision=await create('V2',{quotation_revision_of_id:root,quotation_first_sent_at:'2026-11-07 01:00:00',quotation_result:'won',quotation_feedback_date:'2026-11-18'},150);
 await create('V3-DRAFT',{quotation_revision_of_id:revision,state:'draft',quotation_workflow_step:false,quotation_first_sent_at:false},170);
 const lost=await create('LOST',{quotation_result:'lost',quotation_feedback_date:'2026-11-18',quotation_lost_reason:'price'},200);
 const pending=await create('PENDING',{},300);
 const contract=await create('CONTRACT',{state:'draft',quotation_workflow_step:'contract'},400);
 const cancelled=await create('CANCELLED',{state:'cancel'},500);
 const noSent=await create('NO-SENT',{state:'draft',quotation_workflow_step:'contract',quotation_first_sent_at:false},600);
 const sales=await create('SALE',{state:'sale',date_order:'2026-11-15 01:00:00',quotation_confirmed_at:'2026-11-15 01:00:00',quotation_first_sent_at:false},700);
 const getRows=async filters=>(await call('sale.report','get_jingneng_report_data',[{analysis:'bids',period:'1',...filters}])).rows.filter(r=>r.name.startsWith(token));
 let rows=await getRows();assert(rows.length===4,'Expected 4 distinct valid sent business quotes: '+JSON.stringify(rows.map(r=>r.name)));
 assert(rows.some(r=>r.id===revision&&r.version===2&&r.date==='2026-11-03'),'Latest effective revision must retain initial cohort date');
 assert(rows.filter(r=>r.result==='won').length===1&&rows.filter(r=>r.result==='lost').length===1&&rows.filter(r=>r.result==='pending').length===2,'Explicit outcomes only');
 pass('最新有效版本去重；草稿与取消排除；合同未自动中标');
 const rateRows=rows.filter(r=>r.result!=='pending');assert(rateRows.length===2&&rateRows.filter(r=>r.result==='won').length/rateRows.length===.5,'Pending denominator');pass('待反馈不计入中标率分母');
 await call('sale.order','write',[[pending],{state:'draft'}]);await call('sale.order','write',[[pending],{state:'sent'}]);
 const saved=(await call('sale.order','read',[[pending],['quotation_first_sent_at']]))[0];assert(saved.quotation_first_sent_at==='2026-11-03 01:00:00','First sent changed after resend');pass('重新发送保留首次发送日期');
 const custom=await getRows({period:'custom',start:'2026-11-04',end:'2026-11-10'});assert(!custom.some(r=>r.id===revision),'Revision cohort moved on reprice');pass('日期筛选使用首次发送日期');
 const sold=await getRows({analysis:'sales',scope:'sale'});assert(sold.length===1&&sold[0].id===sales&&sold[0].amount===700,'Confirmed sales scope');pass('销售订单金额独立统计');
 let invalid=false;try{await call('sale.order','write',[[noSent],{quotation_result:'won'}]);}catch{invalid=true;}assert(invalid,'Missing first sent accepted');pass('登记反馈要求真实发送日期');
 let cycle=false;try{await call('sale.order','write',[[root],{quotation_revision_of_id:revision}]);}catch{cycle=true;}assert(cycle,'Revision cycle accepted');pass('报价版本循环校验');
 for(const dim of ['sales','people','products','customers']) {
  await page.goto(origin+'/odoo/jingneng-reports-'+dim);await page.locator('.jn-report-cards').waitFor();
  await page.getByRole('searchbox',{name:'搜索报表'}).fill(token);
  await kpi('all','4');
  const cards=page.locator('.jn-report-card');assert(await cards.nth(0).locator('strong').innerText()==='4','UI effective quote count '+dim);
  assert(await cards.nth(4).locator('strong').innerText()==='50.0%','UI won rate '+dim);
  if(dim==='sales') {
   await page.getByRole('searchbox',{name:'搜索报表'}).fill(token+'-CONTRACT');
   await kpi('rate','—');
   assert(await cards.nth(4).locator('strong').innerText()==='—','Unclosed cohort must have no won rate');
   await page.getByRole('searchbox',{name:'搜索报表'}).fill(token+'-NOT-EXISTS');
   await page.getByText('当前范围暂无已正式发送的有效报价',{exact:true}).waitFor();pass('零结果与无已反馈时不显示虚假中标率');
   await page.getByRole('searchbox',{name:'搜索报表'}).fill(token);
   await kpi('all','4');
  }
  await page.screenshot({path:path.join(work,'报表验收-'+dim+'-图表.png'),fullPage:true});
  await page.getByRole('button',{name:'透视表视图',exact:true}).click();
  await page.screenshot({path:path.join(work,'报表验收-'+dim+'-透视.png'),fullPage:true});
  if(dim==='customers'){
   await page.getByRole('combobox',{name:'客户反馈结果',exact:true}).selectOption('won');
   assert(await cards.nth(4).locator('strong').innerText()==='50.0%','Result filter changed KPI denominator');
   await page.getByRole('combobox',{name:'客户反馈结果',exact:true}).selectOption('all');
   await page.getByRole('button',{name:'查看全部明细',exact:true}).click();
   const pendingRow=page.locator('tbody tr').filter({has:page.getByRole('button',{name:token+'-PENDING',exact:true})});
   await pendingRow.getByRole('button',{name:'登记反馈'}).click();
   const dialog=page.getByRole('dialog');await dialog.waitFor();
   await page.screenshot({path:path.join(work,'报表验收-客户反馈登记.png'),fullPage:true});
   await dialog.getByRole('radio',{name:'中标',exact:true}).check();
   await dialog.getByRole('button',{name:'保存',exact:true}).click();await dialog.waitFor({state:'hidden'});
   await kpi('rate','66.7%');
   assert(await cards.nth(4).locator('strong').innerText()==='66.7%','Feedback save did not refresh report');pass('浏览器登记反馈后报表更新');
   const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('button',{name:'导出明细',exact:true}).click()]);
   const csv=await fs.readFile(await download.path(),'utf8');assert(csv.includes(token+'-V2')&&!csv.includes(token+'-V1')&&csv.split('\r\n').length===5,'Export differs from filtered report');pass('导出与去重明细一致');
   await page.getByRole('button',{name:token+'-PENDING',exact:true}).click();await page.locator('.o_form_view').waitFor();
   await page.goBack();await page.locator('.jn-report-cards').waitFor();
   assert(await page.getByRole('searchbox',{name:'搜索报表'}).inputValue()===token,'Filters lost on return');assert(await page.getByRole('button',{name:token+'-PENDING',exact:true}).isVisible(),'Detail selection lost');pass('打开真实订单并返回保留筛选');
   await page.getByRole('button',{name:'返回汇总',exact:true}).click();
   await page.getByRole('button',{name:'失标原因',exact:true}).click();
   await page.getByRole('button',{name:'价格原因',exact:true}).click();
   assert(await page.locator('tbody tr').count()>=1,'Reason drill missing');pass('失标原因下钻');
  }
  if(dim==='products') {
   await page.getByRole('button',{name:'销售订单业绩',exact:true}).click();await page.getByText('正在加载报表…',{exact:true}).waitFor({state:'hidden'});await page.locator('.jn-report-cards').waitFor();
   await page.getByRole('combobox',{name:'产品单位',exact:true}).selectOption(String(unit.id));
   await page.getByRole('combobox',{name:'计量',exact:true}).selectOption('qty');
   assert((await page.locator('.jn-report-table').innerText()).includes('合计'),'Product pivot missing');pass('产品数量按单位筛选');
  }
  pass('实际菜单 '+dim+' 图表/透视/汇总');
 }
 const quotationAction=(await call('ir.model.data','search_read',[[['module','=','sale'],['name','=','action_quotations']]],{fields:['res_id'],limit:1}))[0].res_id;
 await page.goto(origin+'/odoo/action-'+quotationAction+'/'+noSent);await page.locator('.o_form_view').waitFor();
 assert(await page.getByRole('button',{name:'登记客户反馈',exact:true}).count()===0,'Unsent quote shows feedback entry');
 let blocked=false;try{await call('sale.order','action_open_customer_feedback',[[noSent]]);}catch{blocked=true;}assert(blocked,'Unsent feedback action allowed');
 await call('sale.order','action_quotation_sent',[[noSent]]);await page.reload();await page.getByRole('button',{name:'登记客户反馈',exact:true}).waitFor();
 await page.getByRole('button',{name:'登记客户反馈',exact:true}).click();let feedbackDialog=page.getByRole('dialog');await feedbackDialog.getByRole('radio',{name:'失标',exact:true}).check();await feedbackDialog.getByRole('button',{name:'保存',exact:true}).click();await feedbackDialog.waitFor({state:'hidden'});
 await page.goto(origin+'/odoo/action-'+quotationAction);await page.locator('.o_list_view').waitFor();
 const feedbackRow=page.locator('.o_data_row').filter({hasText:token+'-NO-SENT'});await feedbackRow.getByText('失标',{exact:true}).waitFor();
 await page.screenshot({path:path.join(work,'报表验收-订单列表报价结果.png'),fullPage:true});pass('未发送无结果；发送后登记失标并在订单列表展示');
 await page.setViewportSize({width:800,height:1000});await page.screenshot({path:path.join(work,'报表验收-窄屏.png'),fullPage:true});
 await page.goto(origin+'/odoo/jingneng-reports-customers');await page.locator('.jn-report-cards').waitFor();
 assert(await page.locator('.jn-reports').evaluate(el=>el.getBoundingClientRect().width<=window.innerWidth),'Page wider than viewport');pass('窄屏布局可用');
 assert(!errors.length,'Browser runtime errors: '+JSON.stringify(errors));pass('无浏览器运行错误');
} catch(error) {console.log('ERRORS',JSON.stringify(errors));console.log('CURRENT_BODY',(await page.locator('body').innerText()).slice(-2200));throw error;} finally {
 if(ids.length){await call('sale.order','write',[ids,{state:'cancel'}]);await call('sale.order','write',[ids,{quotation_revision_of_id:false}]);await call('sale.order','unlink',[ids]);}
 if(products.length)await call('product.product','unlink',[products]);
 const after=await call('sale.order','search_read',[[]],{fields:['id','state','partner_id','amount_total','date_order']});assert(normalize(before)===normalize(after),'Original order data changed');
 await fs.writeFile(path.join(work,'报表实施验收.json'),JSON.stringify({checks:check,originalOrders:before.length,originalOrdersUnchanged:true,browserErrors:errors},null,2));
 await browser.close();
}
