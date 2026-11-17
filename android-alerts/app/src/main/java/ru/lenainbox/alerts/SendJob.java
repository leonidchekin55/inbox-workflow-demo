package ru.lenainbox.alerts;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import java.io.OutputStream;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import javax.net.ssl.HttpsURLConnection;
import org.json.JSONObject;
import org.json.JSONTokener;

public final class SendJob extends JobService {
    private static final int JOB_A = 31071;
    private static final int JOB_B = 31072;
    private static final Object JOB_LOCK = new Object();
    private static final Object SEND_LOCK = new Object();
    private static final Map<Integer, Run> RUNS = new HashMap<>();
    private static int lastFinishedId = -1;
    private static boolean newWork;

    private static final class Run {
        final JobParameters params;
        volatile boolean stopped;
        Run(JobParameters params) { this.params = params; }
    }

    static void schedule(Context context) {
        WatchdogJob.ensure(context);
        synchronized (JOB_LOCK) {
            if (!RUNS.isEmpty()) {
                newWork = true;
                return;
            }
            JobScheduler scheduler = (JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE);
            for (JobInfo pending : scheduler.getAllPendingJobs()) {
                int id = pending.getId();
                if ((id == JOB_A || id == JOB_B) && id != lastFinishedId && pending.getMinLatencyMillis() == 0) return;
            }
            scheduleIdle(context, alternate(lastFinishedId), 0);
        }
    }
    private static int alternate(int id) { return id == JOB_A ? JOB_B : JOB_A; }
    private static void scheduleIdle(Context context, int jobId, long delayMillis) {
        JobScheduler scheduler = (JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        JobInfo job = new JobInfo.Builder(jobId, new ComponentName(context, SendJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setMinimumLatency(delayMillis)
                .setBackoffCriteria(30_000, JobInfo.BACKOFF_POLICY_EXPONENTIAL)
                .setPersisted(true).build();
        if (scheduler.schedule(job) != JobScheduler.RESULT_SUCCESS) {
            context.getSharedPreferences("diagnostics", MODE_PRIVATE).edit().putString("delivery", "Android не запланировал доставку").apply();
        }
    }

    @Override public boolean onStartJob(JobParameters params) {
        Run run = new Run(params);
        synchronized (JOB_LOCK) { RUNS.put(params.getJobId(), run); }
        new Thread(() -> {
            boolean retry = false;
            boolean unexpectedFailure = false;
            try {
                synchronized (SEND_LOCK) { if (!run.stopped) retry = deliver(run); }
            } catch (Exception failure) {
                diagnostic("Доставка ожидает повторной попытки");
                retry = true;
                unexpectedFailure = true;
            }
            synchronized (JOB_LOCK) {
                int id = params.getJobId();
                if (RUNS.get(id) == run) {
                    RUNS.remove(id);
                    lastFinishedId = id;
                    if (!run.stopped) jobFinished(params, false);
                    if (RUNS.isEmpty()) {
                        boolean reschedule = retry || newWork;
                        newWork = false;
                        if (!run.stopped && reschedule) {
                            try (EventStore store = new EventStore(this)) {
                                long delay = store.delayUntilNext(System.currentTimeMillis());
                                if (delay >= 0) scheduleIdle(this, alternate(id), unexpectedFailure ? Math.max(delay, 30_000L) : delay);
                            }
                        }
                    }
                }
            }
        }, "alert-delivery").start();
        return true;
    }

    private boolean deliver(Run run) throws Exception {
        try (EventStore store = new EventStore(this)) {
            for (int count = 0; count < 25 && !run.stopped; count++) {
                String[] event;
                String origin;
                String token;
                synchronized (Settings.LOCK) {
                    event = store.firstReady(System.currentTimeMillis());
                    if (event == null) return store.count() > 0;
                    Settings settings = new Settings(this);
                    origin = settings.origin();
                    token = new Vault(this).getToken();
                }
                if (origin.isEmpty() || token.isEmpty()) {
                    diagnostic("Нужны адрес приёмника и токен устройства");
                    return false;
                }
                String validatedOrigin = Settings.validateOrigin(origin);
                int response = 0;
                boolean confirmed = false;
                try {
                    HttpsURLConnection connection = (HttpsURLConnection) new URL(validatedOrigin + "/v1/events").openConnection();
                    try {
                        connection.setInstanceFollowRedirects(false);
                        connection.setConnectTimeout(12_000);
                        connection.setReadTimeout(12_000);
                        connection.setRequestMethod("POST");
                        connection.setDoOutput(true);
                        connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                        connection.setRequestProperty("Authorization", "Bearer " + token);
                        byte[] bytes = event[1].getBytes(StandardCharsets.UTF_8);
                        connection.setFixedLengthStreamingMode(bytes.length);
                        try (OutputStream output = connection.getOutputStream()) { output.write(bytes); }
                        response = connection.getResponseCode();
                        if (response == 200 || response == 201) confirmed = confirmed(connection);
                    } finally { connection.disconnect(); }
                } catch (java.io.IOException failure) {
                    diagnostic("Сеть или приёмник недоступны. Сигнал останется в очереди.");
                }
                if (confirmed) {
                    store.delivered(event[0]);
                    diagnostic("Сигнал принят приёмником");
                } else {
                    int attempts = Integer.parseInt(event[2]);
                    long delay = response >= 400 && response < 500 && response != 429
                            ? 900_000L : Math.min(3_600_000L, 30_000L * (1L << Math.min(attempts, 6)));
                    store.defer(event[0], System.currentTimeMillis() + delay);
                    if (response == 200 || response == 201) diagnostic("Приёмник не подтвердил сохранение. Запланирован повтор.");
                    else if (response != 0) diagnostic("Приёмник отклонил сигнал (код " + response + "). Запланирован повтор.");
                }
            }
            return store.count() > 0;
        }
    }
    private boolean confirmed(HttpsURLConnection connection) {
        String contentType = connection.getContentType();
        if (contentType == null || !contentType.split(";", 2)[0].trim().equalsIgnoreCase("application/json")) return false;
        try (InputStream input = connection.getInputStream(); ByteArrayOutputStream body = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[512];
            int count;
            while ((count = input.read(buffer)) != -1) {
                if (body.size() + count > 4096) return false;
                body.write(buffer, 0, count);
            }
            JSONTokener parser = new JSONTokener(new String(body.toByteArray(), StandardCharsets.UTF_8));
            Object value = parser.nextValue();
            return value instanceof JSONObject && Boolean.TRUE.equals(((JSONObject) value).opt("accepted"))
                    && parser.nextClean() == 0;
        } catch (Exception invalid) { return false; }
    }
    private void diagnostic(String message) {
        getSharedPreferences("diagnostics", MODE_PRIVATE).edit().putString("delivery", message).apply();
    }
    @Override public boolean onStopJob(JobParameters params) {
        synchronized (JOB_LOCK) {
            Run run = RUNS.get(params.getJobId());
            if (run != null && run.params == params) {
                RUNS.remove(params.getJobId());
                run.stopped = true;
                lastFinishedId = params.getJobId();
            }
        }
        return true;
    }
}
