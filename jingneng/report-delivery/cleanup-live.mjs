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
try {
 const rows=await call('sale.order','search_read',[[['name','=like','REPORT-VERIFY-%']]],{fields:['id','name']});
 console.log('TEMP_ORDER_CLEANUP',JSON.stringify(rows.map(r=>r.name)));
 const ids=rows.map(r=>r.id);
 if(ids.length){await call('sale.order','write',[ids,{state:'cancel'}]);await call('sale.order','write',[ids,{quotation_revision_of_id:false}]);await call('sale.order','unlink',[ids]);}
 const products=await call('product.product','search_read',[[['name','=like','REPORT-VERIFY-%']]],{fields:['id','name']});
 if(products.length)await call('product.product','unlink',[products.map(r=>r.id)]);
 console.log('ORIGINAL_ORDER_COUNT',await call('sale.order','search_count',[[]]));
} finally {await browser.close();}
