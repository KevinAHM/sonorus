(function () {
    const OVERLAY_ID = 'modConflictOverlay';

    function escapeText(value) {
        if (typeof window.escapeHtml === 'function') {
            return window.escapeHtml(String(value ?? ''));
        }
        const div = document.createElement('div');
        div.textContent = String(value ?? '');
        return div.innerHTML;
    }

    function closeModal() {
        document.getElementById(OVERLAY_ID)?.remove();
    }

    function hasIssues(result) {
        return Boolean(
            result?.error
            || result?.conflicts?.length
            || result?.sonorus_conflicts?.length
            || result?.entries_with_errors?.length
        );
    }

    function renderConflictGroups(result) {
        return (result.conflicts || []).map(conflict => {
            const modRows = conflict.mods.map(mod => `
                <li style="margin-bottom:0.45rem;">
                    <strong>${escapeText(mod.name)}</strong>
                </li>
            `).join('');

            return `
                <div style="margin-bottom:1rem; padding:0.9rem 1rem; border-radius:8px; background:rgba(201,122,43,0.06); border:1px solid rgba(201,122,43,0.18);">
                    <div style="font-family: var(--font-display, 'Cinzel', serif); color: var(--gold-bright, #d4a84b); margin-bottom:0.5rem;">
                        These mods conflict with each other
                    </div>
                    <ul style="margin:0; padding-left:1.2rem; line-height:1.5;">
                        ${modRows}
                    </ul>
                    <details style="margin-top:0.75rem; opacity:0.82;">
                        <summary style="cursor:pointer;">Technical details</summary>
                        <div style="margin-top:0.5rem; font-size:0.95rem;">
                            Shared package ID ${escapeText(conflict.package_id_hex)}
                        </div>
                    </details>
                </div>
            `;
        }).join('');
    }

    function renderSonorusConflicts(result) {
        if (!(result.sonorus_conflicts || []).length) {
            return '';
        }

        return `
            <div style="margin-bottom:1rem;">
                <div style="font-family: var(--font-display, 'Cinzel', serif); color: var(--gold-bright, #d4a84b); margin-bottom:0.5rem;">
                    These mods conflict with Sonorus
                </div>
                <p style="margin:0 0 0.75rem; line-height:1.5; opacity:0.9;">
                    If one of these stays enabled, Sonorus may fail to load part of its Blueprint support correctly.
                </p>
                <ul style="margin:0; padding-left:1.2rem; line-height:1.5;">
                    ${(result.sonorus_conflicts || []).map(mod => `
                        <li style="margin-bottom:0.45rem;"><strong>${escapeText(mod.name)}</strong></li>
                    `).join('')}
                </ul>
            </div>
        `;
    }

    function renderScanErrors(result) {
        if (!(result.entries_with_errors || []).length) {
            return '';
        }

        return `
            <div style="margin-top:1rem; padding-top:1rem; border-top:1px solid rgba(244,228,193,0.15);">
                <div style="font-family: var(--font-display, 'Cinzel', serif); color: var(--warning, #c97a2b); margin-bottom:0.5rem;">
                    Some mods could not be checked
                </div>
                <p style="margin:0 0 0.75rem; line-height:1.5; opacity:0.9;">
                    Sonorus could not read these mod files, so the results above may be incomplete.
                </p>
                <ul style="margin:0; padding-left:1.2rem; line-height:1.5;">
                    ${(result.entries_with_errors || []).map(entry => `
                        <li style="margin-bottom:0.45rem;">
                            <strong>${escapeText(entry.name)}</strong>
                        </li>
                    `).join('')}
                </ul>
                <details style="margin-top:0.75rem; opacity:0.82;">
                    <summary style="cursor:pointer;">Technical details</summary>
                    <ul style="margin:0.5rem 0 0; padding-left:1.2rem; line-height:1.5;">
                        ${(result.entries_with_errors || []).map(entry => `
                            <li style="margin-bottom:0.45rem;">
                                <strong>${escapeText(entry.name)}</strong>: ${escapeText(entry.error || 'Unknown error')}
                            </li>
                        `).join('')}
                    </ul>
                </details>
            </div>
        `;
    }

    function buildBodyHtml(result) {
        if (result.error) {
            return `
                <p style="line-height:1.6; margin:0 0 1rem;">
                    Sonorus could not finish checking your installed mods for conflicts.
                </p>
                <div style="padding:0.9rem 1rem; border-radius:8px; background:rgba(201,122,43,0.08); border:1px solid rgba(201,122,43,0.2); margin-bottom:1rem;">
                    Try pressing Recheck. If it keeps failing, you can still play, but Sonorus may not be able to warn you about mod collisions.
                </div>
                <details style="opacity:0.82;">
                    <summary style="cursor:pointer;">Technical details</summary>
                    <div style="margin-top:0.5rem;">
                        <code style="white-space:pre-wrap;">${escapeText(result.error)}</code>
                    </div>
                </details>
            `;
        }

        return `
            <p style="line-height:1.6; margin:0 0 1rem;">
                Some of your installed mods were built in a way that can conflict with each other.
            </p>
            <p style="line-height:1.6; margin:0 0 1rem; opacity:0.92;">
                When this happens, one mod can override another, break features, or stop Sonorus from working the way it should.
            </p>
            <p style="line-height:1.6; margin:0 0 1rem; opacity:0.92;">
                Disable one of the conflicting mods, then press Recheck.
            </p>
            ${renderSonorusConflicts(result)}
            ${renderConflictGroups(result)}
            ${renderScanErrors(result)}
        `;
    }

    function showModal(result) {
        closeModal();

        const overlay = document.createElement('div');
        overlay.id = OVERLAY_ID;
        overlay.style.cssText = `
            position: fixed; inset: 0; z-index: 100000;
            background: rgba(10, 8, 6, 0.92);
            display: flex; align-items: center; justify-content: center;
            font-family: var(--font-body, 'Crimson Text', serif);
            padding: 1.5rem;
        `;

        const modal = document.createElement('div');
        modal.style.cssText = `
            background: var(--leather-mid, #2a1f1a);
            border: 2px solid var(--warning, #c97a2b);
            border-radius: 12px;
            max-width: 720px; width: 100%;
            max-height: 85vh; overflow-y: auto;
            padding: 2rem;
            color: var(--parchment-light, #f4e4c1);
            box-shadow: 0 0 40px rgba(0,0,0,0.6), 0 0 8px rgba(201,122,43,0.18);
        `;

        modal.innerHTML = `
            <div style="display:flex;justify-content:center;margin-bottom:0.8rem;color:var(--warning, #c97a2b);">
                <i data-lucide="alert-triangle"></i>
            </div>
            <h2 style="font-family: var(--font-display, 'Cinzel', serif); color: var(--warning, #c97a2b); margin: 0 0 1rem; text-align:center;">
                Mod Conflict Warning
            </h2>
            ${buildBodyHtml(result)}
            <div style="display:flex; gap:0.75rem; justify-content:center; margin-top:1.5rem; flex-wrap:wrap;">
                <button id="modConflictRecheck" style="
                    font-family: var(--font-display, 'Cinzel', serif);
                    background: linear-gradient(135deg, var(--gold-dark, #8b6914), var(--gold-bright, #d4a84b));
                    color: var(--ink-black, #1a1410);
                    border: none; border-radius: 6px;
                    padding: 0.7rem 1.4rem; font-size: 1rem; cursor: pointer; font-weight: 600;
                ">Recheck</button>
                <button id="modConflictClose" style="
                    font-family: var(--font-display, 'Cinzel', serif);
                    background: transparent;
                    color: var(--parchment-light, #f4e4c1);
                    border: 1px solid rgba(244,228,193,0.35); border-radius: 6px;
                    padding: 0.7rem 1.4rem; font-size: 1rem; cursor: pointer;
                ">Close</button>
            </div>
        `;

        overlay.appendChild(modal);
        document.body.appendChild(overlay);

        if (window.lucide) {
            lucide.createIcons({ nodes: [overlay] });
        }

        overlay.addEventListener('click', (event) => {
            if (event.target === overlay) {
                closeModal();
            }
        });

        document.getElementById('modConflictClose')?.addEventListener('click', closeModal);
        document.getElementById('modConflictRecheck')?.addEventListener('click', () => {
            void checkForConflicts(true);
        });
    }

    async function checkForConflicts(manual = false) {
        try {
            const response = await fetch('/api/mod-package-conflicts', { cache: 'no-store' });
            const result = await response.json();

            if (response.ok && !hasIssues(result)) {
                closeModal();
                if (manual && typeof window.showToast === 'function') {
                    window.showToast('No cooked mod package conflicts found', 'success');
                }
                return result;
            }

            showModal(result);
            return result;
        } catch (error) {
            const result = { error: error?.message || 'Conflict scan failed' };
            showModal(result);
            return result;
        }
    }

    window.ModPackageConflicts = {
        check: checkForConflicts,
        close: closeModal,
    };

    document.addEventListener('DOMContentLoaded', () => {
        void checkForConflicts();
    });
})();
