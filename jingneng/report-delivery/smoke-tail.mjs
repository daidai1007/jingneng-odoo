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
