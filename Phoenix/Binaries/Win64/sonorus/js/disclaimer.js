(function() {
    const STORAGE_KEY = 'sonorus_disclaimer_accepted';

    if (localStorage.getItem(STORAGE_KEY)) return;

    const overlay = document.createElement('div');
    overlay.id = 'disclaimerOverlay';
    overlay.style.cssText = `
        position: fixed; inset: 0; z-index: 99999;
        background: rgba(10, 8, 6, 0.92);
        display: flex; align-items: center; justify-content: center;
        font-family: var(--font-body, 'Crimson Text', serif);
    `;

    const modal = document.createElement('div');
    modal.style.cssText = `
        background: var(--leather-mid, #2a1f1a);
        border: 2px solid var(--gold-dark, #8b6914);
        border-radius: 12px;
        max-width: 520px; width: 90%;
        padding: 2rem;
        color: var(--parchment-light, #f4e4c1);
        box-shadow: 0 0 40px rgba(0,0,0,0.6), 0 0 8px rgba(212,168,75,0.15);
        text-align: center;
    `;

    modal.innerHTML = `
        <h2 style="font-family: var(--font-display, 'Cinzel', serif); color: var(--gold-bright, #d4a84b); margin: 0 0 1rem;">
            AI Disclaimer
        </h2>
        <p style="line-height: 1.6; margin: 0 0 0.8rem;">
            Sonorus uses AI language models to generate NPC dialogue. Responses are <strong>not curated</strong> and may be inaccurate, unexpected, or occasionally offensive.
        </p>
        <p style="line-height: 1.6; margin: 0 0 0.8rem;">
            Do not take anything an NPC says as fact or personal advice. These are fictional characters powered by probabilistic text generation, not real people.
        </p>
        <p style="line-height: 1.6; margin: 0 0 1.5rem; font-size: 0.95em; opacity: 0.85;">
            Please be mindful of this, especially during extended play sessions.
            <a href="https://en.wikipedia.org/wiki/Chatbot_psychosis" target="_blank" rel="noopener"
               style="color: var(--gold-bright, #d4a84b);">Learn more</a>
        </p>
        <button id="disclaimerAccept" style="
            font-family: var(--font-display, 'Cinzel', serif);
            background: linear-gradient(135deg, var(--gold-dark, #8b6914), var(--gold-bright, #d4a84b));
            color: var(--ink-black, #1a1410);
            border: none; border-radius: 6px;
            padding: 0.7rem 2rem; font-size: 1rem;
            cursor: pointer; font-weight: 600;
        ">I Understand</button>
    `;

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    document.getElementById('disclaimerAccept').addEventListener('click', function() {
        localStorage.setItem(STORAGE_KEY, '1');
        overlay.remove();
    });
})();
