function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const rawData = atob(base64);
    return Uint8Array.from([...rawData].map(character => character.charCodeAt(0)));
}

const statusBox = document.getElementById("pushStatus");
const enableButton = document.getElementById("enablePush");
const disableButton = document.getElementById("disablePush");

async function registration() {
    return navigator.serviceWorker.register("/notifications/service-worker.js", {scope: "/"});
}

async function refreshStatus() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        statusBox.textContent = window.isSecureContext
            ? "This browser does not support web push."
            : "Web push is blocked because this page is not using HTTPS.";
        enableButton.disabled = true;
        return;
    }
    const currentRegistration = await registration();
    const subscription = await currentRegistration.pushManager.getSubscription();
    statusBox.textContent = subscription
        ? "✅ Notifications are enabled on this device."
        : "Notifications are not enabled on this device yet.";
}

enableButton.addEventListener("click", async () => {
    try {
        const permission = await Notification.requestPermission();
        if (permission !== "granted") throw new Error("Notification permission was not granted.");
        const response = await fetch("/api/notifications/public-key");
        if (!response.ok) throw new Error("Could not load the notification key.");
        const {public_key: publicKey} = await response.json();
        const currentRegistration = await registration();
        let subscription = await currentRegistration.pushManager.getSubscription();
        if (!subscription) {
            subscription = await currentRegistration.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(publicKey)
            });
        }
        const saveResponse = await fetch("/api/notifications/subscribe", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                subscription: subscription.toJSON(),
                device_name: document.getElementById("deviceName").value
            })
        });
        if (!saveResponse.ok) throw new Error(await saveResponse.text());
        await refreshStatus();
    } catch (error) {
        statusBox.textContent = "Could not enable notifications: " + error.message;
    }
});

disableButton.addEventListener("click", async () => {
    try {
        const currentRegistration = await registration();
        const subscription = await currentRegistration.pushManager.getSubscription();
        if (subscription) {
            await fetch("/api/notifications/unsubscribe", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({endpoint: subscription.endpoint})
            });
            await subscription.unsubscribe();
        }
        await refreshStatus();
    } catch (error) {
        statusBox.textContent = "Could not disable notifications: " + error.message;
    }
});

refreshStatus().catch(error => statusBox.textContent = error.message);
