package ru.lenainbox.alerts;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

final class EventStore extends SQLiteOpenHelper {
    EventStore(Context context) { super(context, "events.db", null, 1); }
    @Override public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE events (id TEXT PRIMARY KEY, body TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL DEFAULT 0)");
    }
    @Override public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        throw new IllegalStateException("Database migration required");
    }
    synchronized void record(String id, String body) {
        ContentValues event = new ContentValues();
        event.put("id", id);
        event.put("body", body);
        getWritableDatabase().insertOrThrow("events", null, event);
    }
    synchronized String[] firstReady(long now) {
        try (Cursor row = getReadableDatabase().query("events", new String[]{"id", "body", "attempts"},
                "next_attempt_at <= ?", new String[]{Long.toString(now)}, null, null, "rowid ASC", "1")) {
            return row.moveToFirst() ? new String[]{row.getString(0), row.getString(1), row.getString(2)} : null;
        }
    }
    synchronized void defer(String id, long retryAt) {
        SQLiteDatabase db = getWritableDatabase();
        db.execSQL("UPDATE events SET attempts = attempts + 1, next_attempt_at = ? WHERE id = ?", new Object[]{retryAt, id});
    }
    synchronized void delivered(String id) { getWritableDatabase().delete("events", "id = ?", new String[]{id}); }
    synchronized long delayUntilNext(long now) {
        try (Cursor row = getReadableDatabase().rawQuery("SELECT MIN(next_attempt_at) FROM events", null)) {
            if (!row.moveToFirst() || row.isNull(0)) return -1;
            return Math.max(0L, row.getLong(0) - now);
        }
    }
    synchronized int count() {
        try (Cursor row = getReadableDatabase().rawQuery("SELECT COUNT(*) FROM events", null)) {
            row.moveToFirst(); return row.getInt(0);
        }
    }
}
