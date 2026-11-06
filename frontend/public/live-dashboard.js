// Auto-refresh dashboard every 5 seconds
setInterval(function() {
    fetch('/api/system/status').then(r => r.json()).then(data => {
        if (data.risk_state) {
            document.querySelectorAll('[data-stat="equity"]').forEach(el => {
                el.textContent = '$' + (data.risk_state.equity || 0).toFixed(2);
            });
            document.querySelectorAll('[data-stat="win-rate"]').forEach(el => {
                el.textContent = (data.risk_state.win_rate || 0).toFixed(1) + '%';
            });
            document.querySelectorAll('[data-stat="positions"]').forEach(el => {
                el.textContent = data.risk_state.open_positions_count || 0;
            });
        }
    }).catch(() => {});
}, 5000);
