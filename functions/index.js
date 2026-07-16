const { onDocumentCreated } = require('firebase-functions/v2/firestore');
const { initializeApp } = require('firebase-admin/app');
const { getMessaging } = require('firebase-admin/messaging');
const { getFirestore } = require('firebase-admin/firestore');

initializeApp();

exports.notifyDriver = onDocumentCreated(
    'artifacts/default-app-id/active_routes/{routeToken}',
    async (event) => {
        const data = event.data.data();
        const driverName = data.driverName;
        if (!driverName) return null;

        const key = driverName.toLowerCase().replace(/\s+/g, '_');
        const tokenDoc = await getFirestore().doc(`driver_fcm_tokens/${key}`).get();
        if (!tokenDoc.exists) return null;

        const { token } = tokenDoc.data();
        if (!token) return null;

        try {
            await getMessaging().send({
                token,
                notification: {
                    title: 'ESS Route Planner',
                    body: `Hi ${driverName}, your route is ready. Go to dispatch to get started.`,
                },
                webpush: {
                    notification: {
                        icon: 'https://economysignsupply.com/wp-content/uploads/2024/07/ess-logo-svg-100.svg',
                    },
                    fcmOptions: {
                        link: (process.env.APP_URL || 'https://economy-route-planne.onrender.com') + '/driver.html?r=' + event.params.routeToken,
                    },
                },
            });
            console.log(`Push sent to driver: ${driverName}`);
        } catch (e) {
            console.error(`FCM send failed for ${driverName}:`, e.message);
        }
        return null;
    }
);
