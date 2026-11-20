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
const page=await context.newPage();page.setDefaultTimeout(30000);
const errors=[];page.on('pageerror',e=>errors.push(e.message));
let testOrder;const checks=[];
const fields=['id','name','state','partner_id','amount_total','quotation_first_sent_at','quotation_result'];
const before=await call('sale.order','search_read',[[]],{fields,order:'id'});
try {
 const xmlids=await call('ir.model.data','search_read',[[['module','=','sale'],['name','in',['view_order_form','sale_order_customer_feedback_form','action_quotations','action_orders']]]],{fields:['name','res_id']});
 const ids=Object.fromEntries(xmlids.map(r=>[r.name,r.res_id]));
 const defaultView=await call('sale.order','get_view',[],{view_type:'form'});
 assert(defaultView.id===ids.view_order_form,'Default view is still customer feedback');checks.push('默认表单恢复为销售报价主表单');
 for(const key of ['action_quotations','action_orders']) {
  await page.goto(origin+'/odoo/action-'+ids[key]);await page.locator('.o_list_view').waitFor();
  await page.getByRole('button',{name:'新建',exact:true}).click();
  await page.locator('.o_form_view').waitFor();await page.getByText('图纸与选型',{exact:true}).first().waitFor();
  assert(await page.getByText('图纸与选型',{exact:true}).count()>0,'Drawing tab missing');
  assert(await page.getByText('订单明细',{exact:true}).count()>0,'Order lines missing');
  assert(await page.getByRole('button',{name:'登记客户反馈',exact:true}).count()===0,'Unsent new quote shows feedback entry');
  assert(await page.getByRole('radio',{name:'中标',exact:true}).count()===0,'Feedback choice appears on new quote');
  await page.screenshot({path:path.join(work,'默认表单修复-'+key+'-新建.png'),fullPage:true});
  checks.push(key+'：点击新建打开报价主界面，未发送不出现反馈登记');
 }
 const result=await call('sale.order','create',[{name:'REPORT-FORM-VERIFY-'+Date.now(),partner_id:before[0].partner_id[0],state:'sent',quotation_workflow_step:'quotation'}]);
 testOrder=Array.isArray(result)?result[0]:result;
 const action=await call('sale.order','action_open_customer_feedback',[[testOrder]]);
 assert(action.views[0][0]===ids.sale_order_customer_feedback_form&&action.target==='new','Feedback action lost explicit dialog view');
 await page.goto(origin+'/odoo/action-'+ids.action_quotations+'/'+testOrder);await page.getByRole('button',{name:'登记客户反馈',exact:true}).waitFor();
 await page.getByRole('button',{name:'登记客户反馈',exact:true}).click();
 const dialog=page.getByRole('dialog');await dialog.getByRole('radio',{name:'中标',exact:true}).waitFor();
 assert(await dialog.getByRole('radio',{name:'失标',exact:true}).isVisible(),'Lost choice missing');
 await page.screenshot({path:path.join(work,'默认表单修复-发送后反馈弹窗.png'),fullPage:true});
 await dialog.getByRole('button',{name:'取消',exact:true}).click();
 checks.push('报价已发送后，登记客户反馈仍打开专用弹窗');
 assert(errors.length===0,'Browser errors: '+JSON.stringify(errors));
 checks.push('无浏览器运行错误');
} catch(error) {console.log('FORM_BROWSER_ERRORS',JSON.stringify(errors));await page.screenshot({path:path.join(work,'默认表单修复-调试.png'),fullPage:true});throw error;}
finally {
 if(testOrder){await call('sale.order','write',[[testOrder],{state:'cancel'}]);await call('sale.order','unlink',[[testOrder]]);}
 const after=await call('sale.order','search_read',[[]],{fields,order:'id'});
 assert(JSON.stringify(before)===JSON.stringify(after),'Original order data changed');
 await fs.writeFile(path.join(work,'默认报价表单修复验收.json'),JSON.stringify({checks,originalOrders:before.length,originalOrdersUnchanged:true,browserErrors:errors},null,2));
 await browser.close();
}
console.log('FORM_CHECKS',JSON.stringify(checks));
