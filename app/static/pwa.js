(() => {
    const isStandalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
    const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
    let deferredPrompt = null;

    if ("serviceWorker" in navigator) {
        window.addEventListener("load", () => {
            navigator.serviceWorker.register("/notifications/service-worker.js?v=6.1", {scope: "/"})
                .then(registration => registration.update())
                .catch(error => console.warn("BrooksHouse service worker:", error));
        });
    }

    window.addEventListener("beforeinstallprompt", event => {
        event.preventDefault();
        deferredPrompt = event;
        document.querySelectorAll("[data-pwa-install]").forEach(button => {
            button.hidden = false;
        });
    });

    window.addEventListener("appinstalled", () => {
        deferredPrompt = null;
        document.querySelectorAll("[data-pwa-install]").forEach(button => button.hidden = true);
        document.querySelectorAll("[data-pwa-status]").forEach(box => box.textContent = "✅ BrooksHouse is installed on this device.");
    });

    document.addEventListener("click", async event => {
        const button = event.target.closest("[data-pwa-install]");
        if (!button) return;
        if (deferredPrompt) {
            deferredPrompt.prompt();
            await deferredPrompt.userChoice;
            deferredPrompt = null;
            return;
        }
        const instructions = document.querySelector("[data-ios-instructions]");
        if (instructions) instructions.hidden = false;
    });

    document.addEventListener("DOMContentLoaded", () => {
        document.documentElement.classList.toggle("pwa-standalone", isStandalone);
        document.querySelectorAll("[data-pwa-status]").forEach(box => {
            if (isStandalone) box.textContent = "✅ BrooksHouse is running as an installed app.";
            else if (isIOS) box.textContent = "On iPhone: use Safari Share → Add to Home Screen, then open the new icon.";
            else box.textContent = "Install BrooksHouse for faster full-screen access.";
        });
        if (isIOS && !isStandalone) {
            document.querySelectorAll("[data-ios-instructions]").forEach(box => box.hidden = false);
        }
    });
})();
