package ru.lenainbox.alerts;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Typeface;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

public final class MainActivity extends Activity {
    private EditText origin;
    private EditText token;
    private EditText packages;
    private TextView status;
    private TextView account;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        ScrollView scroll = new ScrollView(this);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        int padding = Math.round(20 * getResources().getDisplayMetrics().density);
        layout.setPadding(padding, padding, padding, padding);
        scroll.addView(layout);
        setContentView(scroll);
        TextView title = text("Сигналы обращений", 24);
        title.setTypeface(null, Typeface.BOLD);
        layout.addView(title);
        layout.addView(text("Доступ Android позволяет видеть уведомления других приложений. Этот код передаёт только сигнал и технические идентификаторы. Текст, имена и документы не передаются.", 16));
        layout.addView(text("Идентификатор этого телефона", 15));
        account = text(new ru.lenainbox.alerts.Settings(this).accountId(), 14);
        account.setTextIsSelectable(true);
        layout.addView(account);
        layout.addView(text("HTTPS-адрес приёмника", 15));
        origin = input("https://example.org", false);
        origin.setText(new ru.lenainbox.alerts.Settings(this).origin());
        layout.addView(origin);
        layout.addView(text("Токен этого телефона", 15));
        token = input("Введите новый токен", true);
        layout.addView(token);
        layout.addView(text("Идентификаторы приложений через запятую", 15));
        packages = input("com.whatsapp,org.telegram.messenger,ru.oneme.app", false);
        packages.setText(new ru.lenainbox.alerts.Settings(this).packages());
        layout.addView(packages);
        Button save = button("Сохранить настройки");
        save.setOnClickListener(view -> save());
        layout.addView(save);
        Button access = button("Открыть доступ к уведомлениям");
        access.setOnClickListener(view -> startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)));
        layout.addView(access);
        Button refresh = button("Обновить состояние и повторить доставку");
        refresh.setOnClickListener(view -> { SendJob.schedule(this); updateStatus(); });
        layout.addView(refresh);
        status = text("", 15);
        layout.addView(status);
        layout.addView(text("После установки проверьте отдельным тестовым сообщением и звонком каждое приложение. Android может задерживать фоновые задания, а некоторые уведомления не содержат данных о типе обращения или диалоге.", 14));
    }
    @Override protected void onResume() { super.onResume(); if (status != null) updateStatus(); }

    private void save() {
        try {
            String endpoint = ru.lenainbox.alerts.Settings.validateOrigin(origin.getText().toString());
            String selection = ru.lenainbox.alerts.Settings.validatePackages(packages.getText().toString());
            String newToken = token.getText().toString();
            if (!newToken.isEmpty() && !newToken.matches("[A-Za-z0-9._~-]{24,512}")) {
                throw new IllegalArgumentException("Проверьте токен устройства");
            }
            synchronized (ru.lenainbox.alerts.Settings.LOCK) {
                ru.lenainbox.alerts.Settings settings = new ru.lenainbox.alerts.Settings(this);
                try (EventStore store = new EventStore(this)) {
                    if (store.count() > 0 && !settings.origin().isEmpty() && !settings.origin().equals(endpoint)) {
                        throw new IllegalArgumentException("Сначала доставьте очередь на прежний адрес");
                    }
                }
                Vault vault = new Vault(this);
                if (newToken.isEmpty() && vault.getToken().isEmpty()) throw new IllegalArgumentException("Введите токен устройства");
                if (!newToken.isEmpty()) vault.putToken(newToken);
                settings.save(endpoint, selection);
            }
            token.setText("");
            SendJob.schedule(this);
            status.setText("Настройки сохранены. Включите доступ к уведомлениям Android.");
        } catch (Exception failure) {
            String message = failure instanceof IllegalArgumentException ? failure.getMessage() : "Не удалось сохранить настройки";
            status.setText(message);
        }
    }
    private void updateStatus() {
        String listener = getSharedPreferences("diagnostics", MODE_PRIVATE).getString("listener", "Доступ к уведомлениям ещё не подтверждён");
        String delivery = getSharedPreferences("diagnostics", MODE_PRIVATE).getString("delivery", "Доставка ещё не проверена");
        try (EventStore store = new EventStore(this)) {
            status.setText(listener + "\n" + delivery + "\nВ очереди: " + store.count());
        } catch (Exception failure) { status.setText("Не удалось прочитать очередь"); }
    }
    private TextView text(String value, int size) {
        TextView view = new TextView(this);
        view.setText(value); view.setTextSize(size); view.setPadding(0, 12, 0, 6);
        return view;
    }
    private EditText input(String hint, boolean secret) {
        EditText view = new EditText(this);
        view.setSingleLine(!hint.contains(","));
        view.setHint(hint);
        if (secret) view.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        else view.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        return view;
    }
    private Button button(String label) {
        Button button = new Button(this);
        button.setText(label);
        button.setLayoutParams(new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return button;
    }
}
