(function () {
    function money(value) { return "$" + Number(value).toFixed(2); }
    function show(alert) {
        let root = document.getElementById("hot-item-alert-live");
        if (!root) {
            root = document.createElement("section");
            root.id = "hot-item-alert-live";
            document.body.prepend(root);
        }
        if (!alert) { root.hidden = true; root.innerHTML = ""; return; }
        root.hidden = false;
        root.style.cssText = "position:sticky;top:0;z-index:9999;margin:0;padding:16px;background:#fff3cd;border:4px solid #f59e0b;color:#3f2d00;box-shadow:0 5px 20px #0004";
        const margin = alert.margin == null ? "" : `<p><b>Estimated margin:</b> ${Number(alert.margin).toFixed(1)}%</p>`;
        const fee = alert.economics_reliable ? "" : " <small>(before an unavailable Walmart fee)</small>";
        const actions = (alert.actions || []).map(a => `<a href="${a.url}" style="display:inline-block;margin:5px;padding:10px 12px;border-radius:8px;background:${a.kind === 'primary' ? '#86151d' : '#34495e'};color:white;text-decoration:none;font-weight:700">${a.label}</a>`).join("");
        root.innerHTML = `<h2 style="margin:0 0 8px">${alert.title}</h2><p><b>Walmart price:</b> ${money(alert.walmart_price)}</p><p><b>Estimated profit per found unit:</b> ${money(alert.estimated_profit_per_unit)}${fee}</p>${margin}<p>${alert.message}</p><p><b>${alert.instruction}</b></p><div>${actions}</div>`;
        root.scrollIntoView({behavior:"smooth", block:"start"});
    }
    window.HotItemAlert = {show};
})();
