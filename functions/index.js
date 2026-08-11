const { onDocumentCreated } = require('firebase-functions/v2/firestore');
const { onSchedule } = require('firebase-functions/v2/scheduler');
const { defineSecret } = require('firebase-functions/params');
const { initializeApp } = require('firebase-admin/app');
const { getMessaging } = require('firebase-admin/messaging');
const { getFirestore } = require('firebase-admin/firestore');
const { google } = require('googleapis');

const SHEETS_KEY = defineSecret('SHEETS_SERVICE_ACCOUNT_KEY');
const SHEET_ID = '1Y3bFg5hEGY1Dn6kR3VH_a6fdPTzhOgi3sPtRrp7DcT8';

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

exports.closeRoutes = onSchedule({ schedule: '0 19 * * *', timeZone: 'America/New_York' }, async () => {
    const db = getFirestore();
    const messaging = getMessaging();
    const todayDate = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
    const now = new Date().toISOString();

    const snap = await db.collection('artifacts/default-app-id/active_routes').get();

    for (const docSnap of snap.docs) {
        const data = docSnap.data();
        if (!data.driverName || !data.createdAt) continue;

        const createdDate = new Date(data.createdAt).toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
        if (createdDate !== todayDate) continue;

        const stops = data.stops || [];
        const hasPending = stops.some(s => !s.delivered);
        if (!hasPending) continue;

        // Auto-confirm pending stops
        const updatedStops = stops.map(s =>
            s.delivered ? s : { ...s, delivered: true, autoConfirmed: true, deliveredAt: now }
        );
        await docSnap.ref.update({ stops: updatedStops });
        console.log(`Auto-confirmed stops for ${data.driverName}`);

        // Notify driver
        const key = data.driverName.toLowerCase().replace(/\s+/g, '_');
        const tokenDoc = await db.doc(`driver_fcm_tokens/${key}`).get();
        if (!tokenDoc.exists) continue;
        const { token } = tokenDoc.data();
        if (!token) continue;

        const appUrl = process.env.APP_URL || 'https://economy-route-planne.onrender.com';
        try {
            await messaging.send({
                token,
                notification: {
                    title: 'ESS Route Planner',
                    body: `${data.driverName}, your route has been closed. Some stops were auto-confirmed.`,
                },
                webpush: {
                    notification: { icon: 'https://economysignsupply.com/wp-content/uploads/2024/07/ess-logo-svg-100.svg' },
                    fcmOptions: { link: `${appUrl}/driver.html?r=${docSnap.id}` },
                },
            });
        } catch (e) {
            console.error(`closeRoutes push failed for ${data.driverName}:`, e.message);
        }
    }
});

exports.remindDrivers = onSchedule('every 30 minutes', async () => {
    const db = getFirestore();
    const messaging = getMessaging();
    const now = Date.now();
    const TWO_HOURS = 2 * 60 * 60 * 1000;

    const snap = await db.collection('artifacts/default-app-id/active_routes').get();

    for (const docSnap of snap.docs) {
        const data = docSnap.data();

        // Skip if already sent reminder or no driver
        if (data.reminderSent || !data.driverName || !data.createdAt) continue;

        const createdMs = new Date(data.createdAt).getTime();

        // Skip routes from previous days (only remind for today's routes)
        const createdDate = new Date(data.createdAt).toISOString().slice(0, 10);
        const todayDate = new Date().toISOString().slice(0, 10);
        if (createdDate !== todayDate) continue;

        // Skip if less than 2 hours have passed
        if (now - createdMs < TWO_HOURS) continue;

        // Skip if any stop has been delivered
        const stops = data.stops || [];
        const anyDelivered = stops.some(s => s.delivered === true);
        if (anyDelivered) continue;

        // Get driver FCM token
        const key = data.driverName.toLowerCase().replace(/\s+/g, '_');
        const tokenDoc = await db.doc(`driver_fcm_tokens/${key}`).get();
        if (!tokenDoc.exists) continue;
        const { token } = tokenDoc.data();
        if (!token) continue;

        const routeToken = docSnap.id;
        const appUrl = process.env.APP_URL || 'https://economy-route-planne.onrender.com';

        try {
            await messaging.send({
                token,
                notification: {
                    title: 'ESS Route Planner',
                    body: `${data.driverName}, remember to use the app to mark your deliveries as completed.`,
                },
                webpush: {
                    notification: {
                        icon: 'https://economysignsupply.com/wp-content/uploads/2024/07/ess-logo-svg-100.svg',
                    },
                    fcmOptions: { link: `${appUrl}/driver.html?r=${routeToken}` },
                },
            });
            console.log(`Reminder sent to ${data.driverName}`);
        } catch (e) {
            console.error(`Reminder FCM failed for ${data.driverName}:`, e.message);
        }

        // Mark so we don't send again
        await docSnap.ref.update({ reminderSent: true });
    }
});

exports.logRouteToSheets = onDocumentCreated(
    { document: 'artifacts/default-app-id/active_routes/{routeToken}', secrets: [SHEETS_KEY] },
    async (event) => {
        const data = event.data.data();
        const token = event.params.routeToken;
        const stops = data.stops || [];
        if (stops.length === 0) return null;

        let credentials;
        try {
            credentials = JSON.parse(SHEETS_KEY.value());
        } catch (e) {
            console.error('Failed to parse SHEETS_SERVICE_ACCOUNT_KEY:', e.message);
            return null;
        }

        const auth = new google.auth.GoogleAuth({
            credentials,
            scopes: ['https://www.googleapis.com/auth/spreadsheets'],
        });

        const sheets = google.sheets({ version: 'v4', auth });
        const dispatchDate = data.createdAt
            ? new Date(data.createdAt).toLocaleDateString('en-US', { timeZone: 'America/New_York' })
            : new Date().toLocaleDateString('en-US', { timeZone: 'America/New_York' });

        const rows = stops.map((stop, i) => [
            dispatchDate,
            token,
            data.zoneName || '',
            data.driverName || '',
            i + 1,
            stop.nombre || stop.name || '',
            stop.direccion || stop.address || '',
            stop.invoices || '',
            stop.pieces || '',
            stop.notes || '',
        ]);

        try {
            // Ensure header row exists on first use
            const meta = await sheets.spreadsheets.values.get({
                spreadsheetId: SHEET_ID,
                range: 'Sheet1!A1:J1',
            });
            if (!meta.data.values || meta.data.values.length === 0) {
                await sheets.spreadsheets.values.update({
                    spreadsheetId: SHEET_ID,
                    range: 'Sheet1!A1',
                    valueInputOption: 'RAW',
                    requestBody: { values: [['Date', 'Route Token', 'Zone', 'Driver', 'Stop #', 'Client', 'Address', 'Invoices', 'Pieces', 'Notes']] },
                });
            }

            await sheets.spreadsheets.values.append({
                spreadsheetId: SHEET_ID,
                range: 'Sheet1!A:J',
                valueInputOption: 'RAW',
                insertDataOption: 'INSERT_ROWS',
                requestBody: { values: rows },
            });
            console.log(`Logged ${rows.length} stops for route ${token} to Google Sheets`);
        } catch (e) {
            console.error('Sheets API error:', e.message);
        }
        return null;
    }
);
