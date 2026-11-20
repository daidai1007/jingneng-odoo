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
const page=await context.newPage();const logs=[];
page.on('pageerror',e=>logs.push('ERROR '+e.message));page.on('console',m=>{if(m.type()==='error')logs.push('CONSOLE '+m.text());});page.on('download',d=>logs.push('DOWNLOAD '+d.suggestedFilename()));
try{
 await page.goto(origin+'/odoo/jingneng-reports-customers');await page.locator('.jn-report-cards').waitFor();
 await page.evaluate(()=>{window.__exports=[];const create=URL.createObjectURL.bind(URL);URL.createObjectURL=(blob)=>{window.__exports.push({url:true,size:blob.size});return create(blob);};const click=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){window.__exports.push({anchor:true,href:this.href,filename:this.download});return click.call(this);};document.addEventListener('click',e=>window.__exports.push({click:true,text:e.target.textContent?.slice(0,30)}),true);});
 const button=page.getByRole('button',{name:'导出明细',exact:true});console.log('BUTTON',await button.evaluate(el=>el.outerHTML));await button.click();await page.waitForTimeout(1500);
 console.log('DOWNLOAD_DIAG',JSON.stringify(await page.evaluate(()=>window.__exports)));console.log('LOGS',JSON.stringify(logs));console.log('PAGES',context.pages().length);
}finally{await browser.close();}
