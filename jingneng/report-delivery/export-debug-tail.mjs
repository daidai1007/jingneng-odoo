const page=await context.newPage();const logs=[];
page.on('pageerror',e=>logs.push('ERROR '+e.message));page.on('console',m=>{if(m.type()==='error')logs.push('CONSOLE '+m.text());});page.on('download',d=>logs.push('DOWNLOAD '+d.suggestedFilename()));
try{
 await page.goto(origin+'/odoo/jingneng-reports-customers');await page.locator('.jn-report-cards').waitFor();
 await page.evaluate(()=>{window.__exports=[];const create=URL.createObjectURL.bind(URL);URL.createObjectURL=(blob)=>{window.__exports.push({url:true,size:blob.size});return create(blob);};const click=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){window.__exports.push({anchor:true,href:this.href,filename:this.download});return click.call(this);};document.addEventListener('click',e=>window.__exports.push({click:true,text:e.target.textContent?.slice(0,30)}),true);});
 const button=page.getByRole('button',{name:'导出明细',exact:true});console.log('BUTTON',await button.evaluate(el=>el.outerHTML));await button.click();await page.waitForTimeout(1500);
 console.log('DOWNLOAD_DIAG',JSON.stringify(await page.evaluate(()=>window.__exports)));console.log('LOGS',JSON.stringify(logs));console.log('PAGES',context.pages().length);
}finally{await browser.close();}
