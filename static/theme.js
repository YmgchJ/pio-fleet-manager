// Local display preference only. No robot or server settings are changed.
(() => {
    const key = 'fleet-manager-theme';
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const valid = value => ['light', 'dark', 'system'].includes(value);
    let preference = 'light';
    try { const saved = localStorage.getItem(key); if (valid(saved)) preference = saved; } catch (_) {}
    function apply(value, save = false) {
        preference = valid(value) ? value : 'light';
        document.documentElement.dataset.theme = preference === 'system' ? (media.matches ? 'dark' : 'light') : preference;
        if (save) { try { localStorage.setItem(key, preference); } catch (_) {} }
        const select = document.getElementById('display-theme');
        if (select) select.value = preference;
        document.dispatchEvent(new CustomEvent('fleet-theme-change'));
    }
    window.fleetThemeColors = () => {
        const style = getComputedStyle(document.documentElement);
        return { text: style.getPropertyValue('--dads-ink').trim(), muted: style.getPropertyValue('--dads-muted').trim(), grid: style.getPropertyValue('--dads-rule').trim() };
    };
    apply(preference);
    media.addEventListener('change', () => { if (preference === 'system') apply('system'); });
    window.addEventListener('storage', event => { if (event.key === key) apply(event.newValue || 'light'); });
    document.addEventListener('DOMContentLoaded', () => {
        apply(preference);
        document.getElementById('display-theme')?.addEventListener('change', event => apply(event.target.value, true));
    });
})();
