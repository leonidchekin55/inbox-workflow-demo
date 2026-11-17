package ru.lenainbox.alerts;

import android.app.Notification;
import android.content.Context;
import android.os.Build;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import org.json.JSONObject;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;
import java.util.UUID;

public final class InboxListener extends NotificationListenerService {
    @Override public void onListenerConnected() {
        super.onListenerConnected();
        try {
            SendJob.schedule(this);
            status("Подключено к уведомлениям Android");
        } catch (Exception failure) { status("Ошибка доступа к уведомлениям"); }
    }
    @Override public void onListenerDisconnected() {
        super.onListenerDisconnected();
        status("Android отключил доступ к уведомлениям");
    }
    private void status(String text) {
        getSharedPreferences("diagnostics", Context.MODE_PRIVATE).edit().putString("listener", text).apply();
    }
    @Override public void onNotificationPosted(StatusBarNotification sbn) {
        if (sbn == null || sbn.getNotification() == null || sbn.getPackageName().equals(getPackageName())) return;
        Notification n = sbn.getNotification();
        if ((n.flags & Notification.FLAG_GROUP_SUMMARY) != 0) return;
        Settings settings = new Settings(this);
        if (!settings.selected(sbn.getPackageName())) return;
        Bundle extras = n.extras;
        String template = extras == null ? null : extras.getString(Notification.EXTRA_TEMPLATE);
        boolean call = Notification.CATEGORY_CALL.equals(n.category) || (template != null && template.endsWith("$CallStyle"));
        boolean message = Notification.CATEGORY_MESSAGE.equals(n.category) || (template != null && template.endsWith("$MessagingStyle"));
        if (!call && !message) return;
        try {
            String eventId = UUID.randomUUID().toString();
            String shortcut = Build.VERSION.SDK_INT >= 26 ? n.getShortcutId() : null;
            String client = shortcut == null || shortcut.isEmpty() ? null : Identity.hash("client|" + sbn.getPackageName() + "|" + shortcut);
            SimpleDateFormat format = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US);
            format.setTimeZone(TimeZone.getTimeZone("UTC"));
            JSONObject body = new JSONObject();
            body.put("event_id", eventId);
            body.put("platform", sbn.getPackageName());
            body.put("account_id", settings.accountId());
            body.put("kind", call ? "call" : "message");
            body.put("occurred_at", format.format(new Date()));
            body.put("client_id", client == null ? JSONObject.NULL : client);
            synchronized (Settings.LOCK) {
                try (EventStore store = new EventStore(this)) { store.record(eventId, body.toString()); }
                SendJob.schedule(this);
            }
        } catch (Exception failure) { status("Ошибка сохранения сигнала"); }
    }
}
