try {
 const rows=await call('sale.order','search_read',[[['name','=like','REPORT-VERIFY-%']]],{fields:['id','name']});
 console.log('TEMP_ORDER_CLEANUP',JSON.stringify(rows.map(r=>r.name)));
 const ids=rows.map(r=>r.id);
 if(ids.length){await call('sale.order','write',[ids,{state:'cancel'}]);await call('sale.order','write',[ids,{quotation_revision_of_id:false}]);await call('sale.order','unlink',[ids]);}
 const products=await call('product.product','search_read',[[['name','=like','REPORT-VERIFY-%']]],{fields:['id','name']});
 if(products.length)await call('product.product','unlink',[products.map(r=>r.id)]);
 console.log('ORIGINAL_ORDER_COUNT',await call('sale.order','search_count',[[]]));
} finally {await browser.close();}
