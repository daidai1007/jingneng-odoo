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
 const views=await call('ir.ui.view','search_read',[[['model','=','sale.order'],['type','=','form'],['mode','=','primary']]],{fields:['id','name','priority'],order:'priority,id'});
 console.log('FORM_PRIORITIES',JSON.stringify(views));
 const result=await call('sale.order','get_view',[],{view_type:'form'});console.log('DEFAULT_FORM',result.id,result.arch.slice(0,120));
}finally{await browser.close();}
