package ru.lenainbox.alerts;

import android.content.Context;
import android.content.SharedPreferences;
import java.net.URI;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import java.util.UUID;

final class Settings {
    static final Object LOCK = new Object();
    static final String DEFAULT_PACKAGES = "com.whatsapp,com.whatsapp.w4b,org.telegram.messenger,ru.oneme.app";
    private final SharedPreferences prefs;

    Settings(Context context) { prefs = context.getSharedPreferences("config", Context.MODE_PRIVATE); }
    String accountId() {
        synchronized (LOCK) {
            String value = prefs.getString("account", "");
            if (value.isEmpty()) {
                value = UUID.randomUUID().toString();
                if (!prefs.edit().putString("account", value).commit()) throw new IllegalStateException("Cannot save installation ID");
            }
            return value;
        }
    }
    String origin() { return prefs.getString("origin", ""); }
    String packages() { return prefs.getString("packages", DEFAULT_PACKAGES); }
    void save(String origin, String packages) {
        if (!prefs.edit().putString("origin", origin).putString("packages", packages).commit()) {
            throw new IllegalStateException("Cannot save settings");
        }
    }
    boolean selected(String packageName) {
        Set<String> values = new HashSet<>(Arrays.asList(packages().split(",")));
        for (String value : values) if (value.trim().equals(packageName)) return true;
        return false;
    }
    static String validateOrigin(String text) {
        try {
            URI uri = new URI(text.trim());
            String path = uri.getPath();
            if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null
                    || uri.getQuery() != null || uri.getFragment() != null
                    || (path != null && !path.isEmpty() && !path.equals("/"))) throw new IllegalArgumentException();
            return new URI("https", null, uri.getHost(), uri.getPort(), null, null, null).toASCIIString();
        } catch (Exception invalid) { throw new IllegalArgumentException("Укажите HTTPS-адрес без пути, логина и параметров"); }
    }
    static String validatePackages(String text) {
        StringBuilder result = new StringBuilder();
        for (String item : text.split(",")) {
            String name = item.trim();
            if (!name.matches("[A-Za-z0-9_]+(?:\\.[A-Za-z0-9_]+)+") || name.length() > 128) {
                throw new IllegalArgumentException("Проверьте список идентификаторов приложений");
            }
            if (result.length() > 0) result.append(',');
            result.append(name);
        }
        if (result.length() == 0) throw new IllegalArgumentException("Выберите хотя бы одно приложение");
        return result.toString();
    }
}
