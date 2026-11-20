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
const page=await context.newPage();page.setDefaultTimeout(30000);
const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text().slice(0,1000));});
try {
 const data=await call('sale.report','get_jingneng_report_data',[{analysis:'bids',period:'6'}]);
 console.log('REPORT_DATA',JSON.stringify({rows:data.rows.length,missing:data.missing_sent,currencies:data.currencies.length}));
 const actions=await call('ir.actions.client','search_read',[[['tag','=','sale.jingneng_reports']]],{fields:['id','name','path','params']});
 console.log('REPORT_ACTIONS',JSON.stringify(actions));
 await page.goto(origin+'/odoo/'+actions[0].path);
 await page.locator('.jn-reports').waitFor();
 await page.getByRole('button',{name:'销售订单业绩',exact:true}).click();
 await page.getByText('正在加载报表…',{exact:true}).waitFor({state:'hidden'});
 await page.locator('.jn-report-cards').waitFor();
 await page.screenshot({path:path.join(work,'报表实际界面-销售.png'),fullPage:true});
 console.log('PAGE_TEXT',(await page.locator('.jn-reports').innerText()).slice(0,2500));
 console.log('BROWSER_ERRORS',JSON.stringify(errors));
} catch(e) { console.log('BROWSER_ERRORS',JSON.stringify(errors));console.log('BODY',(await page.locator('body').innerText()).slice(0,3000));await page.screenshot({path:path.join(work,'报表调试.png'),fullPage:true});throw e; } finally { await browser.close(); }
